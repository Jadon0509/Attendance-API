# DECISIONS.md

1. **Indexes.** Unique `(emp_code, date)` guarantees one record per day and serves single-employee queries. `(date desc, emp_code)` serves the default list order and month scans for analytics. `(status, date desc, emp_code)` serves the status filter. `(emp_code, punch_in desc)` finds the record to punch out. On employees: unique `emp_code`, `(department, joined_on)`, `(joined_on)`. I rejected an index on `late_minutes`; the date range is already selective.
2. **Punch-in race.** Both requests skip any "check first" and call `insert_one`. The unique index lets MongoDB accept exactly one; the other gets `DuplicateKeyError`, which I turn into 409.
3. **Ties.** Ranking uses `$rank` on total late minutes only, so tied people share a rank. The limit is applied after ranking, so a tie at the cutoff returns all tied people, even if that is more than `limit` rows.
4. **Headcount.** The pipeline starts from `employees`, joins logs with `$lookup`, then groups. Someone with no logs is still one employee row, so they are counted.
5. **100x data.** I would keep daily per-department totals in a separate collection, updated when records change, so analytics read small summaries instead of scanning logs.
