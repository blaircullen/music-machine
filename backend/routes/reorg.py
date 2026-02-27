"""
Library Reorg route — trigger plex-reorg-style reorganization from the UI
and expose last-run status.
"""

import json
import threading
import time
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter

router = APIRouter(prefix="/api/reorg", tags=["reorg"])

_LAST_RUN_FILE = Path("/data/reorg_last_run.json")

reorg_status: dict = {
    "running": False,
    "phase": "idle",
    "total": 0,
    "progress": 0,
    "current_file": "",
    "elapsed_s": 0,
    "moved": 0,
    "skipped": 0,
    "errors": 0,
    "already_ok": 0,
    "inbox_moved": 0,
    "last_run": None,
}


def _load_last_run():
    """Load persisted last-run result from disk."""
    try:
        if _LAST_RUN_FILE.exists():
            reorg_status["last_run"] = json.loads(_LAST_RUN_FILE.read_text())
    except Exception:
        pass


_load_last_run()


def _run_reorg_worker():
    from reorg_worker import run_reorg

    start = time.time()
    reorg_status.update({
        "running": True,
        "phase": "scanning",
        "total": 0,
        "progress": 0,
        "current_file": "",
        "elapsed_s": 0,
        "moved": 0,
        "skipped": 0,
        "errors": 0,
        "already_ok": 0,
        "inbox_moved": 0,
    })

    def update(data: dict):
        reorg_status.update(data)
        reorg_status["elapsed_s"] = int(time.time() - start)
        reorg_status["running"] = True

    try:
        stats = run_reorg(update_fn=update)
        last_run = {
            "timestamp": datetime.now().isoformat(),
            "elapsed_s": int(time.time() - start),
            **stats,
        }
        reorg_status["last_run"] = last_run
        reorg_status["phase"] = "complete"
        _LAST_RUN_FILE.write_text(json.dumps(last_run))
    except Exception as e:
        reorg_status["phase"] = "failed"
        reorg_status["last_run"] = {
            "timestamp": datetime.now().isoformat(),
            "error": str(e),
        }
    finally:
        reorg_status["running"] = False
        reorg_status["elapsed_s"] = int(time.time() - start)


@router.post("/start")
def start_reorg():
    if reorg_status["running"]:
        return {"ok": False, "error": "Reorg already running"}
    t = threading.Thread(target=_run_reorg_worker, daemon=True, name="reorg-worker")
    t.start()
    return {"ok": True}


@router.get("/status")
def get_reorg_status():
    return dict(reorg_status)
