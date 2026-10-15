"""
Live check (NOT part of the graded submission - keep it out of the repo if you like).

Seeds awkward data DIRECTLY into MongoDB (db name: MONGO_DB, default 'attendance_check' -- it is DROPPED!),
then calls the running API and compares analytics against an independent pure-Python oracle.

  1. MONGO_DB=attendance_check uvicorn app.main:app --port 8000      (in one terminal)
  2. MONGO_DB=attendance_check python tools/live_check.py            (in another)
"""
import os, sys, random, itertools, requests
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pymongo import MongoClient

BASE = os.getenv("BASE", "http://localhost:8000")
db = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True)[os.getenv("MONGO_DB", "attendance_check")]
PRES = {"PRESENT", "WFH", "ON_DUTY"}
U = timezone.utc

def hu(x, nd):  # half-up
    return float(Decimal(str(x)).quantize(Decimal(1).scaleb(-nd), ROUND_HALF_UP))

def emp(code, dept, joined, ss="09:30", se="18:30"):
    return dict(emp_code=code, name="N" + code, email="x@y.com", department=dept, shift_start=ss, shift_end=se,
                joined_on=joined, created_at=datetime(2026, 1, 1, tzinfo=U))

def log(code, d, status="PRESENT", late=0, ot=0, wh=8.0, half=False):
    pres = status in PRES
    base = datetime.fromisoformat(d).replace(tzinfo=U) + timedelta(hours=4)
    return dict(emp_code=code, date=d, status=status, punch_in=base if pres else None,
                punch_out=base + timedelta(hours=wh) if pres and wh else None, work_hours=wh if pres else None,
                late_minutes=late, overtime_minutes=ot, half_day=half, history=[])

def seed():
    db.employees.delete_many({}); db.attendance_logs.delete_many({})
    E = [emp("EMP0001", "Eng", "2026-01-05"), emp("EMP0002", "Eng", "2026-07-15"),   # mid-month joiner
         emp("EMP0003", "Eng", "2026-01-05"), emp("EMP0004", "Ops", "2026-01-05"),
         emp("EMP0005", "Ops", "2026-01-05"),                                         # no logs at all
         emp("EMP0006", "Sales", "2026-08-10")]                                       # joins after July
    db.employees.insert_many(E)
    L = []
    days = [date(2026, 7, 1) + timedelta(days=i) for i in range(31)]
    random.seed(7)
    for code in ("EMP0001", "EMP0003", "EMP0004", "EMP0002"):
        for d in days:
            if d.weekday() == 4 and code == "EMP0003": continue                       # missing weekday
            if code == "EMP0002" and d < date(2026, 7, 15): continue
            r = random.random()
            if r < .1: L.append(log(code, d.isoformat(), "LEAVE", wh=0))
            elif r < .15: L.append(log(code, d.isoformat(), "ABSENT", wh=0))
            else:
                st = random.choice(["PRESENT", "PRESENT", "WFH", "ON_DUTY"])
                wh = random.choice([3.5, 4.5, 8.0, 8.37, 9.125])
                L.append(log(code, d.isoformat(), st, late=random.choice([0, 0, 15, 15, 30]), ot=random.choice([0, 40]),
                             wh=wh, half=wh < 4.5))
    L.append(log("EMP0001", "2026-07-04", "PRESENT", late=20, ot=45))                # Saturday record
    L = [x for x in L if not (x["emp_code"] == "EMP0001" and x["date"] == "2026-07-04")] + [L[-1]]
    L.append(log("GHOST99", "2026-07-06", late=500))                                  # employee does not exist
    db.attendance_logs.insert_many(L)
    return E, L

# ------------------------------ oracle -------------------------------------
def wk(d): return date.fromisoformat(d).weekday() < 5
def month_days(m):
    y, mo = map(int, m.split("-")); d = date(y, mo, 1)
    while d.month == mo: yield d; d += timedelta(days=1)
def pdays(l): return (0.5 if l.get("half_day") else 1) if l["status"] in PRES and wk(l["date"]) else 0

def o_monthly(E, L, code, m):
    e = next(x for x in E if x["emp_code"] == code); ls = [l for l in L if l["emp_code"] == code and l["date"][:7] == m]
    wd = sum(1 for d in month_days(m) if d.weekday() < 5 and d.isoformat() >= e["joined_on"])
    pd = sum(pdays(l) for l in ls)
    return dict(working_days=wd, present_days=hu(pd, 2), leave_days=sum(l["status"] == "LEAVE" for l in ls),
                late_count=sum(l["late_minutes"] > 0 for l in ls), total_late_minutes=sum(l["late_minutes"] for l in ls),
                total_overtime_minutes=sum(l["overtime_minutes"] for l in ls), attendance_pct=hu(pd * 100 / wd, 2) if wd else None)

def o_summary(E, L, m):
    end = list(month_days(m))[-1].isoformat(); out = {}
    for e in E:
        if e["joined_on"] > end: continue
        ls = [l for l in L if l["emp_code"] == e["emp_code"] and l["date"][:7] == m]
        o = out.setdefault(e["department"], dict(department=e["department"], headcount=0, present_days=0, hs=Decimal(0), hn=0,
                                                 late_count=0, total_late_minutes=0, leave_count=0, on_duty_count=0))
        o["headcount"] += 1; o["present_days"] += sum(pdays(l) for l in ls)
        for l in ls:
            if l["status"] in PRES and l.get("work_hours") is not None: o["hs"] += Decimal(str(l["work_hours"])); o["hn"] += 1
            if l["late_minutes"] > 0: o["late_count"] += 1; o["total_late_minutes"] += l["late_minutes"]
            o["leave_count"] += l["status"] == "LEAVE"; o["on_duty_count"] += l["status"] == "ON_DUTY"
    res = []
    for k in sorted(out):
        o = out[k]; res.append(dict(department=k, headcount=o["headcount"], present_days=hu(o["present_days"], 2),
            avg_work_hours=hu(o["hs"] / o["hn"], 2) if o["hn"] else None, late_count=o["late_count"],
            total_late_minutes=o["total_late_minutes"], leave_count=o["leave_count"], on_duty_count=o["on_duty_count"]))
    return res

