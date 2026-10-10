import os
import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError


load_dotenv()


def _aware(dt: datetime) -> datetime:
    """Return a tz-aware datetime. Naive values are assumed UTC."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}
STATUSES = PRESENCE | {"ABSENT", "LEAVE"}
MAX_EPOCH_MS = 4_102_444_800_000

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017/"),
    serverSelectionTimeoutMS=3000,
)
db = client[os.getenv("MONGO_DB", "candidate_kit")]
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# ----------------------------- Validation models -----------------------------

def to_datetime(value: int) -> datetime:
    if type(value) is not int or not 100_000_000_000 <= value <= MAX_EPOCH_MS:
        raise ValueError("Timestamp must be valid integer epoch milliseconds")
    try:
        return datetime.fromtimestamp(value // 1000, UTC)
    except (ValueError, OverflowError, OSError):
        raise ValueError("Timestamp out of range")


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(min_length=3, max_length=120)
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    joined_on: date

    @field_validator("email")
    @classmethod
    def check_email(cls, value):
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
            raise ValueError("Invalid email")
        return value

    @model_validator(mode="after")
    def check_shifts(self):
        if self.shift_start == self.shift_end:
            raise ValueError("Shift start and end must differ")
        return self


class PunchInIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[int] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def check_timestamp(cls, value):
        if value is not None:
            to_datetime(value)
        return value


class PunchOutIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[int] = None

    @field_validator("punched_at")
    @classmethod
    def check_timestamp(cls, value):
        if value is not None:
            to_datetime(value)
        return value


class RegularizeIn(BaseModel):
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)
    status: Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None

    @field_validator("punch_in", "punch_out")
    @classmethod
    def check_timestamp(cls, value):
        if value is not None:
            to_datetime(value)
        return value


# --------------------------------- Helpers -----------------------------------

def fail(code: int, message: str):
    raise HTTPException(status_code=code, detail=message)


def as_utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).replace(microsecond=0)


def epoch_ms(value: Optional[datetime]) -> Optional[int]:
    value = as_utc(value)
    return int(value.timestamp() * 1000) if value else None


def parse_date(value: str) -> date:
    try:
        result = date.fromisoformat(value)
        if result.isoformat() != value:
            raise ValueError
        return result
    except (ValueError, TypeError):
        fail(422, "Date must use YYYY-MM-DD format")


def parse_month(value: str):
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value or ""):
        fail(422, "Month must use YYYY-MM format")
    start = date.fromisoformat(value + "-01")
    end = date(start.year + (start.month == 12),
               1 if start.month == 12 else start.month + 1, 1)
    return start, end


def mins(value: str) -> int:
    h, m = map(int, value.split(":"))
    return h * 60 + m


def shift_start(day: str, employee: dict) -> datetime:
    return datetime.combine(
        parse_date(day), time.fromisoformat(employee["shift_start"]), IST
    ).astimezone(UTC)


def shift_end(day: str, employee: dict) -> datetime:
    d = parse_date(day)
    if mins(employee["shift_end"]) <= mins(employee["shift_start"]):
        d += timedelta(days=1)
    return datetime.combine(
        d, time.fromisoformat(employee["shift_end"]), IST
    ).astimezone(UTC)


def attendance_day(punch: datetime, employee: dict) -> str:
    local = as_utc(punch).astimezone(IST)
    if mins(employee["shift_end"]) <= mins(employee["shift_start"]):
        if mins(local.strftime("%H:%M")) < mins(employee["shift_end"]):
            return (local.date() - timedelta(days=1)).isoformat()
    return local.date().isoformat()


def late_minutes(punch: datetime, day: str, employee: dict) -> int:
    seconds = int((as_utc(punch) - shift_start(day, employee)).total_seconds())
    return seconds // 60 if seconds > 600 else 0


def overtime_minutes(punch: datetime, day: str, employee: dict) -> int:
    seconds = int((as_utc(punch) - shift_end(day, employee)).total_seconds())
    minutes = seconds // 60
    return minutes if minutes >= 30 else 0


def rounded_hours(start: datetime, end: datetime) -> float:
    seconds = int((as_utc(end) - as_utc(start)).total_seconds())
    return float((Decimal(seconds) / Decimal(3600)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    ))


def recalculate(record: dict, employee: dict) -> dict:
    status = record["status"]
    pin = as_utc(record.get("punch_in"))
    pout = as_utc(record.get("punch_out"))

    if status in {"ABSENT", "LEAVE"}:
        pin = pout = None

    if status in PRESENCE and pin is None:
        fail(422, "Presence status requires punch_in")

    if pin and pout:
        seconds = (pout - pin).total_seconds()
        if seconds <= 0 or seconds > 86400:
            fail(422, "punch_out must be after punch_in and within 24 hours")

    record["punch_in"] = pin
    record["punch_out"] = pout
    record["late_minutes"] = late_minutes(pin, record["date"], employee) if pin and status in PRESENCE else 0
    record["work_hours"] = rounded_hours(pin, pout) if pin and pout else None
    record["overtime_minutes"] = overtime_minutes(pout, record["date"], employee) if pout else 0
    record["half_day"] = bool(record["work_hours"] is not None and record["work_hours"] < 4.50)
    return record


def get_employee(code: str):
    employee = db.employees.find_one({"emp_code": code})
    if not employee:
        fail(404, "Employee not found")
    return employee


def serialize_employee(doc: dict) -> dict:
    result = {k: v for k, v in doc.items() if k != "_id"}
    if isinstance(result.get("created_at"), datetime):
        result["created_at"] = epoch_ms(result["created_at"])
    if isinstance(result.get("joined_on"), date):
        result["joined_on"] = result["joined_on"].isoformat()
    return result


def serialize_attendance(doc: dict) -> dict:
    history = []
    for entry in doc.get("history", []):
        item = dict(entry)
        item["at"] = epoch_ms(item.get("at"))
        history.append(item)

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": epoch_ms(doc.get("punch_in")),
        "punch_out": epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history,
    }


def ensure_indexes():
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)])
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)])
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True, name="uq_attendance_employee_date",
    )
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING), ("status", ASCENDING)])
    db.attendance_logs.create_index([("date", ASCENDING), ("status", ASCENDING), ("late_minutes", DESCENDING)])


@app.on_event("startup")
def startup():
    ensure_indexes()


def paging(page: int, page_size: int):
    if page < 1 or not 1 <= page_size <= 100:
        fail(422, "page must be >= 1 and page_size must be between 1 and 100")


# -------------------------------- Health -------------------------------------

@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        fail(503, "MongoDB unavailable")


# -------------------------------- Employees ----------------------------------

@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["joined_on"] = body.joined_on.isoformat()
    doc["created_at"] = datetime.now(UTC).replace(microsecond=0)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        fail(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {"department": department} if department is not None else {}
    total = db.employees.count_documents(query)
    cursor = db.employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING)
    cursor = cursor.skip((page - 1) * page_size).limit(page_size)
    return {
        "items": [serialize_employee(x) for x in cursor],
        "total": total, "page": page, "page_size": page_size,
    }


# ------------------------------- Attendance ----------------------------------

@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    employee = get_employee(body.emp_code)
    punched = to_datetime(body.punched_at) if body.punched_at is not None else datetime.now(UTC).replace(microsecond=0)
    day = attendance_day(punched, employee)
    doc = {
        "emp_code": body.emp_code, "date": day, "status": body.status,
        "punch_in": punched, "punch_out": None, "work_hours": None,
        "late_minutes": late_minutes(punched, day, employee),
        "overtime_minutes": 0, "half_day": False, "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        fail(409, "Attendance already exists for this employee and date")
    return serialize_attendance(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    employee = get_employee(body.emp_code)
    punched = to_datetime(body.punched_at) if body.punched_at is not None else datetime.now(UTC).replace(microsecond=0)

    record = db.attendance_logs.find_one({
        "emp_code": body.emp_code,
        "punch_in": {"$lte": punched, "$ne": None},
        "punch_out": None,
        "status": {"$in": list(PRESENCE)},
    }, sort=[("punch_in", DESCENDING)])

    if record is None:
        closed = db.attendance_logs.find_one({
            "emp_code": body.emp_code,
            "punch_in": {"$lte": punched, "$ne": None},
            "punch_out": {"$ne": None},
        })
        if closed:
            fail(409, "Attendance already punched out")
        fail(404, "No open attendance record found")

    # Normalize both datetimes to the same awareness before comparing.
    # DB stores naive UTC; request datetimes are tz-aware UTC.
    punch_in_cmp = _aware(record["punch_in"])
    punched_cmp = _aware(punched)

    if punched_cmp <= punch_in_cmp:
        fail(422, "punch_out must be after punch_in")
    if punched_cmp > punch_in_cmp + timedelta(hours=24):
        fail(422, "punch_out must be within 24 hours of punch_in")

    # Atomic compare-and-set: only one simultaneous request can close the record.
    result = db.attendance_logs.update_one(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {"punch_out": punched}},
    )
    if result.modified_count != 1:
        fail(409, "Attendance already punched out")

    updated = db.attendance_logs.find_one({"_id": record["_id"]})
    updated = recalculate(updated, employee)
    db.attendance_logs.update_one({"_id": record["_id"]}, {"$set": {
        "work_hours": updated["work_hours"],
        "late_minutes": updated["late_minutes"],
        "overtime_minutes": updated["overtime_minutes"],
        "half_day": updated["half_day"],
    }})
    return serialize_attendance(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from:
        date.fromisoformat(date_from)
    if date_to:
        date.fromisoformat(date_to)
    if date_from and date_to and date_from > date_to:
        fail(422, "date_from must be <= date_to")
    if status is not None and status not in STATUSES:
        fail(422, "Invalid status")

    query = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to
    if status is not None:
        query["status"] = status

    total = db.attendance_logs.count_documents(query)
    cursor = db.attendance_logs.find(query).sort([
        ("date", DESCENDING), ("emp_code", ASCENDING)
    ])
    cursor = cursor.skip((page - 1) * page_size).limit(page_size)
    return {
        "items": [serialize_attendance(x) for x in cursor],
        "total": total, "page": page, "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{attendance_date}")
def regularize_attendance(emp_code: str, attendance_date: str, body: RegularizeIn):
    date.fromisoformat(attendance_date)
    employee = get_employee(emp_code)
    old = db.attendance_logs.find_one({"emp_code": emp_code, "date": attendance_date})
    if not old:
        fail(404, "Attendance record not found")

    supplied = body.model_dump(exclude_unset=True)
    requested = {k: v for k, v in supplied.items() if k not in {"reason", "regularized_by"}}
    if not requested:
        fail(422, "Provide at least one field to change")

    updated = dict(old)
    for key, value in requested.items():
        if key in {"punch_in", "punch_out"}:
            updated[key] = to_datetime(value) if value is not None else None
        else:
            updated[key] = value

    if updated["status"] in {"ABSENT", "LEAVE"}:
        if requested.get("punch_in") is not None or requested.get("punch_out") is not None:
            fail(422, "ABSENT/LEAVE cannot have punch times")
        updated["punch_in"] = updated["punch_out"] = None
    else:
        if updated.get("punch_in") is None:
            fail(422, "Presence status requires punch_in")
        if attendance_day(updated["punch_in"], employee) != attendance_date:
            fail(422, "punch_in does not belong to the attendance date")
        if updated.get("punch_out") is not None:
            pin_cmp = _aware(updated["punch_in"])
            pout_cmp = _aware(updated["punch_out"])
            if pout_cmp <= pin_cmp:
                fail(422, "punch_out must be after punch_in")
            if pout_cmp > pin_cmp + timedelta(hours=24):
                fail(422, "punch_out must be within 24 hours")

    updated = recalculate(updated, employee)
    changes = {}
    for field in requested:
        before, after = old.get(field), updated.get(field)
        if field in {"punch_in", "punch_out"}:
            before, after = epoch_ms(before), epoch_ms(after)
        if before != after:
            changes[field] = {"from": before, "to": after}
    if not changes:
        fail(422, "No effective changes supplied")

    history = {
        "at": datetime.now(UTC).replace(microsecond=0),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }
    result = db.attendance_logs.update_one(
        {"_id": old["_id"], "history": old.get("history", [])},
        {"$set": {
            "status": updated["status"], "punch_in": updated["punch_in"],
            "punch_out": updated["punch_out"], "work_hours": updated["work_hours"],
            "late_minutes": updated["late_minutes"],
            "overtime_minutes": updated["overtime_minutes"],
            "half_day": updated["half_day"],
        }, "$push": {"history": history}},
    )
    if result.modified_count != 1:
        fail(409, "Attendance changed concurrently; retry")
    return serialize_attendance(db.attendance_logs.find_one({"_id": old["_id"]}))


# ------------------------------- Analytics -----------------------------------

def month_bounds(month: str):
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month or ""):
        fail(422, "month must be YYYY-MM")
    start = date.fromisoformat(month + "-01")
    next_month = date(start.year + (start.month == 12),
                      1 if start.month == 12 else start.month + 1, 1)
    return start, next_month - timedelta(days=1)


def weekday_count(start: date, end: date) -> int:
    count = 0
    current = start
    while current <= end:
        count += current.weekday() < 5
        current += timedelta(days=1)
    return count


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    employee = get_employee(emp_code)
    start, end = month_bounds(month)
    joined = date.fromisoformat(employee["joined_on"])
    effective_start = max(start, joined)
    working_days = weekday_count(effective_start, end) if effective_start <= end else 0

    logs = list(db.attendance_logs.find({
        "emp_code": emp_code, "date": {"$gte": start.isoformat(), "$lte": end.isoformat()}
    }, {"_id": 0}))
    present_days = sum(
        (0.5 if x.get("half_day", False) else 1)
        for x in logs
        if x["status"] in PRESENCE
        and date.fromisoformat(x["date"]).weekday() < 5
        and date.fromisoformat(x["date"]) >= effective_start
    )
    leave_days = sum(x["status"] == "LEAVE" for x in logs)
    late = [x for x in logs if x.get("late_minutes", 0) > 0]
    pct = round_half_up(present_days / working_days * 100, 2) if working_days else None
    return {
        "emp_code": emp_code, "month": month, "working_days": working_days,
        "present_days": round_half_up(present_days),
        "leave_days": leave_days, "late_count": len(late),
        "total_late_minutes": sum(x.get("late_minutes", 0) for x in logs),
        "total_overtime_minutes": sum(x.get("overtime_minutes", 0) for x in logs),
        "attendance_pct": pct,
    }


def round_half_up(value, places=2):
    return float(Decimal(str(value)).quantize(
        Decimal("1").scaleb(-places), rounding=ROUND_HALF_UP
    ))


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    start, end = month_bounds(month)
    employees_query = {"joined_on": {"$lte": end.isoformat()}}
    if department:
        employees_query["department"] = department
    employees = list(db.employees.find(employees_query, {"_id": 0, "emp_code": 1, "department": 1}))
    department_codes = {}
    for employee in employees:
        department_codes.setdefault(employee["department"], set()).add(employee["emp_code"])

    logs = list(db.attendance_logs.find({
        "date": {"$gte": start.isoformat(), "$lte": end.isoformat()}
    }, {"_id": 0}))
    results = []
    for dept, codes in department_codes.items():
        dept_logs = [x for x in logs if x["emp_code"] in codes]
        present = sum(
            (0.5 if x.get("half_day", False) else 1)
            for x in dept_logs if x["status"] in PRESENCE
        )
        hours = [x["work_hours"] for x in dept_logs
                 if x["status"] in PRESENCE and x.get("work_hours") is not None]
        results.append({
            "department": dept, "headcount": len(codes),
            "present_days": round_half_up(present),
            "avg_work_hours": round_half_up(sum(hours) / len(hours)) if hours else None,
            "late_count": sum(x.get("late_minutes", 0) > 0 for x in dept_logs),
            "total_late_minutes": sum(x.get("late_minutes", 0) for x in dept_logs),
            "leave_count": sum(x["status"] == "LEAVE" for x in dept_logs),
            "on_duty_count": sum(x["status"] == "ON_DUTY" for x in dept_logs),
        })
    return {"month": month, "items": sorted(results, key=lambda x: x["department"])}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str,
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    start, end = month_bounds(month)
    pipeline = [
        {"$match": {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline.extend([
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {"rank": {"$rank": {}}},
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {
            "_id": 0, "emp_code": "$_id", "name": "$employee.name",
            "department": "$employee.department", "total_late_minutes": 1, "rank": 1,
        }},
    ])
    return {"month": month, "items": list(db.attendance_logs.aggregate(pipeline))}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    start, end = date.fromisoformat(from_date), date.fromisoformat(to_date)
    if end < start or (end - start).days >= 92:
        fail(422, "Invalid date range: to must be >= from and range at most 92 days")
    if not db.employees.find_one({"department": department}):
        fail(404, "Department not found")

    logs = list(db.attendance_logs.find({
        "date": {"$gte": from_date, "$lte": to_date}
    }, {"_id": 0}))
    codes = {
        x["emp_code"] for x in db.employees.find(
            {"department": department}, {"_id": 0, "emp_code": 1}
        )
    }
    by_day = {}
    for log in logs:
        if log["emp_code"] not in codes:
            continue
        row = by_day.setdefault(log["date"], {"present_count": 0.0, "late_count": 0})
        if log["status"] in PRESENCE:
            row["present_count"] += 0.5 if log.get("half_day", False) else 1
        row["late_count"] += log.get("late_minutes", 0) > 0

    rows, rates = [], []
    current = start
    while current <= end:
        key = current.isoformat()
        headcount = db.employees.count_documents({
            "department": department, "joined_on": {"$lte": key}
        })
        data = by_day.get(key, {"present_count": 0, "late_count": 0})
        working = current.weekday() < 5
        rate = round_half_up(data["present_count"] / headcount, 4) if working and headcount else None
        rows.append({
            "date": key, "is_working_day": working, "headcount": headcount,
            "present_count": round_half_up(data["present_count"]),
            "late_count": data["late_count"], "attendance_rate": rate,
        })
        rates.append(rate)
        current += timedelta(days=1)

    for i, row in enumerate(rows):
        window = [v for v in rates[max(0, i - 6):i + 1] if v is not None]
        row["moving_avg_7d"] = round_half_up(sum(window) / len(window), 4) if window else None
    return {"department": department, "from": from_date, "to": to_date, "items": rows}


# Explain endpoint is a baseline implementation; validate each explain query against
# the exact query plan requirements in your assessment before submission.
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
):
    if endpoint not in {
        "attendance_list", "employee_monthly", "department_summary",
        "late_leaderboard", "department_trend"
    }:
        fail(422, "Unsupported endpoint")

    query = {}
    if emp_code:
        query["emp_code"] = emp_code
    if endpoint == "attendance_list":
        if date_from or date_to:
            query["date"] = {}
            if date_from:
                query["date"]["$gte"] = date_from
            if date_to:
                query["date"]["$lte"] = date_to
    elif month:
        start, end = month_bounds(month)
        query["date"] = {"$gte": start.isoformat(), "$lte": end.isoformat()}
    else:
        fail(422, "Required query parameters are missing")

    explain = db.command(
        "explain",
        {"find": "attendance_logs", "filter": query},
        verbosity="executionStats",
    )
    return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}