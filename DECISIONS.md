# DECISIONS.md 

1. **Indexes.**
On `employees`: unique `emp_code` enforces business identifier uniqueness; `("department", 1), ("joined_on", 1)` accelerates department filtering and headcount lookups. On `attendance_logs`: unique `("emp_code", 1), ("date", 1)` enforces the natural key and race safety; `("date", -1), ("emp_code", 1)` backs paginated listing; `("emp_code", 1), ("punch_in", -1)` speeds up punch-out lookups; and `("date", 1), ("late_minutes", 1)` backs the leaderboard. We considered a partial index on `punch_out: null`, but rejected it since punch-out queries already use `emp_code` and `punch_in` efficiently.

2. **Punch-in race.**
Both concurrent requests compute the same IST attendance date. When both execute `insert_one`, MongoDB's unique compound index on `(emp_code, date)` guarantees atomic serialization. Exactly one write succeeds (HTTP 201). The second fails with a `DuplicateKeyError`, which our handler intercepts to return HTTP 409 Conflict.

3. **Ties.**
Our pipeline uses MongoDB's `$setWindowFields` with `$rank` for standard competition ranking (1, 2, 2, 4) and filters `rank <= limit`. All employees tied at or below the cutoff rank are returned (which may exceed `limit` rows), ordered by `total_late_minutes` descending then `emp_code` ascending.

4. **Headcount.**
The pipeline originates on the `employees` collection (matching `joined_on <= month_end`) and computes headcount from employee documents directly. It then `$lookup`s attendance logs preserving nulls, ensuring employees with zero attendance records in the month are counted accurately.

5. **One thing you would change for 100x the data.**
Pre-aggregate monthly summaries into materialized summary collections via write-time triggers or asynchronous event streams, eliminating dynamic cross-collection `$lookup` and `$densify` stages across 10M+ raw logs.