def o_leader(E, L, m, limit, dept=None):
    names = {e["emp_code"]: e for e in E}; tot = {}
    for l in L:
        if l["date"][:7] == m and l["late_minutes"] > 0 and l["emp_code"] in names and (not dept or names[l["emp_code"]]["department"] == dept):
            t = tot.setdefault(l["emp_code"], [0, 0]); t[0] += l["late_minutes"]; t[1] += 1
    rows = sorted(tot.items(), key=lambda kv: (-kv[1][0], kv[0])); out = []
    for i, (c, (t, n)) in enumerate(rows):
        rank = 1 + sum(1 for _, (t2, _) in rows if t2 > t)
        if rank <= limit: out.append(dict(rank=rank, emp_code=c, name=names[c]["name"], department=names[c]["department"], total_late_minutes=t, late_count=n))
    return out

def o_trend(E, L, dept, f, t):
    f, t = date.fromisoformat(f), date.fromisoformat(t); rows = []; rates = []
    d = f
    while d <= t:
        ds = d.isoformat(); hc = sum(1 for e in E if e["department"] == dept and e["joined_on"] <= ds)
        codes = {e["emp_code"] for e in E if e["department"] == dept}
        ls = [l for l in L if l["date"] == ds and l["emp_code"] in codes]
        pc = sum((0.5 if l.get("half_day") else 1) for l in ls if l["status"] in PRES)
        rate = pc / hc if d.weekday() < 5 and hc > 0 else None
        rates.append(rate); w = [r for r in rates[-7:] if r is not None]
        rows.append(dict(date=ds, is_working_day=d.weekday() < 5, headcount=hc, present_count=pc,
                         late_count=sum(l["late_minutes"] > 0 for l in ls),
                         attendance_rate=hu(Decimal(str(pc)) / hc, 4) if rate is not None else None,
                         moving_avg_7d=hu(sum(w) / len(w), 4) if w else None))
        d += timedelta(days=1)
    return rows

# ------------------------------ checks -------------------------------------
bad = 0
def check(name, got, exp):
    global bad
    if got == exp: print("PASS", name)
    else:
        bad += 1; print("FAIL", name); print("  got:", got); print("  exp:", exp)

def main():
    E, L = seed(); g = lambda p, **q: requests.get(BASE + p, params=q)
    assert g("/health").status_code == 200
    for code in ("EMP0001", "EMP0002", "EMP0003", "EMP0004", "EMP0005", "EMP0006"):
        r = g(f"/analytics/employees/{code}/monthly", month="2026-07").json(); r.pop("emp_code"); r.pop("month")
        check(f"monthly {code}", r, o_monthly(E, L, code, "2026-07"))
    check("summary", g("/analytics/departments/summary", month="2026-07").json()["items"], o_summary(E, L, "2026-07"))
    check("summary Eng only", g("/analytics/departments/summary", month="2026-07", department="Eng").json()["items"],
          [x for x in o_summary(E, L, "2026-07") if x["department"] == "Eng"])
    check("summary Aug has Sales", [x["department"] for x in g("/analytics/departments/summary", month="2026-08").json()["items"]],
          [x["department"] for x in o_summary(E, L, "2026-08")])
    for lim in (1, 2, 3, 10):
        check(f"leaderboard limit={lim}", g("/analytics/leaderboard/late", month="2026-07", limit=lim).json()["items"], o_leader(E, L, "2026-07", lim))
    check("leaderboard Eng", g("/analytics/leaderboard/late", month="2026-07", department="Eng").json()["items"], o_leader(E, L, "2026-07", 10, "Eng"))
    for dept, f, t in (("Eng", "2026-07-01", "2026-07-31"), ("Ops", "2026-06-28", "2026-07-12"), ("Sales", "2026-07-25", "2026-08-15"), ("Eng", "2026-07-13", "2026-07-13")):
        r = g(f"/analytics/departments/{dept}/trend", **{"from": f, "to": t}).json()["items"]
        check(f"trend {dept} {f}..{t} ({len(r)} rows)", r, o_trend(E, L, dept, f, t))
    check("trend unknown dept -> 404", g("/analytics/departments/Nope/trend", **{"from": "2026-07-01", "to": "2026-07-02"}).status_code, 404)
    check("trend >92d -> 422", g("/analytics/departments/Eng/trend", **{"from": "2026-01-01", "to": "2026-06-01"}).status_code, 422)
    for ep, q in (("attendance_list", dict(emp_code="EMP0001")), ("attendance_list", dict(date_from="2026-07-01")),
                  ("attendance_list", dict(status="LEAVE")), ("employee_monthly", dict(emp_code="EMP0001", month="2026-07")),
                  ("department_summary", dict(month="2026-07")), ("department_summary", dict(month="2026-07", department="Eng")),
                  ("late_leaderboard", dict(month="2026-07")), ("department_trend", {"department": "Eng", "from": "2026-07-01", "to": "2026-07-31"})):
        r = g(f"/admin/explain/{ep}", **q); txt = r.text
        ok = r.status_code == 200 and "IXSCAN" in txt and '"COLLSCAN"' not in txt
        check(f"explain {ep} {list(q)} IXSCAN/no COLLSCAN", ok, True)
    print("\nFAILURES:", bad); sys.exit(1 if bad else 0)

if __name__ == "__main__":
    main()
