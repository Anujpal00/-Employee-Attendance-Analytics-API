"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience, but real env vars take priority)

Follows openapi.yaml and DATA_MODEL.md contracts strictly.
"""
import calendar
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import os
import re
from typing import Annotated, Any, Dict, List, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path, status
from pydantic import BaseModel, Field, field_validator
from pymongo import MongoClient
from pymongo.collection import ReturnDocument
from pymongo.errors import DuplicateKeyError

# --------------------------------------------------------------------------- #
# Environment & Database
# --------------------------------------------------------------------------- #
# Load .env file without overriding existing environment variables
load_dotenv(override=False)

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


def init_indexes():
    """Create all required indexes idempotently at startup."""
    # Ensure emp_code_1_date_1 is unique if it pre-existed without unique flag
    try:
        existing = db.attendance_logs.index_information()
        if "emp_code_1_date_1" in existing and not existing["emp_code_1_date_1"].get("unique"):
            db.attendance_logs.drop_index("emp_code_1_date_1")
    except Exception:
        pass

    # employees collection
    db.employees.create_index("emp_code", unique=True)
    db.employees.create_index([("department", 1), ("joined_on", 1)])
    db.employees.create_index("joined_on")

    # attendance_logs collection
    # Natural key uniqueness: at most one record per (emp_code, date)
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    # Covering index for list attendance sorted by date desc, emp_code asc
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    # For punch-out looking up latest punch_in for an employee
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
    # For leaderboard matching date range and positive late_minutes
    db.attendance_logs.create_index([("date", 1), ("late_minutes", 1)])


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan
)


# --------------------------------------------------------------------------- #
# Custom Types & Pydantic Models
# --------------------------------------------------------------------------- #
# Instant in epoch milliseconds: integers only, strict bounds [100000000000, 4102444800000]
EpochMillis = Annotated[
    int,
    Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)
]

PresenceStatusType = Literal["PRESENT", "WFH", "ON_DUTY"]
StatusType = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")

    @field_validator("shift_end")
    @classmethod
    def validate_shift_times(cls, v: str, info) -> str:
        shift_start = info.data.get("shift_start")
        if shift_start is not None and v == shift_start:
            raise ValueError("shift_start must differ from shift_end")
        return v

    @field_validator("joined_on")
    @classmethod
    def validate_joined_date(cls, v: str) -> str:
        try:
            date.fromisoformat(v)
        except ValueError:
            raise ValueError("joined_on must be a valid calendar date")
        return v


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None
    status: PresenceStatusType = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None


class RegularizeIn(BaseModel):
    status: Optional[StatusType] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Helpers & Business Rules (R1 - R10)
# --------------------------------------------------------------------------- #
def round_half_up(val: Optional[float | Decimal], decimals: int = 2) -> Optional[float]:
    """R8: Rounding half-up (not banker's rounding)."""
    if val is None:
        return None
    d = Decimal(str(val))
    q = Decimal("10") ** -decimals
    return float(d.quantize(q, rounding=ROUND_HALF_UP))


def truncate_to_seconds(ms: int) -> int:
    """R1: Instants are truncated to whole seconds."""
    return (ms // 1000) * 1000


def get_current_epoch_ms() -> int:
    """Returns current server time truncated to whole seconds in epoch ms."""
    now_utc = datetime.now(timezone.utc)
    now_sec = int(now_utc.timestamp())
    return now_sec * 1000


def epoch_to_utc_datetime(ms: int) -> datetime:
    """Converts epoch ms to timezone-aware UTC datetime."""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def utc_datetime_to_epoch(dt: Optional[datetime]) -> Optional[int]:
    """Converts datetime to epoch ms."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def determine_attendance_date(punch_in_ms: int, shift_start: str, shift_end: str) -> str:
    """
    R1: Attendance date calculation in IST.
    Overnight shifts: shift_end <= shift_start. A punch-in whose IST time
    is strictly earlier than shift_end belongs to the shift that started the previous day.
    """
    dt_utc = epoch_to_utc_datetime(punch_in_ms)
    dt_ist = dt_utc.astimezone(IST)
    is_overnight = shift_end <= shift_start
    ist_time_str = dt_ist.strftime("%H:%M")

    if is_overnight and ist_time_str < shift_end:
        attendance_date = (dt_ist.date() - timedelta(days=1)).isoformat()
    else:
        attendance_date = dt_ist.date().isoformat()
    return attendance_date


def compute_late_minutes(punch_in_ms: int, attendance_date_str: str, shift_start: str) -> int:
    """
    R2: Grace period 10 minutes.
    Late only if punch-in is strictly more than 10:00 after shift_start.
    late_minutes = floor(minutes since shift_start).
    """
    sh, sm = map(int, shift_start.split(":"))
    shift_start_dt = datetime.fromisoformat(attendance_date_str).replace(
        hour=sh, minute=sm, second=0, microsecond=0, tzinfo=IST
    )
    pi_dt_ist = epoch_to_utc_datetime(punch_in_ms).astimezone(IST)
    diff_sec = (pi_dt_ist - shift_start_dt).total_seconds()
    if diff_sec > 600:
        return int(diff_sec // 60)
    return 0


def compute_work_hours(punch_in_ms: int, punch_out_ms: int) -> float:
    """R4: Work hours = seconds / 3600, rounded 2 decimals half-up."""
    seconds = (punch_out_ms - punch_in_ms) // 1000
    res = (Decimal(int(seconds)) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(res)


def compute_half_day(work_hours: Optional[float]) -> bool:
    """R5: half_day is true when the rounded work_hours < 4.50."""
    if work_hours is None:
        return False
    return work_hours < 4.50


def compute_overtime_minutes(punch_out_ms: int, attendance_date_str: str, shift_start: str, shift_end: str) -> int:
    """
    R3: Whole minutes (floor) from shift_end to punch_out, counted only if >= 30; otherwise 0.
    For overnight shifts, shift_end falls on the calendar day after attendance_date.
    """
    eh, em = map(int, shift_end.split(":"))
    is_overnight = shift_end <= shift_start
    if is_overnight:
        end_date = (datetime.fromisoformat(attendance_date_str) + timedelta(days=1)).date().isoformat()
    else:
        end_date = attendance_date_str

    shift_end_dt = datetime.fromisoformat(end_date).replace(
        hour=eh, minute=em, second=0, microsecond=0, tzinfo=IST
    )
    po_dt_ist = epoch_to_utc_datetime(punch_out_ms).astimezone(IST)
    diff_sec = (po_dt_ist - shift_end_dt).total_seconds()
    minutes = int(diff_sec // 60)
    if minutes >= 30:
        return minutes
    return 0


def format_employee_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Formats an employee document for API response (converts created_at to epoch ms, drops _id)."""
    res = dict(doc)
    res.pop("_id", None)
    res["created_at"] = utc_datetime_to_epoch(res.get("created_at"))
    return res


def format_attendance_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Formats an attendance document for API response (epoch ms, drops _id/id, defaults legacy fields)."""
    res = dict(doc)
    res.pop("_id", None)
    res.pop("id", None)

    res["punch_in"] = utc_datetime_to_epoch(res.get("punch_in"))
    res["punch_out"] = utc_datetime_to_epoch(res.get("punch_out"))
    res["work_hours"] = res.get("work_hours")
    res["late_minutes"] = res.get("late_minutes", 0)
    res["overtime_minutes"] = res.get("overtime_minutes", 0)
    res["half_day"] = res.get("half_day", False)

    history = res.get("history") or []
    formatted_history = []
    for h in history:
        h_entry = dict(h)
        h_entry["at"] = utc_datetime_to_epoch(h_entry.get("at"))
        changes = h_entry.get("changes", {})
        formatted_changes = {}
        for f, chg in changes.items():
            chg_copy = dict(chg)
            if f in ("punch_in", "punch_out"):
                chg_copy["from"] = utc_datetime_to_epoch(chg_copy.get("from"))
                chg_copy["to"] = utc_datetime_to_epoch(chg_copy.get("to"))
            formatted_changes[f] = chg_copy
        h_entry["changes"] = formatted_changes
        formatted_history.append(h_entry)
    res["history"] = formatted_history
    return res


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #

# --- System ---
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database unavailable")


# --- Employees ---
@app.post("/employees", status_code=status.HTTP_201_CREATED)
def create_employee(body: EmployeeIn):
    now_dt = datetime.now(timezone.utc).replace(microsecond=0)
    doc = body.model_dump()
    doc["created_at"] = now_dt

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="emp_code already exists")

    return format_employee_doc(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = Query(None, description="Exact, case-sensitive match"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    q = {}
    if department is not None:
        q["department"] = department

    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    cursor = db.employees.find(q, {"_id": 0}).sort("emp_code", 1).skip(skip).limit(page_size)
    items = [format_employee_doc(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --- Attendance ---
@app.post("/attendance/punch-in", status_code=status.HTTP_201_CREATED)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    raw_ms = body.punched_at if body.punched_at is not None else get_current_epoch_ms()
    punch_in_ms = truncate_to_seconds(raw_ms)
    punch_in_dt_utc = epoch_to_utc_datetime(punch_in_ms)

    att_date = determine_attendance_date(punch_in_ms, emp["shift_start"], emp["shift_end"])
    late_min = compute_late_minutes(punch_in_ms, att_date, emp["shift_start"])

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": punch_in_dt_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_min,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="already punched in for this date")

    return format_attendance_doc(doc)


@app.post("/attendance/punch-out", status_code=status.HTTP_200_OK)
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    raw_ms = body.punched_at if body.punched_at is not None else get_current_epoch_ms()
    punch_out_ms = truncate_to_seconds(raw_ms)
    punch_out_dt_utc = epoch_to_utc_datetime(punch_out_ms)

    # Find most recent record whose punch_in is at or before punched_at
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_dt_utc}},
        sort=[("punch_in", -1)]
    )
    if not rec:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no punch-in found")

    if rec.get("punch_out") is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="that record is already punched out")

    punch_in_ms = utc_datetime_to_epoch(rec["punch_in"])
    if punch_out_ms <= punch_in_ms:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="punched_at must be strictly after punch_in")

    if (punch_out_ms - punch_in_ms) > 24 * 3600 * 1000:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="punched_at cannot be more than 24 hours after punch_in")

    # Compute derived fields (R3, R4, R5)
    work_hours = compute_work_hours(punch_in_ms, punch_out_ms)
    half_day = compute_half_day(work_hours)
    overtime_min = compute_overtime_minutes(punch_out_ms, rec["date"], emp["shift_start"], emp["shift_end"])

    # Atomic update for race safety: ensures exactly one punch-out succeeds
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_dt_utc,
                "work_hours": work_hours,
                "overtime_minutes": overtime_min,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER
    )
    if not updated:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="that record is already punched out")

    return format_attendance_doc(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    status: Optional[StatusType] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="date_from must not exceed date_to")

    q = {}
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

    skip = (page - 1) * page_size
    total = db.attendance_logs.count_documents(q)
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", -1), ("emp_code", 1)])
        .skip(skip)
        .limit(page_size)
    )
    items = [format_attendance_doc(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}", status_code=status.HTTP_200_OK)
def regularize_attendance(
    emp_code: str = Path(...),
    date: str = Path(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    body: RegularizeIn = ...
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not rec:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No attendance record found for date")

    # Business rule: setting status ABSENT or LEAVE clears both punch times
    if body.status in ("ABSENT", "LEAVE") and (body.punch_in is not None or body.punch_out is not None):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="ABSENT or LEAVE cannot have punch times")

    target_status = body.status if body.status is not None else rec["status"]

    if target_status in ("ABSENT", "LEAVE"):
        new_punch_in_ms = None
        new_punch_out_ms = None
        new_punch_in_dt = None
        new_punch_out_dt = None
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        # Presence status requires punch_in
        if body.punch_in is not None:
            new_punch_in_ms = truncate_to_seconds(body.punch_in)
        else:
            new_punch_in_ms = utc_datetime_to_epoch(rec.get("punch_in"))

        if new_punch_in_ms is None:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Presence status requires a punch_in")

        # Verify punch_in stays on record's attendance date under R1
        att_date = determine_attendance_date(new_punch_in_ms, emp["shift_start"], emp["shift_end"])
        if att_date != date:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="punch_in must stay on record's attendance date")

        new_punch_in_dt = epoch_to_utc_datetime(new_punch_in_ms)
        new_late_minutes = compute_late_minutes(new_punch_in_ms, date, emp["shift_start"])

        # punch_out
        if body.punch_out is not None:
            new_punch_out_ms = truncate_to_seconds(body.punch_out)
        else:
            new_punch_out_ms = utc_datetime_to_epoch(rec.get("punch_out"))

        if new_punch_out_ms is not None:
            if new_punch_out_ms <= new_punch_in_ms:
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="punch_out must be after punch_in")
            if (new_punch_out_ms - new_punch_in_ms) > 24 * 3600 * 1000:
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="punch_out must be within 24 hours of punch_in")

            new_punch_out_dt = epoch_to_utc_datetime(new_punch_out_ms)
            new_work_hours = compute_work_hours(new_punch_in_ms, new_punch_out_ms)
            new_half_day = compute_half_day(new_work_hours)
            new_overtime_minutes = compute_overtime_minutes(new_punch_out_ms, date, emp["shift_start"], emp["shift_end"])
        else:
            new_punch_out_dt = None
            new_work_hours = None
            new_half_day = False
            new_overtime_minutes = 0

    # Old values
    old_punch_in_ms = utc_datetime_to_epoch(rec.get("punch_in"))
    old_punch_out_ms = utc_datetime_to_epoch(rec.get("punch_out"))
    old_status = rec.get("status")
    old_work_hours = rec.get("work_hours")
    old_late_minutes = rec.get("late_minutes", 0)
    old_overtime_minutes = rec.get("overtime_minutes", 0)
    old_half_day = rec.get("half_day", False)

    # Build changes dict (storing BSON datetimes in DB per DATA_MODEL.md)
    changes = {}
    if old_status != target_status:
        changes["status"] = {"from": old_status, "to": target_status}
    if old_punch_in_ms != new_punch_in_ms:
        changes["punch_in"] = {"from": rec.get("punch_in"), "to": new_punch_in_dt}
    if old_punch_out_ms != new_punch_out_ms:
        changes["punch_out"] = {"from": rec.get("punch_out"), "to": new_punch_out_dt}
    if old_work_hours != new_work_hours:
        changes["work_hours"] = {"from": old_work_hours, "to": new_work_hours}
    if old_late_minutes != new_late_minutes:
        changes["late_minutes"] = {"from": old_late_minutes, "to": new_late_minutes}
    if old_overtime_minutes != new_overtime_minutes:
        changes["overtime_minutes"] = {"from": old_overtime_minutes, "to": new_overtime_minutes}
    if old_half_day != new_half_day:
        changes["half_day"] = {"from": old_half_day, "to": new_half_day}

    if not changes:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="A request that changes nothing is 422")

    now_dt = datetime.now(timezone.utc).replace(microsecond=0)
    hist_entry = {
        "at": now_dt,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    current_hist_len = len(rec.get("history") or [])
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": rec["_id"],
            f"history.{current_hist_len}": {"$exists": False}
        },
        {
            "$set": {
                "status": target_status,
                "punch_in": new_punch_in_dt,
                "punch_out": new_punch_out_dt,
                "work_hours": new_work_hours,
                "late_minutes": new_late_minutes,
                "overtime_minutes": new_overtime_minutes,
                "half_day": new_half_day,
            },
            "$push": {"history": hist_entry}
        },
        return_document=ReturnDocument.AFTER
    )
    if not updated:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Concurrent regularizations conflict")

    return format_attendance_doc(updated)


# --- Analytics Pipelines & Endpoints ---
def build_employee_monthly_pipeline(emp_code: str, month: str) -> List[Dict[str, Any]]:
    y, m = map(int, month.split("-"))
    last_day = calendar.monthrange(y, m)[1]
    m_start = f"{month}-01"
    m_end = f"{month}-{last_day:02d}"

    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": m_start, "$lte": m_end}
            }
        },
        {
            "$addFields": {
                "iso_day": {"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                "is_present_status": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    "$is_present_status",
                                    {"$gte": ["$iso_day", 1]},
                                    {"$lte": ["$iso_day", 5]}
                                ]
                            },
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {
                        "$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                }
            }
        }
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    y, m = map(int, month.split("-"))
    last_day = calendar.monthrange(y, m)[1]
    m_start_date = date(y, m, 1)
    m_end_date = date(y, m, last_day)

    joined_date = date.fromisoformat(emp["joined_on"])
    if joined_date > m_end_date:
        working_days = 0
    else:
        eff_start = max(m_start_date, joined_date)
        working_days = sum(
            1 for day_idx in range((m_end_date - eff_start).days + 1)
            if (eff_start + timedelta(days=day_idx)).weekday() < 5
        )

    pipeline = build_employee_monthly_pipeline(emp_code, month)
    agg_res = list(db.attendance_logs.aggregate(pipeline))

    if agg_res:
        stats = agg_res[0]
        present_days = float(stats["present_days"])
        leave_days = int(stats["leave_days"])
        late_count = int(stats["late_count"])
        total_late_min = int(stats["total_late_minutes"])
        total_ot_min = int(stats["total_overtime_minutes"])
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_min = 0
        total_ot_min = 0

    if working_days > 0:
        att_pct_raw = (Decimal(str(present_days)) / Decimal(working_days)) * Decimal(100)
        attendance_pct = round_half_up(att_pct_raw, 2)
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_min,
        "total_overtime_minutes": total_ot_min,
        "attendance_pct": attendance_pct,
    }


def build_department_summary_pipeline(month: str, department: Optional[str] = None) -> List[Dict[str, Any]]:
    y, m = map(int, month.split("-"))
    last_day = calendar.monthrange(y, m)[1]
    m_start = f"{month}-01"
    m_end = f"{month}-{last_day:02d}"

    match_stage = {"joined_on": {"$lte": m_end}}
    if department:
        match_stage["department"] = department

    return [
        {"$match": match_stage},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", m_start]},
                                    {"$lte": ["$date", m_end]}
                                ]
                            }
                        }
                    }
                ],
                "as": "logs"
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$addFields": {
                "dayOfWeek": {
                    "$cond": [
                        {"$ifNull": ["$logs.date", False]},
                        {"$isoDayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}},
                        0
                    ]
                },
                "is_present_status": {
                    "$cond": [
                        {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                        True,
                        False
                    ]
                }
            }
        },
        {
            "$group": {
                "_id": "$department",
                "emp_codes": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    "$is_present_status",
                                    {"$gte": ["$dayOfWeek", 1]},
                                    {"$lte": ["$dayOfWeek", 5]}
                                ]
                            },
                            {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1.0]},
                            0.0
                        ]
                    }
                },
                "work_hours_list": {
                    "$push": {
                        "$cond": [
                            {
                                "$and": [
                                    "$is_present_status",
                                    {"$ne": ["$logs.work_hours", None]}
                                ]
                            },
                            "$logs.work_hours",
                            "$$REMOVE"
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]},
                            1,
                            0
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$logs.late_minutes", 0]}
                },
                "leave_count": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$logs.status", "LEAVE"]},
                            1,
                            0
                        ]
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$logs.status", "ON_DUTY"]},
                            1,
                            0
                        ]
                    }
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": {"$size": "$emp_codes"},
                "present_days": "$present_days",
                "work_hours_list": 1,
                "late_count": "$late_count",
                "total_late_minutes": "$total_late_minutes",
                "leave_count": "$leave_count",
                "on_duty_count": "$on_duty_count"
            }
        },
        {"$sort": {"department": 1}}
    ]


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None)
):
    pipeline = build_department_summary_pipeline(month, department)
    docs = list(db.employees.aggregate(pipeline))
    items = []
    for d in docs:
        wh_list = d.pop("work_hours_list", [])
        if wh_list:
            avg_wh_raw = Decimal(str(sum(wh_list))) / Decimal(len(wh_list))
            d["avg_work_hours"] = round_half_up(avg_wh_raw, 2)
        else:
            d["avg_work_hours"] = None
        d["present_days"] = float(d["present_days"])
        items.append(d)

    return {"month": month, "items": items}


def build_late_leaderboard_pipeline(month: str, limit: int = 10, department: Optional[str] = None) -> List[Dict[str, Any]]:
    y, m = map(int, month.split("-"))
    last_day = calendar.monthrange(y, m)[1]
    m_start = f"{month}-01"
    m_end = f"{month}-{last_day:02d}"

    pipeline: List[Dict[str, Any]] = [
        {
            "$match": {
                "date": {"$gte": m_start, "$lte": m_end},
                "late_minutes": {"$gt": 0}
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp"
            }
        },
        {"$unwind": "$emp"}
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    # Standard competition ranking: ties share rank, next rank skipped (1, 2, 2, 4)
    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                }
            }
        },
        # limit applies after ranking
        {"$match": {"rank": {"$lte": limit}}},
        # Tied rows ordered by emp_code ascending
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1
            }
        }
    ])
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = Query(None)
):
    pipeline = build_late_leaderboard_pipeline(month, limit, department)
    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


def build_department_trend_pipeline(department: str, from_date: str, to_date: str) -> List[Dict[str, Any]]:
    from_dt = datetime.fromisoformat(from_date)
    to_dt = datetime.fromisoformat(to_date)
    bounds_end = to_dt + timedelta(days=1)

    return [
        {"$match": {"date": {"$gte": from_date, "$lte": to_date}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp"
            }
        },
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0
                        ]
                    }
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "date_str": "$_id",
                "date_obj": {"$dateFromString": {"dateString": "$_id"}},
                "present_count": 1,
                "late_count": 1
            }
        },
        # Gap-filling stage via $densify across date range
        {
            "$densify": {
                "field": "date_obj",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, bounds_end]
                }
            }
        },
        {
            "$addFields": {
                "date": {
                    "$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": "$date_obj"
                    }
                },
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]}
            }
        },
        # Lookup department employees for headcount calculation per day (R9)
        {
            "$lookup": {
                "from": "employees",
                "pipeline": [
                    {"$match": {"department": department}},
                    {"$project": {"_id": 0, "emp_code": 1, "joined_on": 1}}
                ],
                "as": "dept_employees"
            }
        },
        {
            "$addFields": {
                "iso_day": {"$isoDayOfWeek": "$date_obj"},
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": "$dept_employees",
                            "as": "e",
                            "cond": {"$lte": ["$$e.joined_on", "$date"]}
                        }
                    }
                }
            }
        },
        {
            "$addFields": {
                "is_working_day": {
                    "$cond": [
                        {"$and": [{"$gte": ["$iso_day", 1]}, {"$lte": ["$iso_day", 5]}]},
                        True,
                        False
                    ]
                }
            }
        },
        # attendance_rate: only on working days with headcount > 0
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]}
                            ]
                        },
                        {
                            "$round": [
                                {"$divide": ["$present_count", "$headcount"]},
                                4
                            ]
                        },
                        None
                    ]
                }
            }
        },
        # 7-day moving average over preceding window in the requested range
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d_raw": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, "current"]
                        }
                    }
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": {
                    "$cond": [
                        {"$ne": ["$moving_avg_7d_raw", None]},
                        {"$round": ["$moving_avg_7d_raw", 4]},
                        None
                    ]
                }
            }
        },
        {"$sort": {"date": 1}}
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")
):
    dept_exists = db.employees.find_one({"department": department})
    if not dept_exists:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown department")

    d_from = date.fromisoformat(from_date)
    d_to = date.fromisoformat(to_date)
    if d_to < d_from:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="to date cannot be earlier than from date")

    day_span = (d_to - d_from).days + 1
    if day_span > 92:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Date range cannot exceed 92 days")

    pipeline = build_department_trend_pipeline(department, from_date, to_date)
    items = list(db.attendance_logs.aggregate(pipeline))
    return {"department": department, "items": items}


# --- Admin Explain ---
ExplainEndpointType = Literal[
    "attendance_list",
    "employee_monthly",
    "department_summary",
    "late_leaderboard",
    "department_trend"
]


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: ExplainEndpointType = Path(...),
    emp_code: Optional[str] = Query(None),
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None),
    limit: Optional[int] = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    status_val: Optional[StatusType] = Query(None, alias="status"),
    from_date: Optional[str] = Query(None, alias="from", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    to_date: Optional[str] = Query(None, alias="to", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    page: Optional[int] = Query(1, ge=1),
    page_size: Optional[int] = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        collection = "attendance_logs"
        q = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status_val:
            q["status"] = status_val
        skip = ((page or 1) - 1) * (page_size or 20)
        command_dict = {
            "find": "attendance_logs",
            "filter": q,
            "sort": {"date": -1, "emp_code": 1},
            "skip": skip,
            "limit": page_size or 20
        }
        raw_explain = db.command("explain", command_dict, verbosity="executionStats")

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="emp_code and month are required")
        collection = "attendance_logs"
        pipeline = build_employee_monthly_pipeline(emp_code, month)
        raw_explain = db.command("explain", {"aggregate": collection, "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="month is required")
        collection = "employees"
        pipeline = build_department_summary_pipeline(month, department)
        raw_explain = db.command("explain", {"aggregate": collection, "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="month is required")
        collection = "attendance_logs"
        pipeline = build_late_leaderboard_pipeline(month, limit or 10, department)
        raw_explain = db.command("explain", {"aggregate": collection, "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="department, from, and to are required")
        collection = "attendance_logs"
        pipeline = build_department_trend_pipeline(department, from_date, to_date)
        raw_explain = db.command("explain", {"aggregate": collection, "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")

    else:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid endpoint")

    clean_explain = json_util.loads(json_util.dumps(raw_explain))
    return {
        "endpoint": endpoint,
        "collection": collection,
        "explain": clean_explain
    }
