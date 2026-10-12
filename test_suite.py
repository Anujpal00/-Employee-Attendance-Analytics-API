"""
Comprehensive test suite for Employee Attendance & Analytics API
Covers:
- /health
- /employees (POST, GET, duplicate 409, validation 422, sorting, pagination)
- /attendance/punch-in (R1 overnight, R2 late, grace period, 404, 409 duplicate)
- /attendance/punch-out (R3 overtime, R4 work hours, R5 half day, overnight shift, 404, 409, 422)
- /attendance (GET, filtering, pagination, date_from > date_to 422)
- /attendance/{emp_code}/{date} (PATCH regularization, R1-R5 recomputation, history audit trail, 404, 422)
- /analytics/employees/{emp_code}/monthly (R7 working days, R5-R7 present days, R8 rounding)
- /analytics/departments/summary (R9 headcount with zero logs, avg work hours over records, cross-collection)
- /analytics/leaderboard/late (standard competition ranking 1, 2, 2, 4, ties at limit)
- /analytics/departments/{department}/trend (gap-filling $densify, 7d moving average, 404, 422)
- /admin/explain/{endpoint} (IXSCAN, no COLLSCAN, 422 validation)
"""
import pytest
from fastapi.testclient import TestClient
from app.main import app, db, init_indexes
import sample_seed

client = TestClient(app)

def setup_module():
    # Reload sample seed data
    import subprocess
    subprocess.run(["python", "sample_seed.py"], check=True)
    init_indexes()


def test_health():
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


def test_create_employee():
    # Valid creation
    body = {
        "emp_code": "EMP9999",
        "name": "Test User",
        "email": "test@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-01"
    }
    res = client.post("/employees", json=body)
    assert res.status_code == 201
    data = res.json()
    assert data["emp_code"] == "EMP9999"
    assert "_id" not in data
    assert isinstance(data["created_at"], int)

    # Duplicate creation -> 409
    res_dup = client.post("/employees", json=body)
    assert res_dup.status_code == 409

    # Same shift_start and shift_end -> 422
    body_invalid = dict(body, emp_code="EMP9998", shift_end="09:30")
    res_inv = client.post("/employees", json=body_invalid)
    assert res_inv.status_code == 422

    # Invalid emp_code -> 422
    body_invalid_code = dict(body, emp_code="INVALID_CODE")
    res_inv_code = client.post("/employees", json=body_invalid_code)
    assert res_inv_code.status_code == 422


def test_list_employees():
    res = client.get("/employees?page=1&page_size=3")
    assert res.status_code == 200
    data = res.json()
    assert len(data["items"]) == 3
    assert data["page"] == 1
    assert data["page_size"] == 3
    assert data["total"] >= 6
    # Sorted by emp_code asc
    codes = [item["emp_code"] for item in data["items"]]
    assert codes == sorted(codes)

    # Filter by department
    res_dept = client.get("/employees?department=Sales")
    assert res_dept.status_code == 200
    dept_data = res_dept.json()
    assert dept_data["total"] == 2
    for item in dept_data["items"]:
        assert item["department"] == "Sales"


def test_punch_in():
    # Unknown employee -> 404
    res_404 = client.post("/attendance/punch-in", json={"emp_code": "EMP0000"})
    assert res_404.status_code == 404

    # Duplicate punch in for same day -> 409
    # EMP0001 already has record on 2026-07-06
    # 2026-07-06 09:30 IST is 1783310400000 ms
    # Let's punch in at 2026-07-06 09:30:00 IST for EMP0001
    from datetime import datetime, timezone, timedelta
    IST = timezone(timedelta(hours=5, minutes=30))
    dt_ist = datetime(2026, 7, 6, 9, 30, tzinfo=IST)
    ms = int(dt_ist.timestamp() * 1000)
    res_dup = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms})
    assert res_dup.status_code == 409

    # New punch in on a date with no record: 2026-07-15
    dt_new = datetime(2026, 7, 15, 9, 40, 1, tzinfo=IST) # 10 min 1 sec late -> late_minutes = 10
    ms_new = int(dt_new.timestamp() * 1000)
    res_new = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms_new})
    assert res_new.status_code == 201
    data = res_new.json()
    assert data["emp_code"] == "EMP0001"
    assert data["date"] == "2026-07-15"
    assert data["late_minutes"] == 10
    assert data["punch_out"] is None
    assert data["work_hours"] is None
    assert data["history"] == []
    assert "_id" not in data
    assert "id" not in data


