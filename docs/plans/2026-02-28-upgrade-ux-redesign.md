# Upgrade UX Redesign — Design Doc
**Date:** 2026-02-28
**Status:** Approved

## Problem

The upgrade scan is unreliable on a 5k–20k mixed library: it either hangs indefinitely or completes with no results. When results do appear, the workflow for reviewing them is painful — a flat table with no visibility into what's been scanned, no way to filter by quality tier, and no efficient way to evaluate matches when many are false positives.

## Goals

1. Fix scan reliability — scoped, batchable runs instead of one massive job
2. Show scan coverage — users can see what's been searched vs not
3. Fast review workflow — card-based review mode with keyboard shortcuts and bulk actions

---

## Section 1: Targeted Scan Launcher

**Replace** the current "Scan All" button with a **scan launcher modal** offering:

- **Format filter:** All lossy / MP3 only / AAC only / CD-FLAC (hi-res upgrade)
- **Unscanned only toggle** (on by default) — skips tracks already in upgrade_queue
- **Batch size:** default 50 albums per run (also configurable in Settings)
- **Artist/album text filter** — optional, to target specific music

The scan runs the existing backend ThreadPoolExecutor logic but only against the scoped candidate set. Progress shows: `Scanning 234 of 891 candidates — 12 found so far`. A **Cancel** button stops the job cleanly after the current batch.

**Why this fixes the hang:** Instead of a single 6,000-track job that can stall, the user runs 50-album batches that complete in minutes and can be scoped by format or artist.

---

## Section 2: Scan Status Visibility

**Coverage summary bar** at the top of the Upgrades page:
```
Library scan coverage: 1,240 scanned · 847 never scanned · 312 found · 89 completed
```
Clicking "847 never scanned" filters the list to unscanned tracks.

**"Unscanned" filter tab** added to the existing tab row (`all | found | approved | completed | skipped | unscanned`). Shows tracks from the library with no upgrade_queue entry, displaying format and bitrate. Each row has a **"Scan this album"** button that fires a targeted scan for just that album.

**Implementation note:** No schema changes needed. Unscanned list = LEFT JOIN of `tracks` against `upgrade_queue` on `track_id IS NULL` filtered to active lossy/CD-FLAC tracks.

---

## Section 3: Quick Review Mode

A **Review Mode** toggle on the "found" tab switches from table to focused card view — one card at a time:

```
[3 of 23 found]                          [Exit Review Mode]

  The Beatles — Abbey Road
  ─────────────────────────────────────────────
  You Never Give Me Your Money

  Current:  MP3  · 192 kbps
  Match:    FLAC · 24-bit / 96 kHz  [HI-RES]
  Source:   SomeUser123 · 287 MB
  File:     The Beatles - Abbey Road - 07 - You Never Give Me Your Money.flac

  [S Skip]  [A Approve]  →
```

**Keyboard shortcuts:**
- `A` or `Space` — approve and advance
- `S` or `X` — skip and advance
- `←` / `→` — navigate without acting

**"Approve all hi-res" bulk action** at the top of the found tab (available in both table and review mode) — approves every `match_quality = 'hi_res'` item in one click, leaving lossless-only matches for manual review.

Table mode remains the default; review mode is opt-in.

---

## What's Not Changing

- Download execution pipeline (slskd → SCP → import) — no changes
- Quality gate (FLAC→FLAC must be strictly better resolution)
- Existing approve/skip/download-approved actions
- WebSocket/polling progress display during downloads

---

## Files Affected

**Backend:**
- `backend/routes/upgrades.py` — add scoped scan endpoint, unscanned query endpoint, approve-all-hi-res endpoint
- `backend/routes/scan.py` — extract batch size / format filter params

**Frontend:**
- `frontend/src/pages/Upgrades.tsx` — scan launcher modal, coverage bar, unscanned tab, review mode cards
- `frontend/src/hooks/useUpgradeStatus.ts` — coverage summary fetch
