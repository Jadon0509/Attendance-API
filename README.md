# Employee Attendance & Analytics API

FastAPI + MongoDB. All application code is in `app/main.py`.

## Run
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017" MONGO_DB="attendance_db"   # or put them in a local .env
uvicorn app.main:app --port 8000
```
Indexes are created automatically at startup (idempotent). `GET /health` returns 200 once MongoDB answers.

## Notes
- Every endpoint in `openapi.yaml` is implemented, including `GET /admin/explain/{endpoint}`.
- Analytics (employee monthly, department summary, late leaderboard, department trend) are MongoDB aggregation
  pipelines; Python only formats the numbers (half-up rounding, epoch-ms conversion).
- Review of the starter code: `REVIEW.md`. Design answers: `DECISIONS.md`.
- `tools/live_check.py` is my own check script (seeds awkward data, compares the API with a pure-Python calculation).
  It is optional and not needed to run the app.

## Testing status
- Business rules R1-R5, CRUD, the PATCH audit trail and the 404/409/422 behaviour were checked against a mocked database.
- The aggregation pipelines follow the MongoDB 6/7 documentation. `tools/live_check.py` seeds awkward data into a real
  MongoDB and compares every analytics endpoint (and the explain plans) with an independent pure-Python calculation.
- The moving average in the trend uses the exact (unrounded) daily rates and rounds only the final value.