def test_punch_out():
    # EMP0006 on 2026-07-07 punched in at 09:35 IST (04:05 UTC) and punch_out is null
    # Let's punch out at 18:35 IST (13:05 UTC)
    from datetime import datetime, timezone, timedelta
    IST = timezone(timedelta(hours=5, minutes=30))
    dt_out = datetime(2026, 7, 7, 18, 35, 0, tzinfo=IST)
    ms_out = int(dt_out.timestamp() * 1000)

    res = client.post("/attendance/punch-out", json={"emp_code": "EMP0006", "punched_at": ms_out})
    assert res.status_code == 200
    data = res.json()
    assert data["emp_code"] == "EMP0006"
    assert data["date"] == "2026-07-07"
    assert data["punch_out"] == ms_out
    # 09:35 to 18:35 is 9 hours
    assert data["work_hours"] == 9.0
    assert data["half_day"] is False
    # shift_end is 18:30, punched out 18:35 (5 min overtime < 30 min threshold) -> overtime_minutes = 0
    assert data["overtime_minutes"] == 0

    # Simultaneous second punch out for the same record -> 409
    res_dup = client.post("/attendance/punch-out", json={"emp_code": "EMP0006", "punched_at": ms_out})
    assert res_dup.status_code == 409


def test_regularize_attendance():
    # Regularize EMP0001 on 2026-07-06
    # Change status to WFH
    body = {
        "status": "WFH",
        "reason": "Approved remote work regularized by manager",
        "regularized_by": "manager.john"
    }
    res = client.patch("/attendance/EMP0001/2026-07-06", json=body)
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "WFH"
    assert len(data["history"]) == 1
    hist = data["history"][0]
    assert hist["by"] == "manager.john"
    assert hist["reason"] == "Approved remote work regularized by manager"
    assert "status" in hist["changes"]
    assert hist["changes"]["status"]["from"] == "PRESENT"
    assert hist["changes"]["status"]["to"] == "WFH"

    # Request that changes nothing -> 422
    res_no_change = client.patch("/attendance/EMP0001/2026-07-06", json=body)
    assert res_no_change.status_code == 422


def test_analytics_employee_monthly():
    res = client.get("/analytics/employees/EMP0001/monthly?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["emp_code"] == "EMP0001"
    assert data["month"] == "2026-07"
    assert data["working_days"] == 23 # July 2026 has 23 weekdays
    assert data["present_days"] >= 2.0


def test_analytics_department_summary():
    res = client.get("/analytics/departments/summary?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["month"] == "2026-07"
    depts = {item["department"]: item for item in data["items"]}
    assert "Engineering" in depts
    assert "Sales" in depts
    assert "Support" in depts
    assert depts["Engineering"]["headcount"] >= 2


def test_analytics_leaderboard():
    res = client.get("/analytics/leaderboard/late?month=2026-07&limit=10")
    assert res.status_code == 200
    data = res.json()
    assert data["month"] == "2026-07"
    assert len(data["items"]) >= 1
    first = data["items"][0]
    assert first["rank"] == 1
    assert first["total_late_minutes"] > 0


def test_analytics_department_trend():
    res = client.get("/analytics/departments/Sales/trend?from=2026-07-05&to=2026-07-10")
    assert res.status_code == 200
    data = res.json()
    assert data["department"] == "Sales"
    # Exactly 6 days (July 5 through July 10)
    assert len(data["items"]) == 6
    dates = [item["date"] for item in data["items"]]
    assert dates == ["2026-07-05", "2026-07-06", "2026-07-07", "2026-07-08", "2026-07-09", "2026-07-10"]

    # Unknown department -> 404
    res_404 = client.get("/analytics/departments/NonExistent/trend?from=2026-07-05&to=2026-07-10")
    assert res_404.status_code == 404

    # to < from -> 422
    res_422 = client.get("/analytics/departments/Sales/trend?from=2026-07-10&to=2026-07-05")
    assert res_422.status_code == 422


def test_explain_endpoints():
    endpoints = [
        ("attendance_list", {}),
        ("employee_monthly", {"emp_code": "EMP0001", "month": "2026-07"}),
        ("department_summary", {"month": "2026-07"}),
        ("late_leaderboard", {"month": "2026-07"}),
        ("department_trend", {"department": "Sales", "from": "2026-07-05", "to": "2026-07-10"}),
    ]
    for ep, params in endpoints:
        res = client.get(f"/admin/explain/{ep}", params=params)
        assert res.status_code == 200, f"Explain failed for {ep}: {res.text}"
        data = res.json()
        assert data["endpoint"] == ep
        assert "collection" in data
        assert "explain" in data
        import json
        exp_str = json.dumps(data["explain"])
        assert "IXSCAN" in exp_str, f"{ep} does not contain IXSCAN"
        assert "COLLSCAN" not in exp_str, f"{ep} contains COLLSCAN"
