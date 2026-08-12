---
status: pending
priority: p2
issue_id: "009"
tags: [bug, frontend, safari, code-review]
---

# audio.play() Called Before HAVE_FUTURE_DATA — Safari Rejection

## Problem Statement
In `usePlayer.ts`, the track-loading effect calls `audio.src = url; audio.load(); audio.play()` synchronously. The `play()` call fires when the audio element is in `HAVE_NOTHING` state (no data received yet). Chrome and Firefox usually queue the play intent internally, but Safari is strict: it may reject `play()` with `NotAllowedError` or silently swallow it. The `.catch()` handler sets `playing: false`, leaving the user with a loaded track that doesn't auto-play. They press Play and it works — but the first-track experience is broken on Safari/iOS.

## Findings
- **File:** `frontend/src/hooks/usePlayer.ts:143-151`
```ts
audio.src = track.stream_url
audio.load()
setState(s => ({ ...s, currentTime: 0, duration: 0, buffering: true }))
if (wasPlaying || state.currentIndex > 0) {
  audio.play().catch(() => {   // ← called at HAVE_NOTHING on Safari
    setState(s => ({ ...s, playing: false, buffering: false }))
  })
}
```
- The `canplay` event fires when `HAVE_FUTURE_DATA` is reached — correct moment to call `play()`

## Proposed Solution

**Option A (Recommended) — Defer play() to canplay:**
```ts
audio.src = track.stream_url
audio.load()
setState(s => ({ ...s, currentTime: 0, duration: 0, buffering: true }))

if (wasPlaying || state.currentIndex > 0) {
  const playOnReady = () => {
    audio.removeEventListener('canplay', playOnReady)
    audio.play().catch(() => setState(s => ({ ...s, playing: false, buffering: false })))
  }
  audio.addEventListener('canplay', playOnReady, { once: true })
}
```

**Option B — Use a `playIntended` ref:** Set `playIntendedRef.current = true` before load, have the existing `canplay` listener call `play()` if the ref is set. Cleaner if the canplay listener is already named.

## Acceptance Criteria
- [ ] On Safari iOS, navigating to a station and pressing play auto-advances correctly
- [ ] First track auto-plays without user needing to press Play twice
- [ ] Chrome/Firefox behavior unchanged

## Effort: Small
