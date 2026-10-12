# Employee Attendance & Analytics API (FastAPI + MongoDB)

Production-ready backend for an HR attendance management and analytics service built with **FastAPI** and **MongoDB 7.0**, strictly adhering to the contract defined in `openapi.yaml` and the stored data specifications in `DATA_MODEL.md`.

---

## 🚀 Quickstart

### 1. Requirements & Setup
- Python 3.11+
- MongoDB 6.0+ (Tested against MongoDB 7.0.12)

```bash
# Create and activate virtual environment
python -m venv .venv
# On Windows PowerShell:
.venv\Scripts\Activate.ps1
# On Linux/macOS:
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### 2. Environment Configuration
The service reads connection parameters from environment variables (with process environment variables taking priority over any local `.env`):
- `MONGO_URI`: MongoDB connection string (default: `mongodb://localhost:27017`)
- `MONGO_DB`: Target database name (default: `attendance_db`)

### 3. Load Sample Data & Start Application
```bash
# Seed the initial sample documents (optional, for local testing)
python sample_seed.py

# Start the server (readiness probe /health ready in < 1 second)
uvicorn app.main:app --port 8000
```

---

## 🛠️ Architecture & Key Highlights

1. **Automatic, Idempotent Index Initialization**
   - Indexes are created automatically during the application startup lifespan.
   - Unique constraints on `employees.emp_code` and `attendance_logs.(emp_code, date)` guarantee zero race conditions under concurrent creates and punches.
   - Covering indexes on `attendance_logs.(date, emp_code)` ensure paginated listings and analytics execute via index scans (`IXSCAN`) without collection scans (`COLLSCAN`).

2. **Strict Rule & Boundary Adherence (R1 – R10)**
   - **R1 (IST & Overnight shifts):** Automatically associates overnight punches (IST time < `shift_end`) to the shift that started on the previous day. All timestamps are truncated to whole seconds before calculation and boundary conversion.
   - **R2 (Late calculation):** Grace period is strictly 10:00 minutes. Lateness of 10m 01s evaluates to 10 minutes (floor since shift start).
   - **R3 (Overtime):** 30-minute threshold enforced; respects next-day shift end for overnight shifts.
   - **R4 & R8 (Exact rounding):** Employs `decimal.Decimal` with `ROUND_HALF_UP` arithmetic, avoiding Python's default banker's rounding discrepancies.
   - **R5 (Half day):** Automatically flagged when work hours < 4.50; counted as 0.5 of a present day.
   - **R7 (Working days):** Accurately computed from `joined_on` for mid-month joiners (Mon–Fri only).

3. **High-Performance MongoDB Aggregations**
   - **Department Summary:** Cross-collection aggregation accounting for employees with zero logs in headcount (R9).
   - **Late Leaderboard:** Standard competition ranking (1, 2, 2, 4) using `$setWindowFields` and `$rank`, applying limits post-ranking so ties at the cutoff are preserved.
   - **Department Trend:** Gap-filling series using `$densify` for every single calendar day, with 7-day moving averages calculated over non-null rates.
   - **Explain Endpoint (`/admin/explain/{endpoint}`):** Provides raw `executionStats` confirming `IXSCAN` plans across all core queries and aggregations.

---

## 🧪 Testing

Run the full automated test suites covering all endpoints, edge cases, and explain plans:
```bash
pytest test_suite.py test_all_edge_cases.py -v
```

---

## 📚 Deliverables Summary

- `app/main.py`: All application code consolidated in this single file.
- `REVIEW.md`: Detailed audit table of all starter defects and their resolutions (Part A).
- `DECISIONS.md`: Concise answers to the five design and architecture questions (Part C).
- `CHANGES_EXPLAINED.md`: Comprehensive side-by-side comparison of prior code vs current implementation.
- `requirements.txt`: Python package dependencies.
