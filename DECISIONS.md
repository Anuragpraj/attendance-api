# DECISIONS.md

1. **Indexes.** On employees: unique `emp_code`, `(department, emp_code)` and `joined_on`. On attendance_logs: unique `(emp_code, date)` and `(date descending, emp_code)`. The unique one blocks duplicates and serves per-employee queries; the date one serves the list sort and month scans. I rejected indexes on `status` and `late_minutes` because the date range already narrows the data.
2. **Punch-in race.** Both requests go straight to insert, with no check first. The unique `(emp_code, date)` index lets MongoDB accept one and refuse the other with a duplicate-key error. My code turns that error into 409, so exactly one request gets 201.
3. **Ties.** Equal late minutes share a rank and the next rank is skipped (1, 2, 2, 4). I filter by rank, not by row count, so everyone tied at the limit is returned, even if that is more than `limit` rows.
4. **Headcount.** The summary starts from the employees collection and attaches logs to them, so employees with no logs stay in, with zero days.
5. **100x data.** Keep daily totals per department in a separate collection, updated on every punch or correction, and store the department on each log, so reports stop scanning and joining raw records.
