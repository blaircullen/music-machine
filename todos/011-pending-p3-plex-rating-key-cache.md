---
status: pending
priority: p3
issue_id: "011"
tags: [performance, plex, code-review]
---

# Cache Plex ratingKey per Track to Eliminate Redundant Searches

## Problem Statement
`_sync_plex_playlist()` calls `search_plex_track()` for each of 38 tracks on every station refresh — 40-80 sequential Plex API calls. The `track_id → ratingKey` mapping is stable (only changes if Plex re-indexes). A simple cache would make subsequent refreshes near-instant.

## Findings
- **File:** `backend/sonic_service.py:506-528`
- This is also the root cause of `006` (artwork thundering herd) — solving this here covers both

## Proposed Solution
**Option A — In-memory module dict (fast, lost on restart):**
```python
_rating_key_cache: dict[int, str] = {}  # track_id → ratingKey
```

**Option B — DB table `track_plex_cache(track_id, rating_key, cached_at)`** — persists across restarts, requires schema migration.

Option A is fine for a personal tool. The cache fills up after one refresh cycle and stays hot.

## Effort: Small
