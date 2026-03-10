---
status: pending
priority: p1
issue_id: "003"
tags: [bug, code-review, sonic-analyzer]
---

# Infinite Retry Loop on Essentia TimeoutExpired

## Problem Statement
When `essentia_streaming_extractor_music` times out (after 120s), `analyze_track()` logs the error and returns `False` — but does **not** remove the track from `analysis_queue`. The worker then immediately re-picks it on the next loop iteration. At 32 concurrency with a 0.1s sleep, this creates a tight loop retrying a track that will always timeout, blocking queue slots and burning CPU indefinitely.

CLAUDE.md explicitly states: _"Always DELETE from queue on: (1) file-not-found, (2) extractor non-zero exit. Both are permanent failures."_ Timeout is also a permanent failure class (e.g. multi-channel DSD files, corrupt containers).

## Findings
- **File:** `sonic-analyzer/worker.py:241-243`
```python
except subprocess.TimeoutExpired:
    logger.error(f"Track {track_id}: extractor timed out after 120s")
    return False  # ← track stays in queue, picked again immediately
```
- Compare with the file-not-found path (lines 163-169) and non-zero exit path (193-199) which both DELETE from queue correctly.

## Proposed Solution

**Option A (Recommended):**
```python
except subprocess.TimeoutExpired:
    logger.error(f"Track {track_id}: extractor timed out after 120s, removing from queue")
    try:
        conn = get_db()
        conn.execute("DELETE FROM analysis_queue WHERE track_id = ?", (track_id,))
        conn.commit()
        conn.close()
    except Exception:
        pass
    return False
```

**Option B — Retry with counter:** Allow up to N retries before permanent removal. Useful if timeouts are transient (resource contention). Store retry count in `analysis_queue`. More complex, likely not needed.

## Acceptance Criteria
- [ ] A track that times out is removed from `analysis_queue` and not re-picked
- [ ] Log message explicitly states "removed from queue"
- [ ] The track remains in `tracks` table (still scannable; just not analyzable)
- [ ] A bootstrap insert of the track back into the queue is possible if the user wants to retry manually

## Effort: Small
