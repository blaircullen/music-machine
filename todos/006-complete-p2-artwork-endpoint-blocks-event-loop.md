---
status: pending
priority: p2
issue_id: "006"
tags: [performance, bug, code-review, plex]
---

# Artwork Endpoint Blocks Event Loop + Thundering Herd (No Cache)

## Problem Statement
`GET /api/tracks/{track_id}/artwork` is `async def` but calls `search_plex_track()` — a synchronous function that makes multiple Plex HTTP calls — directly on the event loop. Under uvicorn this blocks the entire event loop for each call. When a 38-track station loads, the player fires 38 concurrent artwork requests, each potentially blocking the event loop for 150-500ms while doing 2-4 Plex API round-trips. There is zero caching — every player session repeats the full resolution sequence.

## Findings
- **File:** `backend/routes/sonic.py:163-217`
- `search_plex_track()` in `plex_playlist_sync.py` is sync (uses `requests`), called from `async def track_artwork()`
- Each request: (1) DB lookup, (2) sync Plex title search (1-4 calls), (3) async Plex metadata fetch, (4) async image stream
- 38 concurrent requests × 3+ Plex calls = 100+ Plex API calls on player load
- Also: two separate `async with httpx.AsyncClient` blocks (lines 185 and 204) — two connections per request when one suffices

## Proposed Solution

**Option A (Recommended) — Cache ratingKey per track_id in-memory:**
```python
# Module-level cache in sonic.py
_artwork_cache: dict[int, str] = {}  # track_id → thumb_url

@router.get("/api/tracks/{track_id}/artwork")
async def track_artwork(track_id: int):
    if track_id in _artwork_cache:
        # Fetch directly from cached thumb URL
        ...
    # Otherwise resolve and store in _artwork_cache
```

**Option B — Persist ratingKey to DB:**
Add `plex_rating_key TEXT` column to `tracks` table (or a separate `track_plex_cache` table). Populated once per track, persists across restarts.

**Option C (immediate fix, no caching) — Run sync call in thread pool:**
```python
from fastapi.concurrency import run_in_threadpool
rating_key = await run_in_threadpool(search_plex_track, row["artist"] or "", row["title"] or "")
```
This at least stops blocking the event loop.

**Also fix:** Merge the two `httpx.AsyncClient` blocks into one to reuse the connection.

## Acceptance Criteria
- [ ] Player loads 38-track station without hammering Plex with 100+ API calls
- [ ] Second player load of same station: 0 Plex API calls (served from cache)
- [ ] `track_artwork` does not block the uvicorn event loop
- [ ] Artwork still loads correctly for new tracks not yet in cache

## Effort: Medium
