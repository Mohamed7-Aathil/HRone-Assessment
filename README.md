# Employee Attendance & Analytics API

This is my submission for the HROne Software Engineer Trainee assignment. It's a
small FastAPI + MongoDB service that handles employee attendance — punch-in,
punch-out, manual corrections with an audit trail — plus a few analytics
endpoints built on MongoDB aggregation pipelines.

All application code lives in `app/main.py`.

## Stack

- Python 3.11
- FastAPI
- MongoDB (6.0+; I tested against MongoDB 7.0)
- PyMongo (sync driver)

## How to run

```bash
python -m venv .venv
.venv\Scripts\activate           # Windows
# source .venv/bin/activate      # macOS / Linux

pip install -r requirements.txt
copy .env.example .env           # then set MONGO_URI and MONGO_DB
# cp .env.example .env           # on macOS / Linux

python sample_seed.py
uvicorn app.main:app --port 8000
```
