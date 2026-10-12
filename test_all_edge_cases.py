from datetime import datetime, timezone, timedelta
from fastapi.testclient import TestClient
import subprocess

from app.main import app, db, init_indexes

client = TestClient(app)
IST = timezone(timedelta(hours=5, minutes=30))

def setup_module():
    subprocess.run(["python", "sample_seed.py"], check=True)
    init_indexes()


def test_overnight_shift_punch_in_and_date_rule_r1():
    # EMP0005 has shift 22:00 - 06:00 (overnight shift)
    # 1. Punch in at 2026-07-20 21:50 IST:
    # Time is >= 06:00, so date is 2026-07-20
    dt1 = datetime(2026, 7, 20, 21, 50, tzinfo=IST)
    ms1 = int(dt1.timestamp() * 1000)
    res1 = client.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": ms1})
    assert res1.status_code == 201
    assert res1.json()["date"] == "2026-07-20"
    assert res1.json()["late_minutes"] == 0

    # 2. Punch in at 2026-07-22 01:30 IST:
    # Time 01:30 is earlier than shift_end (06:00), so date belongs to previous day: 2026-07-21
    # Shift start is 2026-07-21 22:00 IST.
    # Punch in is 2026-07-22 01:30 IST.
    # Elapsed time = 3 hours 30 min = 210 minutes late!
    dt2 = datetime(2026, 7, 22, 1, 30, tzinfo=IST)
    ms2 = int(dt2.timestamp() * 1000)
    res2 = client.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": ms2})
    assert res2.status_code == 201
    assert res2.json()["date"] == "2026-07-21"
    assert res2.json()["late_minutes"] == 210


def test_grace_period_rule_r2_boundary_cases():
    # EMP0001 has shift_start 09:30
    # Case A: exactly 09:40:00 IST -> inside grace period (elapsed <= 10 min) -> late_minutes = 0
    dt_a = datetime(2026, 8, 3, 9, 40, 0, tzinfo=IST)
    ms_a = int(dt_a.timestamp() * 1000)
    res_a = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms_a})
    assert res_a.status_code == 201
    assert res_a.json()["late_minutes"] == 0

    # Case B: 09:40:01 IST -> strictly > 10 min -> late_minutes = floor(601/60) = 10
    dt_b = datetime(2026, 8, 4, 9, 40, 1, tzinfo=IST)
    ms_b = int(dt_b.timestamp() * 1000)
    res_b = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms_b})
    assert res_b.status_code == 201
    assert res_b.json()["late_minutes"] == 10

    # Case C: 10:15:59 IST -> strictly > 10 min -> late_minutes = floor(2759/60) = 45
    dt_c = datetime(2026, 8, 5, 10, 15, 59, tzinfo=IST)
    ms_c = int(dt_c.timestamp() * 1000)
    res_c = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms_c})
    assert res_c.status_code == 201
    assert res_c.json()["late_minutes"] == 45


def test_overtime_and_work_hours_half_up_rounding():
    # EMP0001 shift 09:30 - 18:30
    # Punch in 09:30:00 IST on 2026-08-06
    dt_in = datetime(2026, 8, 6, 9, 30, 0, tzinfo=IST)
    ms_in = int(dt_in.timestamp() * 1000)
    res_in = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms_in})
    assert res_in.status_code == 201

    # Punch out at 19:05:00 IST (35 min overtime >= 30 threshold)
    # Duration: 9 hours 35 min = 34500 seconds
    # 34500 / 3600 = 9.583333... -> 9.58
    dt_out = datetime(2026, 8, 6, 19, 5, 0, tzinfo=IST)
    ms_out = int(dt_out.timestamp() * 1000)
    res_out = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms_out})
    assert res_out.status_code == 200
    assert res_out.json()["work_hours"] == 9.58
    assert res_out.json()["overtime_minutes"] == 35
    assert res_out.json()["half_day"] is False


def test_mid_month_joiner_working_days_r7():
    # EMP0002 joined on 2026-07-13 (Monday)
    # Month: July 2026 has 23 working days total.
    # Working days from July 13 to July 31:
    # Week of July 13-17: 5 days
    # Week of July 20-24: 5 days
    # Week of July 27-31: 5 days
    # Total = 15 working days!
    res = client.get("/analytics/employees/EMP0002/monthly?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["working_days"] == 15


def test_explain_validation_error():
    # Missing required query params should return 422
    res = client.get("/admin/explain/employee_monthly")
    assert res.status_code == 422

    res2 = client.get("/admin/explain/department_trend")
    assert res2.status_code == 422
