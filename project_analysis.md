# HROne Engineering Assignment — Complete Analysis

## 🎯 What This Is

A **Software Engineer hiring assignment** for HROne. You must build the backend of an HR attendance system using **FastAPI + MongoDB**. Employees punch in/out, managers correct records, and analytics endpoints answer business questions.

**Guide time:** 4–5 hours | **Stack:** Python 3.11+, FastAPI, PyMongo, MongoDB 6.0+

---

## 📁 Folder Structure & What Each File Does

| File/Folder | Purpose | Status |
|---|---|---|
| [PROBLEM_STATEMENT.docx](file:///c:/Users/kkart/Downloads/SDT_candidate_kit/candidate_kit/PROBLEM_STATEMENT.docx) | The full assignment spec | ✅ Read-only reference |
| [openapi.yaml](file:///c:/Users/kkart/Downloads/SDT_candidate_kit/candidate_kit/openapi.yaml) | **The contract** — 650 lines defining every endpoint, parameter, status code, response schema, and business rules R1–R10 | ✅ Read-only reference |
| [DATA_MODEL.md](file:///c:/Users/kkart/Downloads/SDT_candidate_kit/candidate_kit/DATA_MODEL.md) | MongoDB collections, field types, 9 annotated sample documents | ✅ Read-only reference |
| [app/main.py](file:///c:/Users/kkart/Downloads/SDT_candidate_kit/candidate_kit/app/main.py) | **Starter code** — buggy, half-finished; needs review + fixing (Part A) and completion (Part B) | ⚠️ Needs heavy work |
| [app/\_\_init\_\_.py](file:///c:/Users/kkart/Downloads/SDT_candidate_kit/candidate_kit/app/__init__.py) | Empty package init | ✅ Fine |
| [requirements.txt](file:///c:/Users/kkart/Downloads/SDT_candidate_kit/candidate_kit/requirements.txt) | Dependencies (fastapi, uvicorn, pymongo, pydantic, python-dotenv) | ⚠️ May need additions |
| [sample_data/](file:///c:/Users/kkart/Downloads/SDT_candidate_kit/candidate_kit/sample_data) | 6 employees + 9 attendance logs as MongoDB Extended JSON | ✅ Test data |
| [sample_seed.py](file:///c:/Users/kkart/Downloads/SDT_candidate_kit/candidate_kit/sample_seed.py) | Script to load sample data into MongoDB | ✅ Ready to use |
| [REVIEW.md](file:///c:/Users/kkart/Downloads/SDT_candidate_kit/candidate_kit/REVIEW.md) | Template — list every defect found in starter code | 📝 Empty, must fill |
| [DECISIONS.md](file:///c:/Users/kkart/Downloads/SDT_candidate_kit/candidate_kit/DECISIONS.md) | Template — 5 design questions to answer (~150–200 words total) | 📝 Empty, must fill |
| [README.md](file:///c:/Users/kkart/Downloads/SDT_candidate_kit/candidate_kit/README.md) | Setup instructions template | ⚠️ Needs customization |

---

## 📊 Sample Data Overview

**6 Employees** across 3 departments:
| emp_code | Name | Department | Shift | Joined | Notes |
|---|---|---|---|---|---|
| EMP0001 | Asha Rao | Engineering | 09:30–18:30 | 2026-01-05 | |
| EMP0002 | Vikram Shah | Engineering | 09:30–18:30 | 2026-07-13 | **Mid-month joiner** |
| EMP0003 | Neha Iyer | Sales | 09:30–18:30 | 2026-02-01 | |
| EMP0004 | Rohit Das | Sales | 09:30–18:30 | 2026-02-01 | |
| EMP0005 | Meera Nair | Support | 22:00–06:00 | 2026-03-10 | **Overnight shift** |
| EMP0006 | Karan Bedi | Support | 09:30–18:30 | 2026-03-10 | |

**9 Attendance Logs** covering edge cases: normal day, late+overtime, open record, half day (WFH), ABSENT, LEAVE, overnight shift, regularized record (with history), and a legacy record (missing `history`/`half_day` fields).

---

## 🔍 Part A: Starter Code Defect Analysis

I found **15+ defects** in [app/main.py](file:///c:/Users/kkart/Downloads/SDT_candidate_kit/candidate_kit/app/main.py):

### Helper Function Defects

| # | Where | What is Wrong | How You'd Notice |
|---|---|---|---|
| 1 | [`compute_late_minutes` L33–37](file:///c:/Users/kkart/Downloads/SDT_candidate_kit/candidate_kit/app/main.py#L33-L37) | Uses `punch_in.replace(hour=h)` — this puts `shift_start` on the same calendar date as `punch_in`. For **overnight shifts** where punch-in is before midnight and shift_start is e.g. 22:00 the previous day, this is wrong. Also, for overnight shifts where punch-in is *after* midnight (before `shift_end`), the date is already the next day. | Overnight shift employee: punch-in at 22:05 for a 22:00 shift → correct. But punch-in at 01:00 for a 22:00 shift → `start` becomes 22:00 same day, gives massive late minutes. |
| 2 | [`compute_late_minutes` L36](file:///c:/Users/kkart/Downloads/SDT_candidate_kit/candidate_kit/app/main.py#L36) | Uses `int()` which truncates towards zero. For negative differences (early arrival), `int(-0.5) = 0`, which accidentally works. But the **grace period logic is wrong**: R2 says late only if **strictly more than 10:00 minutes** after shift_start. The code checks `> 10` but computes `int(total_seconds / 60)` — `int()` truncates, so at exactly 10:00.99 it gives 10, and 10 > 10 is false. But at 10:01.00 it gives 10, and the function returns 10. However, R2 says "09:40:01 → 10" meaning the late_minutes should be **floor of total minutes since shift_start**. The current `int()` truncation happens to work for positives but the semantic is muddled. The real bug: it doesn't use `math.floor`. For negative values `int()` truncates toward zero (e.g. `int(-5.7)=-5`) while `floor` gives -6. Not a current issue since negatives return 0, but wrong semantics. | `09:40:01` → `(1 second) / 60 = 0.016` → `int(0.016)=0` → returns 0. BUT it should return 10 (minutes since shift_start). **The calculation is fundamentally wrong** — it divides total seconds by 60 but doesn't compute elapsed time since shift_start correctly because of the replace-based approach. Wait — let me re-check: `punch_in=09:40:01`, `start=09:30:00`, diff = 601 seconds, `601/60 = 10.016`, `int(10.016)=10`, `10 > 10` is false → returns 0. ✅ That's correct per R2! But `punch_in=09:40:01` actually should give **10** per R2's example "09:40:01 → 10". So the `> 10` check is wrong — it should be `> 10` on the raw minutes from `total_seconds`, not on the floored result. Actually: 601 seconds > 600 seconds (10 minutes), so late. Minutes since shift_start = floor(601/60) = 10. The code does `int(601/60)=10`, checks `10 > 10` which is **FALSE**, returns 0. **BUG!** It should check `total_seconds > 600` (i.e., more than 10 minutes in seconds). |
| 3 | [`compute_work_hours` L40–41](file:///c:/Users/kkart/Downloads/SDT_candidate_kit/candidate_kit/app/main.py#L40-L41) | Uses Python's built-in `round()` which does **banker's rounding** (round-half-to-even). R4 requires **half-up** rounding. Example: `round(4.545, 2)` → `4.54` (banker's) vs `4.55` (half-up). | `work_hours` of exactly `X.XX5` seconds boundary → wrong by 0.01. |
| 4 | [`compute_overtime` L44–47](file:///c:/Users/kkart/Downloads/SDT_candidate_kit/candidate_kit/app/main.py#L44-L47) | **Does not apply R3's 30-minute threshold.** R3 says overtime is counted "only if that is at least 30; otherwise 0." The code returns any positive value. Also, for overnight shifts, `shift_end` is on the next day but the code puts it on `date_str`'s day. | Employee punches out 15 minutes after shift_end → code returns 15, should return 0. Overnight shift: `shift_end = "06:00"`, `date_str = "2026-07-06"` → end = `2026-07-06 06:00 IST` but the shift actually ends `2026-07-07 06:00 IST`. |

### Endpoint Defects

| # | Where | What is Wrong | How You'd Notice |
|---|---|---|---|
| 5 | [`/health` L72–73](file:///c:/Users/kkart/Downloads/SDT_candidate_kit/candidate_kit/app/main.py#L72-L74) | Does **not ping MongoDB**. The contract says "200 only when MongoDB answers a ping; else 503." The code always returns 200. | Kill MongoDB → health still returns 200. |
| 6 | [`create_employee` L79](file:///c:/Users/kkart/Downloads/SDT_candidate_kit/candidate_kit/app/main.py#L79) | Uses **check-then-insert** (find_one → insert_one). **Not race-safe.** Two concurrent creates with the same `emp_code` can both pass the check. The contract says "enforced by a database unique index." No unique index is created. | Send two concurrent POST /employees with same emp_code → both get 201. |
| 7 | [`create_employee` L82](file:///c:/Users/kkart/Downloads/SDT_candidate_kit/candidate_kit/app/main.py#L82) | `datetime.now()` returns a **naive** datetime (no timezone). The contract expects `created_at` as epoch milliseconds in the response, and MongoDB stores BSON datetime. A naive datetime is ambiguous. Should use `datetime.now(timezone.utc)` or equivalent. | `created_at` timestamp is off by the server's local timezone offset from UTC. |
| 8 | [`create_employee` L82–85](file:///c:/Users/kkart/Downloads/SDT_candidate_kit/candidate_kit/app/main.py#L82-L85) | `created_at` is returned as a raw `datetime` object, not as epoch milliseconds. The contract requires `created_at` to be an integer (epoch ms). | Response has `"created_at": "2026-07-06T..."` instead of `"created_at": 1783312500000`. |
| 9 | [`create_employee`](file:///c:/Users/kkart/Downloads/SDT_candidate_kit/candidate_kit/app/main.py#L77-L85) | **No input validation**: doesn't validate `shift_start != shift_end`, doesn't validate `emp_code` format (`^EMP\d{4,6}$`), doesn't validate `email` format, doesn't validate field lengths, doesn't validate `joined_on` is a valid date. The openapi spec defines patterns and constraints. | Invalid emp_code like "HELLO" is accepted. shift_start == shift_end is accepted. |
| 10 | [`list_employees` L93](file:///c:/Users/kkart/Downloads/SDT_candidate_kit/candidate_kit/app/main.py#L93) | **Skip calculation is wrong.** `skip = page * page_size` — for page 1 this skips `page_size` items (the entire first page). Should be `(page - 1) * page_size`. | Page 1 returns the second page of results. Page 1 with 6 employees and page_size=20 returns nothing. |
| 11 | [`list_employees` L94](file:///c:/Users/kkart/Downloads/SDT_candidate_kit/candidate_kit/app/main.py#L94) | **`total` counts ALL employees**, not the filtered set. When `department` filter is applied, `total` should count only matching employees. `count_documents({})` ignores the filter. | Filter by department "Engineering" (2 employees), but `total` says 6. |
| 12 | [`list_employees` L95](file:///c:/Users/kkart/Downloads/SDT_candidate_kit/candidate_kit/app/main.py#L95) | Results are **not sorted by `emp_code` ascending** as the contract requires. | Employees appear in insertion order, not alphabetical by emp_code. |
| 13 | [`list_employees`](file:///c:/Users/kkart/Downloads/SDT_candidate_kit/candidate_kit/app/main.py#L89) | `created_at` is returned as a raw datetime, not epoch milliseconds. Also, no `page_size` max (100) validation, and `page < 1` is not rejected with 422. | `created_at` wrong format; page_size=999 is accepted. |
| 14 | [`punch_in` L101](file:///c:/Users/kkart/Downloads/SDT_candidate_kit/candidate_kit/app/main.py#L101) | **No 404 for unknown employee.** If `emp_code` doesn't exist, `emp` is `None`, and line 113 crashes with `TypeError: 'NoneType' object is not subscriptable` → returns 500 instead of 404. | POST punch-in with a non-existent emp_code → 500 error. |
| 15 | [`punch_in` L102](file:///c:/Users/kkart/Downloads/SDT_candidate_kit/candidate_kit/app/main.py#L102) | `datetime.fromtimestamp(body.punched_at / 1000)` — returns a **naive** datetime in the **local** timezone, not UTC. R1 says dates are IST. This will behave differently on servers in different timezones. Should convert to IST explicitly. | Server in UTC: punch at 09:30 IST shows as 04:00 UTC, but `fromtimestamp` interprets epoch as local time. If server is UTC, the IST date is wrong. |
| 16 | [`punch_in` L102](file:///c:/Users/kkart/Downloads/SDT_candidate_kit/candidate_kit/app/main.py#L102) | Does **not truncate to whole seconds** per R1. "Instants are truncated to whole seconds before any calculation, and the truncated value is what is stored and returned." | `punched_at` with non-zero milliseconds → stored with sub-second precision. |
| 17 | [`punch_in` L102](file:///c:/Users/kkart/Downloads/SDT_candidate_kit/candidate_kit/app/main.py#L102) | Does **not validate** `punched_at`. The EpochMillis schema says values below `100000000000` (looks like seconds), floats, and strings should be rejected with 422. The Pydantic model accepts `Optional[int]` but doesn't enforce the minimum. | `punched_at: 1000` (seconds-looking value) is silently accepted. |
| 18 | [`punch_in` L103](file:///c:/Users/kkart/Downloads/SDT_candidate_kit/candidate_kit/app/main.py#L103) | **Attendance date (R1)** is computed as `ts.date().isoformat()`. Since `ts` is in local (naive) time rather than IST, the date is wrong. Also, **overnight shift logic is missing**: for overnight shifts where punch-in's IST time is earlier than `shift_end`, the date should be the *previous* calendar day. | EMP0005 (22:00–06:00 shift): punch-in at 02:00 IST on July 7 → date should be "2026-07-06" (previous day's shift), but code returns "2026-07-07". |
| 19 | [`punch_in` L104–105](file:///c:/Users/kkart/Downloads/SDT_candidate_kit/candidate_kit/app/main.py#L104-L105) | **Race condition**: uses check-then-insert (find_one → insert_one). Two concurrent punch-ins for the same employee/date can both pass the find_one check. The contract says "exactly one request gets 201 and the rest get 409. Do not rely on check-then-insert." Should use a unique index on `(emp_code, date)` and catch the `DuplicateKeyError`. | Two simultaneous punch-in requests for the same employee on the same date → both get 201. |
| 20 | [`punch_in` L106–121](file:///c:/Users/kkart/Downloads/SDT_candidate_kit/candidate_kit/app/main.py#L106-L121) | **Does not validate `status`** — accepts any string including "ABSENT", "LEAVE", or arbitrary values. Only "PRESENT", "WFH", "ON_DUTY" should be accepted at punch-in. | `status: "ABSENT"` with a punch-in creates an invalid record. |
| 21 | [`punch_in` L119](file:///c:/Users/kkart/Downloads/SDT_candidate_kit/candidate_kit/app/main.py#L119) | Returns `doc["id"] = str(res.inserted_id)` — the contract says **attendance records have no `id` field**. "_id is internal and never appears in a request or response. Attendance records have no id either." | Response includes `"id": "..."` which is not in the contract schema. |
| 22 | [`punch_in` response](file:///c:/Users/kkart/Downloads/SDT_candidate_kit/candidate_kit/app/main.py#L106-L121) | `punch_in` and `punch_out` are returned as raw datetime objects, not as epoch milliseconds (or null). All instants in responses must be epoch ms integers. | Response has `"punch_in": "2026-07-06T..."` instead of an integer. |
| 23 | [`list_attendance` L144–150](file:///c:/Users/kkart/Downloads/SDT_candidate_kit/candidate_kit/app/main.py#L144-L150) | **Loads ALL matching docs into memory** (`list(find(q))`), sorts in Python, then slices. On 100k docs this is catastrophically slow and memory-heavy. Should use MongoDB's `.sort()`, `.skip()`, `.limit()`. | On 100k records: endpoint is extremely slow, high memory usage, possible OOM. |
| 24 | [`list_attendance` L145](file:///c:/Users/kkart/Downloads/SDT_candidate_kit/candidate_kit/app/main.py#L145) | Sorts only by `date` descending. The contract says "sorted by date descending, **then emp_code ascending**." | Two records on the same date appear in arbitrary order instead of by emp_code. |
| 25 | [`list_attendance` L149](file:///c:/Users/kkart/Downloads/SDT_candidate_kit/candidate_kit/app/main.py#L149) | Returns `d["id"] = str(d.pop("_id"))` — attendance records should **not** have an `id` field. | Response includes `"id"` field not in the contract. |
| 26 | [`list_attendance`](file:///c:/Users/kkart/Downloads/SDT_candidate_kit/candidate_kit/app/main.py#L124-L150) | Datetime fields (`punch_in`, `punch_out`, history `at`/`from`/`to`) are returned as raw datetimes, not epoch milliseconds. Also, `created_at` (on employees) same issue. Missing `half_day` and `history` are not defaulted. No page_size max validation. No `date_from > date_to` → 422 validation. | All instant fields are wrong format. Legacy records (no `history`/`half_day`) crash or return incomplete data. |

### Missing Features — No Indexes

| # | What |
|---|---|
| 27 | **No indexes created at startup.** The contract says "Create all indexes at startup, idempotently." No `create_index` calls exist anywhere. This means: no unique index on `emp_code`, no unique compound index on `(emp_code, date)` for attendance, no indexes for analytics query performance. |

---

## 🔨 Part B: Endpoints That Must Be Built from Scratch

These are listed as **TODO** in [main.py L153–162](file:///c:/Users/kkart/Downloads/SDT_candidate_kit/candidate_kit/app/main.py#L153-L162):

| Endpoint | Difficulty | Key Challenges |
|---|---|---|
| `POST /attendance/punch-out` | Medium | Find the right open record (works across midnight for overnight shifts), compute R3–R5, concurrency (exactly one 200), validate `punched_at > punch_in` and within 24 hours |
| `PATCH /attendance/{emp_code}/{date}` | Hard | Regularization: recompute all derived fields, build `history` diff entry (only changed fields), handle ABSENT/LEAVE clearing punches, validate no-change = 422, concurrency (don't lose history entries) |
| `GET /analytics/employees/{emp_code}/monthly` | Hard | MongoDB aggregation pipeline. Compute `working_days` (Mon–Fri from `joined_on`), `present_days` (half_day=0.5, Mon–Fri only), `late_count`, `attendance_pct` with R8 rounding |
| `GET /analytics/departments/summary` | **Very Hard** | Cross-collection aggregation (`$lookup`). Headcount includes employees with **zero logs** (R9). Average over records, not avg of avgs. Must be a MongoDB pipeline, not Python. |
| `GET /analytics/leaderboard/late` | **Very Hard** | Standard competition ranking (1,2,2,4 — NOT dense rank). `limit` applies after ranking (return all rows with `rank <= limit`). Ties ordered by emp_code. Cannot use array index as rank. Needs `$setWindowFields` with `$rank`. |
| `GET /analytics/departments/{department}/trend` | **Very Hard** | Gap-fill with `$densify` for every calendar day. 7-day moving average of non-null `attendance_rate` within the requested range only. Per-day `headcount` from `joined_on`. Must be done in MongoDB, not Python loop. Range max 92 days. |
| `GET /admin/explain/{endpoint}` | Medium | Run `explain("executionStats")` on each endpoint's pipeline. Must show IXSCAN, no COLLSCAN on ~100k records. Accepts the same query params as the explained endpoint. |

---

## 📋 Part C: Documentation to Write

### REVIEW.md
List each defect (the 25+ above), with: location, what's wrong, how to notice, how you fixed it.

### DECISIONS.md (5 questions, ~150–200 words total)
1. **Indexes** — which and why
2. **Punch-in race** — exactly what happens with concurrent identical requests
3. **Ties at cutoff** — what the leaderboard returns
4. **Headcount** — how dept summary counts zero-log employees
5. **100x scale** — one thing you'd change

---

## 📐 Key Business Rules Summary (R1–R10)

| Rule | Critical Detail |
|---|---|
| **R1** | IST timezone. Overnight shifts: punch-in before `shift_end` → previous day. **Truncate to whole seconds.** |
| **R2** | Late only if **strictly >10 min** after shift_start. Late_minutes = floor(minutes since shift_start). Grace period edge: 09:40:00 → 0, 09:40:01 → 10. |
| **R3** | Overtime only if ≥ 30 minutes past shift_end; otherwise 0. |
| **R4** | Work hours = seconds/3600, **half-up** rounding to 2 decimals. |
| **R5** | Half day = true if rounded `work_hours < 4.50`. Counts as 0.5 present day. |
| **R6** | PRESENT/WFH/ON_DUTY = present. ABSENT/LEAVE = not. |
| **R7** | Working days = Mon–Fri. Mid-month joiners: count from `joined_on`. Weekend records count for late/overtime but NOT for `present_days`. |
| **R8** | 2 decimals half-up (rates: 4 decimals half-up). |
| **R9** | Headcount = employees with `joined_on ≤ end of period`, including those with zero logs. |
| **R10** | Pagination: page starts at 1, page_size default 20, max 100, total reflects filters. |

---

## ✅ What's Already Done (Working or Partially Working)

- [x] Project structure (`app/main.py` as single file)
- [x] FastAPI app skeleton with title/version
- [x] Environment variable loading (MONGO_URI, MONGO_DB) — with dotenv
- [x] IST timezone constant defined
- [x] Pydantic models for EmployeeIn and PunchInIn (but incomplete validation)
- [x] `GET /health` endpoint (but doesn't actually ping MongoDB)
- [x] `POST /employees` endpoint (but many bugs)
- [x] `GET /employees` endpoint (but pagination, sorting, total bugs)
- [x] `POST /attendance/punch-in` endpoint (but many bugs)
- [x] `GET /attendance` endpoint (but performance, sorting, format bugs)
- [x] Helper functions (all 3 have bugs)
- [x] Sample data and seed script
- [x] requirements.txt with core dependencies

## ❌ What's Missing (Must Build)

- [ ] **Fix all 25+ defects** in existing code
- [ ] **7 new endpoints** (punch-out, regularize, 4 analytics, explain)
- [ ] **MongoDB indexes** at startup (unique constraints + query performance)
- [ ] **All datetime ↔ epoch-ms conversions** at API boundary
- [ ] **Input validation** (emp_code format, epoch ms range, status enums, date formats)
- [ ] **Concurrency handling** (unique indexes + DuplicateKeyError catch)
- [ ] **3 complex aggregation pipelines** (department summary, leaderboard, trend)
- [ ] **Half-up rounding** (replace Python's `round()` with `Decimal` or manual)
- [ ] **Overnight shift logic** throughout
- [ ] **REVIEW.md** filled in
- [ ] **DECISIONS.md** filled in
- [ ] **README.md** customized

---

## ⚖️ Evaluation Weights

| Area | Weight |
|---|---|
| Review (Part A) — REVIEW.md + fixes | 10% |
| CRUD, rules, concurrency — automated tests | 30% |
| Analytics correctness — vs hidden dataset | 30% |
| MongoDB usage & speed — explain, indexes, 100k perf | 15% |
| Decisions & code quality | 15% |

> [!IMPORTANT]
> The hidden test dataset is **much larger and nastier** than the 9 sample docs. It includes: ties, weekend records, employees with no logs, mid-month joiners, overnight shifts, and ~100,000 attendance records. The grader runs automated tests checking exact field names, status codes, and numeric precision.
