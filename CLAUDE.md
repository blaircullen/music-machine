# plex-dedup

Music library deduplication & quality upgrade tool for Plex.

## Stack
Python 3.12, FastAPI, SQLite, React 18, Vite, Tailwind CSS

## Commands
- Backend tests: `PYTHONPATH=backend pytest backend/tests/ -v`
- Frontend dev: `cd frontend && npm run dev`
- Frontend build: `cd frontend && npm run build`
- Docker build: `docker compose build`
- Docker run: `docker compose up -d`
- Full rebuild: `docker compose build --no-cache && docker compose up -d`

## Deploy (from dev machine)
```bash
ssh-add --apple-load-keychain 2>/dev/null
rsync -av frontend/src/ sunygxc@10.0.0.75:/home/sunygxc/projects/plex-dedup/frontend/src/
ssh vm101 "cd /home/sunygxc/projects/plex-dedup && docker compose build --no-cache && docker compose up -d"
```
Note: `ssh-add --apple-load-keychain` must run in the same shell session before SSH commands.

## Architecture
- `backend/` — FastAPI app
  - `scanner.py` — audio tag reading (mutagen) and fingerprinting (chromaprint)
  - `dedup.py` — metadata grouping, quality ranking, duplicate detection
  - `file_manager.py` — trash/restore/empty file operations
  - `upgrade_service.py` — MusicGrabber API client (Monochrome/Tidal search + FLAC download)
  - `database.py` — SQLite schema and connection management
  - `routes/` — FastAPI routers (scan, dupes, trash, stats, upgrades, settings)
- `frontend/` — React SPA (Vite + Tailwind)
  - `pages/` — Dashboard, Duplicates, Upgrades, Trash, Settings
  - `hooks/` — useScanProgress (polling), useUpgradeStatus (polling), ScanContext

## Docker
- Single container, multi-stage build (Node frontend → Python backend)
- SQLite at `/data/plex-dedup.db`
- Music library mounted at `/music` (read-only)
- Trash at `/trash`
- Staging at `/staging`
- Port 8686

## Key Patterns
- quality_score: lossless +10000 base, bit_depth * 100, sample_rate / 100, bitrate
- normalize_text: lowercase, strip punctuation, collapse whitespace, remove leading "the"
- Dedup groups by (normalized_artist, normalized_title, normalized_album)
- Scan auto-runs dupe analysis on completion — no separate analyze step needed
- All background tasks (scan, search, download) use try/finally to reset status
- Frontend uses polling (2s) not WebSocket for progress — WebSocket can't push from threadpool
- Pre-validate before spawning background tasks — fast tasks finish before polling catches them
