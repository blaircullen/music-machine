---
status: pending
priority: p1
issue_id: "001"
tags: [security, code-review]
---

# Path Traversal in /api/stream/{track_id}

## Problem Statement
`stream_track()` in `backend/routes/sonic.py` serves audio files by reading `file_path` from the DB and passing it directly to `FileResponse` with no boundary validation. Any row in the `tracks` table with a malicious `file_path` can read arbitrary files from inside the container (DB itself, env vars, Plex token, etc).

## Findings
- **File:** `backend/routes/sonic.py:118`
- `file_path = Path(row["file_path"])` — no `is_relative_to()` check
- Compare with `tagger.py:229` which correctly does: `if not music_path.is_relative_to(base_music_path): raise 400`
- Precondition: a malicious `file_path` must exist in the DB. This is reachable via: (1) symlinks in the NAS music directory picked up by the scanner; (2) any future bug in another write path (upgrade importer, tagger)

## Proposed Solution

**Option A (Recommended) — 4-line fix:**
```python
MUSIC_ROOT = Path(os.environ.get("MUSIC_PATH", "/music")).resolve()
# In stream_track(), after line 118:
resolved = file_path.resolve()
if not resolved.is_relative_to(MUSIC_ROOT):
    raise HTTPException(status_code=403, detail="Access denied")
```

**Option B — Validate at DB insert time in scanner.py:** Reject any path that doesn't start with the music root when writing to `tracks`. Defends in depth but doesn't fix the existing endpoint.

**Recommended:** Option A is 4 lines and fixes the vector immediately. Do both for defense in depth.

## Acceptance Criteria
- [ ] `GET /api/stream/{track_id}` where the track's `file_path` is `/etc/passwd` returns 403
- [ ] Normal audio stream requests still work
- [ ] `MUSIC_PATH` env var is respected as the boundary

## Effort: Small
