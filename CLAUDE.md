# Music Machine (M²)

Music library deduplication & quality upgrade tool for Plex.

## Stack

Python 3.12, FastAPI, SQLite, React 19, Vite 7, Tailwind CSS v4

## Commands

- Backend tests: `PYTHONPATH=backend pytest backend/tests/ -v`
- Frontend dev: `cd frontend && npm run dev`
- Frontend build: `cd frontend && npm run build`
- Docker build: `docker compose build`
- Full rebuild: `docker compose build --no-cache && docker compose up -d`

## Deploy (from dev machine)

```bash
ssh-add --apple-load-keychain 2>/dev/null
rsync -av frontend/src/ sunygxc@10.0.0.75:/home/sunygxc/projects/music-machine/frontend/src/
ssh vm101 "cd /home/sunygxc/projects/music-machine && docker compose build --no-cache && docker compose up -d"
```

Note: `ssh-add --apple-load-keychain` must run in the same shell session before SSH commands.

### Fast Deploy (frontend-only, no rebuild)

```bash
npm --prefix frontend run build
scp frontend/dist/index.html frontend/dist/assets/*.{js,css} sunygxc@10.0.0.75:~/projects/music-machine/frontend/dist/...
ssh vm101 'docker restart music-machine'
```

For backend Python changes: scp the .py file + `docker restart music-machine` (no rebuild needed if not adding new deps).

## Hard Constraints

### Docker Layer Caching

`docker compose up -d --build` almost always uses cached frontend layers even after `scp`ing new files. **Must use `docker compose build --no-cache`** to force a full frontend rebuild. Do this every time when deploying frontend changes.

### .dockerignore

Without `.dockerignore`, `COPY frontend/ ./` copies host `node_modules/` over `npm ci`-installed ones, breaking `tsc`. Always exclude `node_modules` and `dist` from Docker context.

### Cross-Filesystem File Moves

`Path.rename()` fails across filesystems (container → NFS). Must use `shutil.move()`. Reorder: place FLAC first, then trash original (prevents orphans on failure).

### fetch() Error Handling

JS `fetch()` only rejects on network errors, NOT HTTP errors (4xx/5xx). Always check `res.ok` before updating UI state.

### Optimistic Flag Clearing

`downloadRequested`/`searchRequested` flags are cleared by phase transition effects. Do NOT add a "safety valve" that clears flags when backend is idle — it fires immediately because the effect re-runs before the first poll confirms the backend started, causing flash-and-disappear bugs.

### Fast Background Tasks

If a BackgroundTask completes faster than the polling interval (2s), the frontend never sees the active phase. Always pre-validate before spawning background tasks and return errors synchronously.

### docker cp + Python Modules

`docker cp` updates .py files but Python's `sys.modules` cache holds old bytecode. Changes only take effect after `docker restart`. Hot-patching only works for files loaded fresh per-request.

### User Preference

No automated file actions — user wants to review all duplicate resolutions manually.

## Architecture

- `backend/` — FastAPI app
  - `scanner.py` — audio tag reading (mutagen) and fingerprinting (chromaprint)
  - `dedup.py` — metadata grouping, quality ranking, duplicate detection
  - `file_manager.py` — trash/restore/empty file operations
  - `upgrade_service.py` — MusicGrabber API client (Monochrome/Tidal search + FLAC download)
  - `database.py` — SQLite schema and connection management (`sqlite3.connect(timeout=30)`)
  - `reorg_worker.py` — file reorg with DB path updates (must UPDATE tracks after shutil.move)
  - `routes/` — FastAPI routers (scan, dupes, trash, stats, upgrades, settings, reorg, jobs)
- `frontend/` — React SPA (Vite + Tailwind)
  - `pages/` — Dashboard, Duplicates, Upgrades, Trash, Library, Settings, JobLog
  - `hooks/` — useScanProgress (polling 2s), useUpgradeStatus (polling 2s), useReorgStatus (polling 3s), ScanContext
  - `components/ui/` — GlassCard, Button, Badge, Skeleton, StatCard, Modal, EmptyState, ProgressBar, Toast

## Docker

- Single container, multi-stage build (Node frontend → Python backend)
- SQLite at `/data/music-machine.db`
- Music library at `/music` (NFS, read-write for trash moves)
- Trash at `/trash` (`/mnt/music_dupes` on VM 101, local disk NOT NAS)
- Port 8686

## Key Patterns

- **Quality scoring:** lossless +10000 base, bit_depth × 100, sample_rate ÷ 100, bitrate
- **Dedup:** groups by (normalized_artist, normalized_title, normalized_album)
- **Scan phases:** counting → scanning → cleaning → analyzing → complete (upgrade search is separate)
- **Scan auto-runs** dupe analysis on completion — no separate analyze step needed
- **All background tasks** use try/finally to always reset status on crash
- **Frontend uses polling** (2s) not WebSocket — `_broadcast_sync` can't push from threadpool to async loop
- **SPA routing:** Starlette `StaticFiles(html=True)` doesn't handle client routes. Fix: mount `/assets` with StaticFiles, catch-all `@app.get("/{full_path:path}")` returns `FileResponse(index.html)`
- **UX rule:** Every user action must show immediate feedback. Never use Button auto-state for long-running ops — use local `*Requested` flags that bridge the gap before backend confirms via polling

## MusicGrabber Integration

**URL:** `http://10.0.0.75:38274` (same VM 101)
**Source:** Monochrome API (`api.monochrome.tf`) — free Tidal frontend, no subscription
**Quality cascade:** HI_RES_LOSSLESS → LOSSLESS → HIGH → LOW

- **Search:** `POST /api/search` with `{"query": "...", "source": "monochrome", "limit": 10}`
- **Download:** `POST /api/download` with `{"video_id": ..., "source": "monochrome", "source_url": "...", "convert_to_flac": true}`
- **Job status:** `GET /api/jobs/{job_id}` → `{status: "queued"|"downloading"|"completed"|"failed"}`
- **429 retry:** `search_for_flac()` retries up to 8× with base-3 exponential backoff (2/4/10/28s, capped at 30s), uses async sleep
- **Concurrency:** `upgrade_concurrency` setting default 2 (reduced from 8 to avoid 429s)
- **Inter-search throttle:** 1.5s delay between tracks within an album group
- **Retry skipped:** `POST /api/upgrades/retry-skipped` re-queues rate-limited tracks for next scan
- **Download pipeline:** 2-step (downloading → importing)
- **File finding:** `_find_musicgrabber_download()` searches Singles dir → artist dir → broad mtime-based search (5min window)

## Design

- **Theme:** Dark slate base, glassmorphism cards, amber/gold (#d4a017) accent
- **Fonts:** Syne (headings) + Outfit (body) via Google Fonts
- **Layout:** Fixed 220px sidebar
- **Deps:** lucide-react, motion, react-hot-toast, recharts
- **Copyright:** 2026 Shawnee Digital

## Plex Library Scan

After upgrades/reorg, trigger: `GET http://10.0.0.13:32400/library/sections/5/refresh?X-Plex-Token=mxrEzLiMjZ1FftGMZaiq`
