---
status: pending
priority: p2
issue_id: "007"
tags: [bug, code-review, sonic]
---

# ValueError Crash in _refresh_diverse_seeds When All Seed Vectors Are None

## Problem Statement
`_refresh_diverse_seeds()` has a fallback path for `len(vecs) < 2` that calls `np.mean()` on potentially an **empty** list. `np.mean` on an empty array with `axis=0` raises `ValueError: zero-size array to reduction operation`. This crashes the entire station refresh silently.

## Findings
- **File:** `backend/sonic_service.py:474-477`
```python
if len(vecs) < 2:
    # Fall back to centroid
    centroid = np.mean(np.array(vecs, dtype=np.float32), axis=0)  # ← crash if vecs=[]
    return _similarity_search(matrix, track_ids, centroid, excluded, n=_SAMPLE_SIZE)
```
- Reachable when: seed tracks exist in DB, `_seeds_are_diverse()` returned True (so feature vectors were found), but by the time `_refresh_diverse_seeds` runs, all feature vectors are None after the `_unpack_vector` filter at line 473
- The error propagates up to `refresh_station()` which catches it as a generic Exception and returns `ok: False`

## Proposed Solution

**Option A (Recommended):**
```python
if len(vecs) < 2:
    if not vecs:
        # No vectors at all — fall back to global similarity search without a target
        return _similarity_search(matrix, track_ids, matrix.mean(axis=0), excluded, n=_SAMPLE_SIZE)
    centroid = vecs[0]  # Only one valid vector — use it directly
    return _similarity_search(matrix, track_ids, centroid, excluded, n=_SAMPLE_SIZE)
```

**Option B — Return early with empty list:** Let `refresh_station` handle it with its existing "No similar tracks found" error.

**Option C — Remove `_refresh_diverse_seeds` entirely:** The simplicity review flagged this as YAGNI. If removed, the crash path disappears along with ~75 lines of complex code.

## Acceptance Criteria
- [ ] Station refresh does not raise ValueError when diverse seed path is taken with all-None feature vectors
- [ ] Fallback produces a reasonable (non-empty) playlist if any tracks are in the feature matrix

## Effort: Small
