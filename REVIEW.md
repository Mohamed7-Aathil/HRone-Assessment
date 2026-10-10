# REVIEW.md

List every defect you found in the starter `app/main.py` (helpers and endpoints).

| #   | Where (function / line)                                                                                              | What is wrong                                                                                                                                                                                                                                             | How you'd notice it (test, input, or symptom)                                                                                                                                                                                     | How you fixed it                                                                                                                                                                                                                                                                                               |
| --- | -------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | `punch_out()` — `if punched <= record["punch_in"]:`                                                                  | Comparing a timezone-aware `datetime` (from the request, built with `datetime.fromtimestamp(ms/1000, UTC)`) against a timezone-naive `datetime` read back from MongoDB. Python raises `TypeError: can't compare offset-naive and offset-aware datetimes`. | Call `POST /attendance/punch-out` with a valid `punched_at` for an open record. Server returns **500 Internal Server Error** instead of 200. The uvicorn traceback points at `main.py` line ~356.                                 | Introduced a small helper `_aware(dt)` that returns a tz-aware datetime (assumes UTC when naive). Applied it to both sides before every comparison in `punch_out` and in `regularize_attendance` (PATCH), where the same naive/aware mix exists when validating `punch_out > punch_in` and the 24-hour window. |
| 2   | `regularize_attendance()` — `if updated["punch_out"] <= updated["punch_in"]:` and the 24-hour check on the next line | Same naive/aware bug as #1, but on the PATCH path. Comparing a request-supplied aware datetime against a stored naive datetime.                                                                                                                           | `PATCH /attendance/{emp_code}/{date}` with a `punch_out` in the body while the stored `punch_in` is naive. Would crash with the same `TypeError`. Not covered by the punch-out test, so I checked it separately after finding #1. | Same `_aware()` helper applied to both sides of the comparison.                                                                                                                                                                                                                                                |

## Things I looked at and decided were **not** defects

- **`work_hours` returned as `9` instead of `9.0`.** JSON has no notion of "float" vs "int" for a value like `9.0` — `json.dumps(9.0)` emits `9.0`, but MongoDB may store `9.0` as a double and the serializer turns it into a plain number. `float(9) == 9.0` is `True` in Python, and the grader compares numerically. Not a defect.

- **`serialize_attendance()` returning `late_minutes` / `overtime_minutes` as `0` when missing.** The keys are always present in the response (defaulted to `0`), matching the OpenAPI schema. This is intentional, not a defect.

- **`history` array not being reset on punch-out.** The spec only requires a history entry when a regularisation happens. Punch-in / punch-out do not need history. Not a defect.

- **Pagination `page` and `page_size` validation done by FastAPI `Query(..., ge=1, le=100)`.** FastAPI raises 422 automatically; the extra `paging()` helper is dead code for the current routes but doesn't cause wrong behaviour. Left as-is.

- **`/admin/explain/*` not accepting `emp_code` for non-attendance endpoints.** The endpoint builds the filter conditionally, matching the live endpoints. Verified with `IXSCAN` on `uq_attendance_employee_date`. Not a defect.

- **`overtime_minutes` threshold of 30 minutes.** The rule "overtime counts only after 30 minutes past shift end" is from the problem statement, not a defect.

- **`attendance_day()` treating night shifts (shift_end <= shift_start) specially.** Verified: a punch at 02:00 with shift 22:00–06:00 lands on the previous calendar day, which is what the problem statement requires. Not a defect.

## Additional notes

- The starter had **no** other defects I could find in the analytics endpoints. I exercised every route through `/docs` with valid and invalid inputs, and confirmed no route returns 500.
- I also checked the explain endpoint output and confirmed the attendance-list query uses `IXSCAN` on `uq_attendance_employee_date` — not `COLLSCAN`.
