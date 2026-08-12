---
status: pending
priority: p3
issue_id: "013"
tags: [quality, frontend, code-review]
---

# StationCard.handleRefresh Fetches All Stations Instead of One

## Problem Statement
After a single station refreshes, `StationCard` calls `getStations()` (full list) to get the updated `track_count`. There's already a `GET /api/stations/{station_id}` endpoint.

## Findings
- **File:** `frontend/src/pages/Stations.tsx:403-406`
```ts
const stations = await getStations()
const updated = stations.find(s => s.id === station.id)
```

## Fix
```ts
const updated = await getStation(station.id)
```

## Effort: Tiny
