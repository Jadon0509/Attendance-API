"""
Employee Attendance & Analytics API  (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB   (real environment variables win over a local .env)
"""
import calendar
import json
import os
import re
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Dict, List, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()  # override=False (default): real environment variables take priority

# tz_aware=True -> PyMongo returns aware UTC datetimes instead of naive ones.
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    tzinfo=timezone.utc,
    serverSelectionTimeoutMS=5000,
    maxPoolSize=100,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]
ALL_STATUS = ["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]

MONTH_RE = r"^\d{4}-(0[1-9]|1[0-2])$"
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
HHMM_RE = r"^([01]\d|2[0-3]):[0-5]\d$"

GRACE_SECONDS = 10 * 60
MIN_OVERTIME_MINUTES = 30
HALF_DAY_BELOW = Decimal("4.50")
MAX_TREND_DAYS = 92


# --------------------------------------------------------------------------- #
# Indexes (created at startup, idempotent)
# --------------------------------------------------------------------------- #
_indexes_ready = False


def ensure_indexes() -> None:
    global _indexes_ready
    emp, att = db.employees, db.attendance_logs
    # employees
    emp.create_index([("emp_code", 1)], unique=True, name="uniq_emp_code")
    emp.create_index([("department", 1), ("emp_code", 1)], name="dept_emp_code")
    emp.create_index([("department", 1), ("joined_on", 1)], name="dept_joined_on")
    emp.create_index([("joined_on", 1)], name="joined_on")
    # attendance_logs
    att.create_index([("emp_code", 1), ("date", 1)], unique=True, name="uniq_emp_date")
    att.create_index([("date", -1), ("emp_code", 1)], name="date_desc_emp")
    att.create_index([("status", 1), ("date", -1), ("emp_code", 1)], name="status_date_emp")
    att.create_index([("emp_code", 1), ("punch_in", -1)], name="emp_punch_in")
    _indexes_ready = True


@asynccontextmanager
async def lifespan(_: FastAPI):
    deadline = time.time() + 12
    while True:
        try:
            ensure_indexes()
            break
        except PyMongoError:
            if time.time() > deadline:
                break  # /health keeps answering 503 and retries index creation
            time.sleep(1)
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
async def mongo_error_handler(_, exc: PyMongoError):
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


# --------------------------------------------------------------------------- #
# Helpers: numbers, instants, dates
# --------------------------------------------------------------------------- #
def round_half_up(value: Any, places: int) -> float:
    """Half-up rounding (not banker's, not binary-float rounding)."""
    if isinstance(value, Decimal):
        d = value
    else:
        # strip binary noise (1e-16) before rounding so 0.125 stays a true tie
        d = Decimal(repr(float(value))).quantize(Decimal("1e-10"), rounding=ROUND_HALF_EVEN)
    return float(d.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def ms_to_dt(ms: int) -> datetime:
    """Epoch ms -> aware UTC datetime truncated to whole seconds (R1)."""
    return EPOCH + timedelta(seconds=ms // 1000)


def to_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    return (to_utc(dt) - EPOCH) // timedelta(milliseconds=1)


def now_utc() -> datetime:
    return EPOCH + timedelta(seconds=int(time.time()))


def parse_hhmm(s: str) -> tuple:
    h, m = s.split(":")
    return int(h), int(m)


def parse_ymd(s: str, field: str = "date") -> date:
    if not isinstance(s, str) or not DATE_RE.match(s):
        raise HTTPException(422, detail=[{"loc": ["query", field], "msg": "expected YYYY-MM-DD", "type": "value_error"}])
    try:
        return date.fromisoformat(s)
    except ValueError:
        raise HTTPException(422, detail=[{"loc": ["query", field], "msg": "invalid date", "type": "value_error"}])


def missing(*names: str):
    return HTTPException(
        422, detail=[{"loc": ["query", n], "msg": "field required", "type": "missing"} for n in names]
    )


def at_ist(d: date, hhmm: str) -> datetime:
    h, m = parse_hhmm(hhmm)
    return datetime(d.year, d.month, d.day, h, m, tzinfo=IST)


def is_overnight(emp: dict) -> bool:
    return emp["shift_end"] <= emp["shift_start"]  # zero padded HH:MM compares correctly


def attendance_date(punch_in: datetime, emp: dict) -> date:
    """R1: IST calendar date of the punch-in, shifted back a day for early overnight punches."""
    ist = punch_in.astimezone(IST)
    d = ist.date()
    if is_overnight(emp) and ist.strftime("%H:%M") < emp["shift_end"]:
        d -= timedelta(days=1)
    return d


def compute_late_minutes(punch_in: datetime, emp: dict, att_date: date) -> int:
    """R2: late only if strictly more than 10:00 after shift_start; floor minutes since shift_start."""
    seconds = int((punch_in - at_ist(att_date, emp["shift_start"])).total_seconds())
    return seconds // 60 if seconds > GRACE_SECONDS else 0


def compute_overtime(punch_out: datetime, emp: dict, att_date: date) -> int:
    """R3: floor minutes after shift_end, only when >= 30. Overnight shifts end on the next day."""
    end = at_ist(att_date, emp["shift_end"])
    if is_overnight(emp):
        end += timedelta(days=1)
    minutes = int((punch_out - end).total_seconds()) // 60
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600, 2 decimals half-up."""
    seconds = int((punch_out - punch_in).total_seconds())
    return round_half_up(Decimal(seconds) / Decimal(3600), 2)


def derive(status: str, punch_in, punch_out, emp: dict, att_date: date) -> dict:
    """All derived fields for a record, per the table in DATA_MODEL.md."""
    if status not in PRESENCE:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    out = {
        "work_hours": None,
        "late_minutes": compute_late_minutes(punch_in, emp, att_date),
        "overtime_minutes": 0,
        "half_day": False,
    }
    if punch_out is not None:
        wh = compute_work_hours(punch_in, punch_out)
        out["work_hours"] = wh
        out["overtime_minutes"] = compute_overtime(punch_out, emp, att_date)
        out["half_day"] = Decimal(repr(wh)) < HALF_DAY_BELOW  # R5 uses the rounded value
    return out


# --------------------------------------------------------------------------- #
# Serialisation (BSON datetime <-> epoch ms at the boundary)
# --------------------------------------------------------------------------- #
def emp_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc.get("name"),
        "email": doc.get("email"),
        "department": doc.get("department"),
        "shift_start": doc.get("shift_start"),
        "shift_end": doc.get("shift_end"),
        "joined_on": doc.get("joined_on"),
        "created_at": dt_to_ms(doc.get("created_at")),
    }


def _history_out(entry: dict) -> dict:
    changes = {}
    for key, ch in (entry.get("changes") or {}).items():
        changes[key] = {
            side: (dt_to_ms(ch.get(side)) if isinstance(ch.get(side), datetime) else ch.get(side))
            for side in ("from", "to")
        }
    return {"at": dt_to_ms(entry.get("at")), "by": entry.get("by"), "reason": entry.get("reason"), "changes": changes}


def att_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(doc.get("punch_in")),
        "punch_out": dt_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day") or False),
        "history": [_history_out(h) for h in (doc.get("history") or [])],
    }


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
EpochMs = Annotated[int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)]


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM_RE)
    shift_end: str = Field(default="18:30", pattern=HHMM_RE)
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def _valid_date(cls, v: str) -> str:
        if not DATE_RE.match(v):
            raise ValueError("expected YYYY-MM-DD")
        date.fromisoformat(v)  # raises ValueError on e.g. 2026-02-30
        return v

    @model_validator(mode="after")
    def _shift_differs(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    # default None is not validated, but an explicit JSON null is -> 422 (schemas are not nullable)
    status: Status = None  # type: ignore[assignment]
    punch_in: EpochMs = None  # type: ignore[assignment]
    punch_out: EpochMs = None  # type: ignore[assignment]
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# System + employees
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        db.command("ping")
        if not _indexes_ready:
            ensure_indexes()
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        db.employees.insert_one(dict(doc))  # unique index decides, not a prior find
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return emp_out(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q: Dict[str, Any] = {"department": department} if department else {}
    total = db.employees.count_documents(q)
    cur = db.employees.find(q, {"_id": 0}).sort("emp_code", 1).skip((page - 1) * page_size).limit(page_size)
    return {"items": [emp_out(d) for d in cur], "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance
# --------------------------------------------------------------------------- #
def get_employee(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if not emp:
        raise HTTPException(404, "employee not found")
    return emp


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    d = attendance_date(ts, emp)
    doc = {
        "emp_code": body.emp_code,
        "date": d.isoformat(),
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp, d),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date): exactly one racer wins
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return att_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", -1)]
    )
    if not rec:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    p_in = to_utc(rec["punch_in"])
    if ts <= p_in:
        raise HTTPException(422, "punched_at must be after punch_in")
    if ts - p_in > timedelta(hours=24):
        raise HTTPException(422, "punch_out is more than 24 hours after punch_in")
    d = date.fromisoformat(rec["date"])
    der = derive(rec["status"], p_in, ts, emp, d)
    # compare-and-set on punch_out: of two simultaneous requests only one matches
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": {"punch_out": ts, **{k: der[k] for k in ("work_hours", "overtime_minutes", "half_day")}}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return att_out(updated)


def build_attendance_query(emp_code, date_from, date_to, status):
    """Filter / sort / hint shared by GET /attendance and its explain."""
    q: Dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    sort = {"date": -1, "emp_code": 1}
    if emp_code:
        hint = {"emp_code": 1, "date": 1}
    elif status:
        hint = {"status": 1, "date": -1, "emp_code": 1}
    else:
        hint = {"date": -1, "emp_code": 1}
    return q, sort, hint


def validate_attendance_filters(date_from, date_to):
    df = parse_ymd(date_from, "date_from") if date_from else None
    dt = parse_ymd(date_to, "date_to") if date_to else None
    if df and dt and df > dt:
        raise HTTPException(422, detail=[{"loc": ["query", "date_from"], "msg": "date_from after date_to", "type": "value_error"}])


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    validate_attendance_filters(date_from, date_to)
    q, sort, hint = build_attendance_query(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q) if q else db.attendance_logs.estimated_document_count()
    cur = (
        db.attendance_logs.find(q, {"_id": 0})
        .sort(list(sort.items()))
        .hint(list(hint.items()))
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {"items": [att_out(d) for d in cur], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, body: RegularizeIn, date: str = Path(...)):
    d = parse_ymd(date, "date")
    emp = get_employee(emp_code)
    for _attempt in range(6):  # optimistic concurrency: re-read and retry if we lost a race
        rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": d.isoformat()})
        if not rec:
            raise HTTPException(404, "no attendance record for that date")

        status = body.status or rec["status"]
        if status in ("ABSENT", "LEAVE"):
            if body.punch_in is not None or body.punch_out is not None:
                raise HTTPException(422, "ABSENT/LEAVE records cannot have punch times")
            p_in = p_out = None
        else:
            p_in = ms_to_dt(body.punch_in) if body.punch_in is not None else rec.get("punch_in")
            p_out = ms_to_dt(body.punch_out) if body.punch_out is not None else rec.get("punch_out")
            if p_in is None:
                raise HTTPException(422, "a presence status requires punch_in")
            p_in = to_utc(p_in)
            p_out = to_utc(p_out) if p_out is not None else None
            if attendance_date(p_in, emp) != d:
                raise HTTPException(422, "punch_in must stay on the record's attendance date")
            if p_out is not None and (p_out <= p_in or p_out - p_in > timedelta(hours=24)):
                raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")

        cur_in = to_utc(rec["punch_in"]) if rec.get("punch_in") else None
        cur_out = to_utc(rec["punch_out"]) if rec.get("punch_out") else None
        if status == rec["status"] and p_in == cur_in and p_out == cur_out:
            raise HTTPException(422, "request changes nothing")

        new = {"status": status, "punch_in": p_in, "punch_out": p_out, **derive(status, p_in, p_out, emp, d)}
        old = {
            "status": rec["status"],
            "punch_in": cur_in,
            "punch_out": cur_out,
            "work_hours": rec.get("work_hours"),
            "late_minutes": rec.get("late_minutes") or 0,
            "overtime_minutes": rec.get("overtime_minutes") or 0,
            "half_day": bool(rec.get("half_day") or False),
        }
        changes = {k: {"from": old[k], "to": new[k]} for k in new if new[k] != old[k]}
        entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}

        # Guard: nothing relevant changed since we read the record, and history length is unchanged.
        hist = rec.get("history")
        guard: Dict[str, Any] = {
            "_id": rec["_id"],
            "status": rec["status"],
            "punch_in": rec.get("punch_in"),
            "punch_out": rec.get("punch_out"),
            "history": {"$size": len(hist)} if hist is not None else {"$exists": False},
        }
        updated = db.attendance_logs.find_one_and_update(
            guard, {"$set": new, "$push": {"history": entry}}, return_document=ReturnDocument.AFTER
        )
        if updated is not None:
            return att_out(updated)
    raise HTTPException(409, "concurrent modification, please retry")


# --------------------------------------------------------------------------- #
# Analytics: shared pieces
# --------------------------------------------------------------------------- #
def month_bounds(month: str):
    y, m = int(month[:4]), int(month[5:7])
    last = calendar.monthrange(y, m)[1]
    return date(y, m, 1), date(y, m, last)


def _present(p):  # R6
    return {"$in": [f"{p}status", PRESENCE]}


def _weekday(p):  # Mon-Fri; date strings are plain calendar dates, parsed at UTC midnight
    dow = {"$dayOfWeek": {"$dateFromString": {"dateString": f"{p}date", "onError": None}}}
    return {"$and": [{"$gte": [dow, 2]}, {"$lte": [dow, 6]}]}


def _half_weight(p):  # R5: half day = 0.5
    return {"$cond": [{"$eq": [{"$ifNull": [f"{p}half_day", False]}, True]}, 0.5, 1]}


def x_present_any_day(p):
    return {"$cond": [_present(p), _half_weight(p), 0]}


def x_present_weekday(p):  # R7
    return {"$cond": [{"$and": [_present(p), _weekday(p)]}, _half_weight(p), 0]}


def x_late_flag(p):
    return {"$cond": [{"$gt": [{"$ifNull": [f"{p}late_minutes", 0]}, 0]}, 1, 0]}


def x_late_minutes(p):
    return {"$cond": [{"$gt": [{"$ifNull": [f"{p}late_minutes", 0]}, 0]}, f"{p}late_minutes", 0]}


def x_overtime(p):
    return {"$ifNull": [f"{p}overtime_minutes", 0]}


def x_is(p, status):
    return {"$cond": [{"$eq": [f"{p}status", status]}, 1, 0]}


def _has_hours(p):
    return {"$and": [_present(p), {"$isNumber": f"{p}work_hours"}]}


def x_hours_dec(p):  # exact decimal sum, so the average is not polluted by float error
    return {"$cond": [_has_hours(p), {"$toDecimal": f"{p}work_hours"}, 0]}


def x_hours_flag(p):
    return {"$cond": [_has_hours(p), 1, 0]}


def _logs_lookup(start_s: str, end_s: str, fields: dict) -> dict:
    return {
        "$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [{"$match": {"date": {"$gte": start_s, "$lte": end_s}}}, {"$project": {"_id": 0, **fields}}],
            "as": "logs",
        }
    }


LOG_FIELDS = {"date": 1, "status": 1, "half_day": 1, "late_minutes": 1, "overtime_minutes": 1, "work_hours": 1}


def working_days(start: date, end: date, joined_on: date) -> int:
    d, n = max(start, joined_on), 0
    while d <= end:
        n += d.weekday() < 5
        d += timedelta(days=1)
    return n


# ---- employee monthly ------------------------------------------------------ #
def monthly_pipeline(emp_code: str, month: str):
    s, e = (x.isoformat() for x in month_bounds(month))
    p = "$"
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": s, "$lte": e}}},
        {
            "$group": {
                "_id": None,
                "present_days": {"$sum": x_present_weekday(p)},
                "leave_days": {"$sum": x_is(p, "LEAVE")},
                "late_count": {"$sum": x_late_flag(p)},
                "total_late_minutes": {"$sum": x_late_minutes(p)},
                "total_overtime_minutes": {"$sum": x_overtime(p)},
            }
        },
    ]
    return "attendance_logs", pipeline, {"emp_code": 1, "date": 1}


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=MONTH_RE)):
    emp = get_employee(emp_code)
    start, end = month_bounds(month)
    coll, pipeline, hint = monthly_pipeline(emp_code, month)
    row = next(iter(db[coll].aggregate(pipeline, hint=hint)), {})
    wd = working_days(start, end, date.fromisoformat(emp["joined_on"]))
    present = Decimal(repr(float(row.get("present_days", 0))))
    pct = round_half_up(present * 100 / Decimal(wd), 2) if wd else None
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": wd,
        "present_days": round_half_up(present, 2),
        "leave_days": int(row.get("leave_days", 0)),
        "late_count": int(row.get("late_count", 0)),
        "total_late_minutes": int(row.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(row.get("total_overtime_minutes", 0)),
        "attendance_pct": pct,
    }


# ---- department summary ---------------------------------------------------- #
def summary_pipeline(month: str, department: Optional[str]):
    start, end = month_bounds(month)
    s, e = start.isoformat(), end.isoformat()
    match: Dict[str, Any] = {"joined_on": {"$lte": e}}  # R9
    if department:
        match["department"] = department
    hint = {"department": 1, "joined_on": 1} if department else {"joined_on": 1}
    L = "$$l."

    def over_logs(expr):
        return {"$sum": {"$map": {"input": "$logs", "as": "l", "in": expr}}}

    pipeline = [
        {"$match": match},
        _logs_lookup(s, e, LOG_FIELDS),
        {
            "$project": {
                "_id": 0,
                "department": 1,
                "pd": over_logs(x_present_weekday(L)),
                "lc": over_logs(x_late_flag(L)),
                "lm": over_logs(x_late_minutes(L)),
                "lv": over_logs(x_is(L, "LEAVE")),
                "od": over_logs(x_is(L, "ON_DUTY")),
                "hs": over_logs(x_hours_dec(L)),
                "hn": over_logs(x_hours_flag(L)),
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},  # one row per employee, logs or not
                "present_days": {"$sum": "$pd"},
                "late_count": {"$sum": "$lc"},
                "total_late_minutes": {"$sum": "$lm"},
                "leave_count": {"$sum": "$lv"},
                "on_duty_count": {"$sum": "$od"},
                "hours_sum": {"$sum": "$hs"},
                "hours_n": {"$sum": "$hn"},
            }
        },
        {"$sort": {"_id": 1}},
    ]
    return "employees", pipeline, hint


def _dec(v) -> Decimal:
    return v.to_decimal() if hasattr(v, "to_decimal") else Decimal(repr(float(v)))


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=MONTH_RE), department: Optional[str] = None):
    coll, pipeline, hint = summary_pipeline(month, department)
    items = []
    for r in db[coll].aggregate(pipeline, hint=hint):
        n = int(r["hours_n"])
        avg = round_half_up(_dec(r["hours_sum"]) / n, 2) if n else None
        items.append(
            {
                "department": r["_id"],
                "headcount": int(r["headcount"]),
                "present_days": round_half_up(_dec(r["present_days"]), 2),
                "avg_work_hours": avg,
                "late_count": int(r["late_count"]),
                "total_late_minutes": int(r["total_late_minutes"]),
                "leave_count": int(r["leave_count"]),
                "on_duty_count": int(r["on_duty_count"]),
            }
        )
    return {"month": month, "items": items}


# ---- late leaderboard ------------------------------------------------------ #
def leaderboard_pipeline(month: str, limit: int, department: Optional[str]):
    start, end = month_bounds(month)
    pipeline: List[dict] = [
        {"$match": {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total": {"$sum": "$late_minutes"}, "cnt": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # logs of unknown employees disappear here
    ]
    if department:
        pipeline.append({"$match": {"emp.department": department}})
    pipeline += [
        # $rank = standard competition ranking (1, 2, 2, 4). Rank on the total ONLY, so ties stay ties.
        {"$setWindowFields": {"sortBy": {"total": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applied after ranking
        {"$sort": {"total": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": "$total",
                "late_count": "$cnt",
            }
        },
    ]
    return "attendance_logs", pipeline, {"date": -1, "emp_code": 1}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=MONTH_RE),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    coll, pipeline, hint = leaderboard_pipeline(month, limit, department)
    items = [
        {
            "rank": int(r["rank"]),
            "emp_code": r["emp_code"],
            "name": r["name"],
            "department": r["department"],
            "total_late_minutes": int(r["total_late_minutes"]),
            "late_count": int(r["late_count"]),
        }
        for r in db[coll].aggregate(pipeline, hint=hint)
    ]
    return {"month": month, "items": items}


# ---- department trend ------------------------------------------------------ #
def validate_trend_range(d_from: str, d_to: str):
    f, t = parse_ymd(d_from, "from"), parse_ymd(d_to, "to")
    if t < f or (t - f).days + 1 > MAX_TREND_DAYS:
        raise HTTPException(422, detail=[{"loc": ["query", "to"], "msg": "to < from or range exceeds 92 days", "type": "value_error"}])
    return f, t


def trend_pipeline(department: str, f: date, t: date):
    fs, ts = f.isoformat(), t.isoformat()
    from_dt = datetime(f.year, f.month, f.day, tzinfo=UTC)
    to_excl = datetime(t.year, t.month, t.day, tzinfo=UTC) + timedelta(days=1)  # $densify upper bound is exclusive
    L = "$$l."
    # Every employee emits: a base event on `from` (so the stream is never empty), a "joined" event
    # (clamped to `from` for people who joined earlier), and one event per attendance record.
    events = {
        "$concatArrays": [
            [{"d": fs, "hc": 0, "present": 0, "late": 0}],
            [{"d": {"$max": ["$joined_on", fs]}, "hc": 1, "present": 0, "late": 0}],
            {
                "$map": {
                    "input": "$logs",
                    "as": "l",
                    "in": {"d": f"{L}date", "hc": 0, "present": x_present_any_day(L), "late": x_late_flag(L)},
                }
            },
        ]
    }
    dow = {"$dayOfWeek": "$dt"}
    pipeline = [
        {"$match": {"department": department, "joined_on": {"$lte": ts}}},
        _logs_lookup(fs, ts, {"date": 1, "status": 1, "half_day": 1, "late_minutes": 1}),
        {"$project": {"_id": 0, "events": events}},
        {"$unwind": "$events"},
        {
            "$group": {
                "_id": "$events.d",
                "hc": {"$sum": "$events.hc"},
                "present": {"$sum": "$events.present"},
                "late": {"$sum": "$events.late"},
            }
        },
        {"$addFields": {"dt": {"$dateFromString": {"dateString": "$_id"}}}},
        {"$densify": {"field": "dt", "range": {"step": 1, "unit": "day", "bounds": [from_dt, to_excl]}}},
        {"$addFields": {"hc": {"$ifNull": ["$hc", 0]}, "present": {"$ifNull": ["$present", 0]}, "late": {"$ifNull": ["$late", 0]}}},
        {
            "$setWindowFields": {
                "sortBy": {"dt": 1},
                "output": {"headcount": {"$sum": "$hc", "window": {"documents": ["unbounded", "current"]}}},
            }
        },
        {"$addFields": {"is_working_day": {"$and": [{"$gte": [dow, 2]}, {"$lte": [dow, 6]}]}}},
        {
            "$addFields": {
                "rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {"$divide": ["$present", "$headcount"]},
                        None,
                    ]
                }
            }
        },
        {"$setWindowFields": {"sortBy": {"dt": 1}, "output": {"mavg": {"$avg": "$rate", "window": {"documents": [-6, 0]}}}}},
        {"$sort": {"dt": 1}},
        {
            "$project": {
                "_id": 0,
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$dt"}},
                "is_working_day": 1,
                "headcount": 1,
                "present": 1,
                "late": 1,
                "mavg": 1,
            }
        },
    ]
    return "employees", pipeline, {"department": 1, "joined_on": 1}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(..., alias="from"), to: str = Query(...)):
    f, t = validate_trend_range(from_, to)
    if db.employees.count_documents({"department": department}, limit=1) == 0:
        raise HTTPException(404, "unknown department")
    coll, pipeline, hint = trend_pipeline(department, f, t)
    items = []
    for r in db[coll].aggregate(pipeline, hint=hint):
        hc = int(r["headcount"])
        rate = None
        if r["is_working_day"] and hc > 0:
            rate = round_half_up(Decimal(repr(float(r["present"]))) / Decimal(hc), 4)
        items.append(
            {
                "date": r["date"],
                "is_working_day": bool(r["is_working_day"]),
                "headcount": hc,
                "present_count": float(r["present"]),
                "late_count": int(r["late"]),
                "attendance_rate": rate,
                "moving_avg_7d": round_half_up(r["mavg"], 4) if r.get("mavg") is not None else None,
            }
        )
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin: explain
# --------------------------------------------------------------------------- #
ExplainName = Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]


def _jsonable(doc: Any) -> Any:
    return json.loads(json_util.dumps(doc, json_options=json_util.RELAXED_JSON_OPTIONS))


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: ExplainName,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_RE),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        validate_attendance_filters(date_from, date_to)
        q, sort, hint = build_attendance_query(emp_code, date_from, date_to, status)
        coll = "attendance_logs"
        cmd = {
            "find": coll, "filter": q, "sort": sort, "hint": hint,
            "skip": (page - 1) * page_size, "limit": page_size,
        }
    else:
        if endpoint == "employee_monthly":
            if not emp_code or not month:
                raise missing(*[n for n, v in (("emp_code", emp_code), ("month", month)) if not v])
            coll, pipeline, hint = monthly_pipeline(emp_code, month)
        elif endpoint == "department_summary":
            if not month:
                raise missing("month")
            coll, pipeline, hint = summary_pipeline(month, department)
        elif endpoint == "late_leaderboard":
            if not month:
                raise missing("month")
            coll, pipeline, hint = leaderboard_pipeline(month, limit, department)
        else:  # department_trend
            if not department or not from_ or not to:
                raise missing(*[n for n, v in (("department", department), ("from", from_), ("to", to)) if not v])
            f, t = validate_trend_range(from_, to)
            coll, pipeline, hint = trend_pipeline(department, f, t)
        cmd = {"aggregate": coll, "pipeline": pipeline, "cursor": {}, "hint": hint}
    result = db.command({"explain": cmd, "verbosity": "executionStats"})
    return {"endpoint": endpoint, "collection": coll, "explain": _jsonable(result)}
