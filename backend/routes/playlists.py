"""
Playlist sync routes — trigger M3U → Plex playlist sync and list results.
"""

import json
import logging
import threading
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter

router = APIRouter(prefix="/api/playlists", tags=["playlists"])
logger = logging.getLogger(__name__)

_LAST_SYNC_FILE = Path("/data/playlist_last_sync.json")

playlist_sync_status: dict = {
    "running": False,
    "last_sync": None,
}


def _load_last_sync():
    try:
        if _LAST_SYNC_FILE.exists():
            playlist_sync_status["last_sync"] = json.loads(_LAST_SYNC_FILE.read_text())
    except Exception:
        pass


_load_last_sync()


def _run_sync():
    from plex_playlist_sync import sync_all_m3u_playlists

    playlist_sync_status["running"] = True
    try:
        results = sync_all_m3u_playlists()
        last_sync = {
            "timestamp": datetime.now().isoformat(),
            "playlists": results,
        }
        playlist_sync_status["last_sync"] = last_sync
        _LAST_SYNC_FILE.write_text(json.dumps(last_sync))
    except Exception as e:
        logger.error(f"Playlist sync failed: {e}")
        playlist_sync_status["last_sync"] = {
            "timestamp": datetime.now().isoformat(),
            "error": str(e),
        }
    finally:
        playlist_sync_status["running"] = False


@router.post("/sync")
def start_sync():
    if playlist_sync_status["running"]:
        return {"ok": False, "error": "Playlist sync already running"}
    t = threading.Thread(target=_run_sync, daemon=True, name="playlist-sync")
    t.start()
    return {"ok": True}


@router.get("")
def get_playlists():
    return {
        "running": playlist_sync_status["running"],
        "last_sync": playlist_sync_status["last_sync"],
    }
