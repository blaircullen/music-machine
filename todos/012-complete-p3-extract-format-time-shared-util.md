---
status: pending
priority: p3
issue_id: "012"
tags: [quality, frontend, code-review]
---

# Duplicate formatTime/formatDuration — Extract to Shared Util

## Problem Statement
`Player.tsx:5` defines `formatTime` and `Stations.tsx:80` defines `formatDuration` — identical logic, different names. Will diverge over time.

## Fix
Create `frontend/src/lib/utils.ts`:
```ts
export function formatDuration(secs: number): string {
  if (!isFinite(secs) || isNaN(secs)) return '0:00'
  const m = Math.floor(secs / 60)
  const s = Math.floor(secs % 60)
  return `${m}:${s.toString().padStart(2, '0')}`
}
```
Import in both files. Remove the local definitions.

## Effort: Tiny
