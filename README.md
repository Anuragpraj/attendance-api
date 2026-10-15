# Employee Attendance & Analytics API

FastAPI + MongoDB (6.0 or newer). All code is in `app/main.py`; indexes are created automatically at startup.

## Run
```bash
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt
export MONGO_URI="mongodb+srv://<user>:<password>@<cluster>.mongodb.net"   # or mongodb://localhost:27017
export MONGO_DB="attendance_db"
uvicorn app.main:app --port 8000
```
Interactive docs: http://localhost:8000/docs. A local `.env` also works, but real environment variables win. No `.env` is included in this repository.

## Things to know
- Everything in the brief is implemented. I checked all endpoints by hand against the sample data on a free Atlas cluster, but I did not build a large 100,000-record dataset, so index behaviour at that size is shown only through the explain endpoint.
- `moving_avg_7d` averages the unrounded daily rates and rounds once at the end.
- If two corrections hit the same record at the same moment, one succeeds and the other gets 409 (retry), so no history entry is lost.
