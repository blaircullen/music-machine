---
status: pending
priority: p1
issue_id: "004"
tags: [bug, code-review, sonic-analyzer]
---

# Feature Cache Never Reloads After First Load — New Tracks Invisible to Recommendations

## Problem Statement
The sonic feature cache (`_feature_matrix`) loads once on first station refresh and never reloads during the process lifetime. The sonic-analyzer sidecar writes `sonic_cache_dirty='true'` to the settings table after each analysis, but the FastAPI backend's `_ensure_cache()` only reads the DB dirty flag when `_cache_dirty` (the in-memory flag) is already `True`. After the first successful load sets `_cache_dirty = False`, the DB flag is never checked again. Newly analyzed tracks are invisible to station recommendations until the backend container restarts.

## Findings
- **File:** `backend/sonic_service.py:93-113`
- `_ensure_cache()` flow:
  1. If `_cache_dirty or _feature_matrix is None` → enter block
  2. Read DB dirty flag and set `_cache_dirty = True` if dirty
  3. If `_cache_dirty or _feature_matrix is None` → call `_load_feature_cache()`
- After first load: `_cache_dirty = False` and `_feature_matrix is not None` → the outer `if` is False → the DB is never checked → DB dirty flag is ignored
- `invalidate_cache()` (line 116) exists but is **never called from any route or scheduled job** in the backend
- The sonic-analyzer correctly sets the DB flag, but the backend ignores it after initial load

## Proposed Solution

**Option A (Recommended) — Always check DB dirty flag in _ensure_cache:**
```python
def _ensure_cache():
    global _cache_dirty
    with _cache_lock:
        # Always check the DB flag, not just when already dirty
        try:
            from database import get_db
            with get_db() as db:
                row = db.execute(
                    "SELECT value FROM settings WHERE key = 'sonic_cache_dirty'"
                ).fetchone()
            if row and row["value"] == "true":
                _cache_dirty = True
        except Exception:
            pass
        if _cache_dirty or _feature_matrix is None:
            _load_feature_cache()
        return _feature_matrix, _track_ids
```

**Option B — Call `invalidate_cache()` from a route:** Add a `/api/sonic/invalidate-cache` endpoint or have sonic.py's stats endpoint call it. But this doesn't solve the cross-process signal.

**Option C — Periodic background check:** Use FastAPI's `startup` event + `asyncio.create_task` to poll the dirty flag every 60s. More complex, not needed for this use case.

## Acceptance Criteria
- [ ] After sonic-analyzer writes new feature vectors and sets dirty flag, the next station refresh uses the updated cache without requiring a container restart
- [ ] Verified by: analyzing a new track, triggering a station refresh, confirming the new track appears in the recommendation pool
- [ ] The dirty flag poll does not add noticeable latency to normal station refreshes

## Effort: Small
