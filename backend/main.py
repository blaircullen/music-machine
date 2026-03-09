import asyncio
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from database import init_db
from ws_manager import manager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def _scheduled_scan_loop():
    """Run a full library scan daily at 1 AM."""
    from routes.scan import run_scan, scan_status

    while True:
        now = datetime.now()
        target = now.replace(hour=1, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        wait_seconds = (target - now).total_seconds()
        logger.info(
            f"Next scheduled scan at {target.isoformat()}, sleeping {wait_seconds:.0f}s"
        )
        time.sleep(wait_seconds)

        if scan_status["running"]:
            logger.info("Scheduled scan skipped — scan already in progress")
            continue

        logger.info("Starting scheduled scan (1 AM daily)")
        music_path = Path(os.environ.get("MUSIC_PATH", "/music"))
        try:
            run_scan(music_path)
            logger.info("Scheduled scan complete")
        except Exception as e:
            logger.error(f"Scheduled scan failed: {e}")


def _scheduled_playlist_sync_loop():
    """Sync M3U playlists to Plex daily at 2 AM."""
    from routes.playlists import _run_sync

    while True:
        now = datetime.now()
        target = now.replace(hour=2, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        wait_seconds = (target - now).total_seconds()
        logger.info(
            f"Next scheduled playlist sync at {target.isoformat()}, sleeping {wait_seconds:.0f}s"
        )
        time.sleep(wait_seconds)

        logger.info("Starting scheduled playlist sync (2 AM daily)")
        try:
            _run_sync()
            logger.info("Scheduled playlist sync complete")
        except Exception as e:
            logger.error(f"Scheduled playlist sync failed: {e}")


def _scheduled_station_refresh_loop():
    """Refresh all Pandora stations daily at 6 AM."""
    from stations_service import refresh_all_stations

    while True:
        now = datetime.now()
        target = now.replace(hour=6, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        wait_seconds = (target - now).total_seconds()
        logger.info(
            f"Next station refresh at {target.isoformat()}, sleeping {wait_seconds:.0f}s"
        )
        time.sleep(wait_seconds)

        logger.info("Starting scheduled station refresh (6 AM daily)")
        try:
            refresh_all_stations()
            logger.info("Scheduled station refresh complete")
        except Exception as e:
            logger.error(f"Scheduled station refresh failed: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize database schema and defaults
    init_db()

    # Clean up orphaned 'running' jobs from previous process (crash/restart)
    try:
        from database import get_db
        with get_db() as db:
            cur = db.execute(
                "UPDATE jobs SET status='failed', error_msg='Orphaned: process restarted', "
                "updated_at=CURRENT_TIMESTAMP WHERE status='running'"
            )
            if cur.rowcount:
                logger.info(f"Cleaned up {cur.rowcount} orphaned running job(s)")
                # Reset any tracks left in mid-flight states by the crashed job.
                # 'searching' rows that already have mg_track_id set completed their
                # search before the crash — promote them to 'found' rather than losing the result.
                db.execute(
                    "UPDATE upgrade_queue SET status='found' "
                    "WHERE status='searching' AND mg_track_id IS NOT NULL"
                )
                db.execute(
                    "UPDATE upgrade_queue SET status='pending' "
                    "WHERE status IN ('searching', 'downloading')"
                )
                db.commit()
    except Exception as e:
        logger.warning(f"Failed to clean up orphaned jobs: {e}")

    # Capture the running event loop for use by background threads
    loop = asyncio.get_event_loop()

    # Inject event loop reference into route modules that need it
    from routes import scan as scan_mod, upgrades as upgrades_mod, tagger as tagger_mod
    scan_mod.set_event_loop(loop)
    upgrades_mod.set_event_loop(loop)
    tagger_mod.set_event_loop(loop)

    # Start the daily scheduled scan thread
    scheduler_thread = threading.Thread(
        target=_scheduled_scan_loop, daemon=True, name="scan-scheduler"
    )
    scheduler_thread.start()

    # Start the daily playlist sync thread
    playlist_sync_thread = threading.Thread(
        target=_scheduled_playlist_sync_loop, daemon=True, name="playlist-sync-scheduler"
    )
    playlist_sync_thread.start()

    # Start the daily station refresh thread
    station_refresh_thread = threading.Thread(
        target=_scheduled_station_refresh_loop, daemon=True, name="station-refresh-scheduler"
    )
    station_refresh_thread.start()

    logger.info("music-machine backend ready")
    yield
    logger.info("music-machine backend shutting down")


app = FastAPI(title="music-machine", version="2.0.0", lifespan=lifespan)

# Import and register all routers
from routes import scan, dupes, upgrades, trash, stats, jobs, settings, reorg, playlists, tagger, stations

app.include_router(scan.router)
app.include_router(dupes.router)
app.include_router(upgrades.router)
app.include_router(trash.router)
app.include_router(stats.router)
app.include_router(jobs.router)
app.include_router(settings.router)
app.include_router(reorg.router)
app.include_router(playlists.router)
app.include_router(tagger.router)
app.include_router(stations.router)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """
    WebSocket endpoint. Backend broadcasts JSON messages to all connected clients:
      {"type": "scan_progress"|"job_update"|"stats_update", "data": {...}}

    Clients can send any message to keep alive (we read and discard).
    """
    await manager.connect(websocket)
    try:
        while True:
            # Keep connection alive; client can send pings
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=30.0)
            except asyncio.TimeoutError:
                # Send a ping to detect dead connections
                try:
                    await websocket.send_text('{"type":"ping"}')
                except Exception:
                    break
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.debug(f"WebSocket error: {e}")
    finally:
        manager.disconnect(websocket)


@app.get("/api/health")
def health():
    return {"status": "ok", "version": "2.0.0"}


# Serve React frontend (present after multi-stage Docker build)
_frontend_dist = Path("/app/frontend/dist")
if _frontend_dist.exists():
    # Serve /assets/* as static files directly
    app.mount("/assets", StaticFiles(directory=str(_frontend_dist / "assets")), name="assets")

    # SPA catch-all: serve index.html for all non-API client-side routes
    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        file_path = _frontend_dist / full_path
        if file_path.is_file():
            return FileResponse(str(file_path))
        return FileResponse(str(_frontend_dist / "index.html"))
