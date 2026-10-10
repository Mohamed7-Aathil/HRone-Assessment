# DECISIONS.md

**1. Indexes.**
I created seven indexes across the two collections. On `employees`:
`emp_code` (unique) blocks duplicate employees and serves every lookup by code;
`(department, emp_code)` and `(department, joined_on)` help the department
summary and trend endpoints. On `attendance_logs`: `(emp_code, date)` is unique
and prevents two records for the same employee-day; `(date desc, emp_code)`
serves the default attendance listing sorted by most recent date;
`(emp_code, date, status)` helps filtered lookups by status;
`(date, status, late_minutes desc)` serves the late leaderboard. I considered a
plain `date` index but rejected it — every query that filters by date also
filters by `emp_code` or `status`, so a compound index keyed on those first is
more selective.

**2. Punch-in race.**
Both requests call `insert_one` on `attendance_logs` with the same
`(emp_code, date)` pair. The unique index `uq_attendance_employee_date` allows
exactly one insert to succeed. The second insert raises `DuplicateKeyError`,
which I catch and translate into a 409 response. There is no read-then-write
window, so the check is race-free.

**3. Ties.**
The leaderboard pipeline uses `$setWindowFields` with `$rank`, sorted by
`total_late_minutes` descending. `$rank` assigns the same rank to tied
employees, which is the correct semantics for a leaderboard. The final `$sort`
adds `_id` (emp_code) ascending as a secondary key, so the output order is
deterministic and a `limit` cut-off never drops a row arbitrarily.

**4. Headcount.**
The department summary starts from the `employees` collection filtered by
`department` and `joined_on <= end_of_month`, then joins their attendance logs
in Python. An employee with zero attendance logs still appears in the summary
with `present_days: 0`, `late_count: 0`, and `avg_work_hours: null`. This
guarantees headcount reflects the roster, not just active loggers.

**5. At 100x.**
I would move the analytics off the app process and into MongoDB aggregation
pipelines — `$lookup` + `$group` for the department summary, `$setWindowFields`
already used for the leaderboard, and a `$densify`-based trend for missing
dates. That keeps the heavy work server-side, uses the compound indexes
end-to-end, and avoids pulling large result sets into Python. I would also add
a covering index for the leaderboard `(date, status, late_minutes)` so the whole
pipeline runs from the index.
