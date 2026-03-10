---
status: pending
priority: p3
issue_id: "014"
tags: [quality, code-review, cleanup]
---

# Dead Code Cleanup

## Items

### 1. `n = len(blob) // 4` never used — sonic_service.py:130
```python
n = len(blob) // 4   # ← computed, never referenced
vec = np.frombuffer(blob, dtype=np.float32).copy()
```
Delete the `n =` line.

### 2. `controls.play()` and `controls.pause()` unused — usePlayer.ts:160-165
These are in `PlayerControls` interface and `controls` object but `Player.tsx` only uses `togglePlay()`. Remove from interface and implementation. Add back if media session API is ever needed.

### 3. `stations_service.py` (old Last.fm implementation) — check if dead
The architecture agent flagged this file may still exist alongside `sonic_service.py`. Verify it's not imported anywhere, then delete.

### 4. `_pack_vector` should use `.tobytes()` — sonic_service.py:139
```python
# Current (slower):
return struct.pack(f"{len(vec)}f", *vec.astype(np.float32))
# Better:
return vec.astype(np.float32).tobytes()
```

### 5. `station_feedback` table has no pruning
Append-only forever. Add a periodic DELETE for rows older than 90 days to the existing history pruning job in `_save_history()` or a separate scheduled task.

## Effort: Small (all items together)
