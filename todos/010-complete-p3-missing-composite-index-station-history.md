---
status: pending
priority: p3
issue_id: "010"
tags: [performance, database, code-review]
---

# Missing Composite Index on station_track_history(station_id, generated_at)

## Problem Statement
Two separate single-column indexes exist on `station_track_history`. The queue endpoint makes two queries that both filter on `station_id` AND `generated_at`. SQLite can only use one index per query — the current setup causes a partial scan. Will degrade as history accumulates over months.

## Findings
- **File:** `backend/database.py:195-200`
- Current indexes: `idx_station_track_history_station_id` and `idx_station_track_history_generated_at` (separate)
- Queries in `sonic.py:58-78` need both columns together:
  ```sql
  SELECT MAX(generated_at) FROM station_track_history WHERE station_id = ?
  SELECT ... WHERE station_id = ? AND generated_at = ?
  ```

## Fix
```sql
CREATE INDEX IF NOT EXISTS idx_station_track_history_station_generated
    ON station_track_history(station_id, generated_at);
```
Add to `database.py` schema. Can keep or drop the individual indexes.

## Effort: Tiny
