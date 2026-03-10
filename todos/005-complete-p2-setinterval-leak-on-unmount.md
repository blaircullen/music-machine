---
status: pending
priority: p2
issue_id: "005"
tags: [bug, react, code-review, frontend]
dependencies: []
---

# setInterval Leak in regenerate() and handleRefresh() on Component Unmount

## Problem Statement
Two `setInterval` polling loops have no cleanup path when their parent component unmounts: `regenerate()` in `usePlayer.ts` and `handleRefresh()` in `Stations.tsx`. If the user navigates away mid-refresh, the intervals keep firing — making API calls, calling setState on stale closures, and potentially causing a race if the user returns to the same page before the orphaned poll resolves.

## Findings
**usePlayer.ts:229-243:**
```ts
const poll = setInterval(async () => {
  // polls /api/stations/{id}/refresh-status
  // eventually calls loadQueue() and setState
}, 2000)
// interval stored only in local Promise closure — no cleanup possible
```
If component unmounts: interval fires until job finishes, calls `loadQueue()` on unmounted instance, races with fresh mount's `loadQueue()` on re-navigation.

**Stations.tsx:392-413 (StationCard.handleRefresh):**
```ts
const poll = setInterval(async () => {
  // calls setRefreshing(), toast.success(), getStations(), onRefreshed()
}, 2000)
// same pattern, same leak
```
If station card unmounts (user deletes station, navigates): `toast.success()` fires, `onRefreshed()` calls parent state updater — wrong behavior.

## Proposed Solution

**Option A (Recommended) — Store interval ID in a ref, clear in useEffect cleanup:**

In `usePlayer.ts`, add a ref:
```ts
const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)
```
In `regenerate()`:
```ts
pollRef.current = setInterval(async () => { ... }, 2000)
```
In the audio `useEffect` cleanup (lines 126-129):
```ts
return () => {
  audio.pause()
  audio.src = ''
  if (pollRef.current) clearInterval(pollRef.current)
}
```

For `StationCard` in `Stations.tsx`, use `useEffect` cleanup with a mounted flag:
```ts
useEffect(() => {
  return () => { if (pollRef.current) clearInterval(pollRef.current) }
}, [])
```

**Option B — AbortController pattern:** Thread a cancel signal through the poll Promise. More elegant but higher complexity for the same outcome.

## Acceptance Criteria
- [ ] Navigating away from Player while regenerating stops the polling loop
- [ ] No network requests to `/api/stations/{id}/refresh-status` after Player unmounts
- [ ] Deleting a station while its refresh is in progress does not trigger `toast.success` or `onRefreshed`

## Effort: Small-Medium
