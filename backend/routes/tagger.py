import asyncio
import json
import logging
import os
import threading
import time
from pathlib import Path

from fastapi import APIRouter

from database import get_db
from ws_manager import manager
from tagger import tag_directory

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/tagger", tags=["tagger"])

# Global tagger state
tagger_status = {
    "running": False,
    "phase": "idle",
    "processed": 0,
    "total": 0,
    "tagged": 0,
    "failed": 0,
    "skipped": 0,
    "current_file": None,
    "elapsed_s": 0,
    "started_at": None,
}

_tagger_lock = threading.Lock()
_event_loop: asyncio.AbstractEventLoop | None = None


def set_event_loop(loop: asyncio.AbstractEventLoop):
    global _event_loop
    _event_loop = loop


def _broadcast_sync(msg_type: str, data: dict):
    loop = _event_loop
    if loop is None or not loop.is_running():
        return
    try:
        asyncio.run_coroutine_threadsafe(manager.broadcast(msg_type, data), loop)
    except Exception as e:
        logger.debug(f"Broadcast error: {e}")


def _update_status(**kwargs):
    tagger_status.update(kwargs)
    if tagger_status.get("started_at"):
        tagger_status["elapsed_s"] = int(time.time() - tagger_status["started_at"])
    _broadcast_sync("tagger_progress", dict(tagger_status))


def _run_tagger(music_path: Path, force: bool = False, dry_run: bool = False):
    """Background thread: runs tagger pipeline."""
    if not _tagger_lock.acquire(blocking=False):
        logger.warning("Tagger already in progress, skipping")
        return

    # Create job record
    job_id = None
    try:
        with get_db() as db:
            cursor = db.execute(
                "INSERT INTO jobs (job_type, status) VALUES ('tagger', 'running')"
            )
            job_id = cursor.lastrowid
    except Exception as e:
        logger.error(f"Failed to create tagger job: {e}")

    tagger_status.update({
        "running": True,
        "phase": "scanning",
        "processed": 0,
        "total": 0,
        "tagged": 0,
        "failed": 0,
        "skipped": 0,
        "current_file": None,
        "elapsed_s": 0,
        "started_at": time.time(),
    })
    _broadcast_sync("tagger_progress", dict(tagger_status))

    try:
        for update in tag_directory(str(music_path), force=force, dry_run=dry_run):
            utype = update.get("type")

            if utype == "error":
                raise ValueError(update["error"])

            elif utype == "count":
                _update_status(total=update["total"], phase="tagging")

            elif utype == "progress":
                _update_status(
                    processed=update["processed"],
                    current_file=update.get("current_file"),
                )

            elif utype == "result":
                result = update["result"]
                status = result["status"]

                if status == "tagged":
                    tagger_status["tagged"] += 1
                elif status == "failed":
                    tagger_status["failed"] += 1
                elif status == "skipped":
                    tagger_status["skipped"] += 1

                tagger_status["processed"] = tagger_status.get("processed", 0) + 1
                _update_status()

                # Record to database
                try:
                    with get_db() as db:
                        # Find track_id if exists
                        track_row = db.execute(
                            "SELECT id FROM tracks WHERE file_path = ?",
                            (result["file_path"],),
                        ).fetchone()
                        track_id = track_row["id"] if track_row else None

                        db.execute(
                            """INSERT INTO tag_jobs
                               (track_id, file_path, status, acoustid_score,
                                mb_recording_id, mb_release_id,
                                matched_artist, matched_title, matched_album,
                                cover_art_url, error_msg)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (
                                track_id,
                                result["file_path"],
                                result["status"],
                                result.get("acoustid_score"),
                                result.get("mb_recording_id"),
                                result.get("mb_release_id"),
                                result.get("matched_artist"),
                                result.get("matched_title"),
                                result.get("matched_album"),
                                result.get("cover_art_url"),
                                result.get("error_msg"),
                            ),
                        )

                        # Record file_transaction if file was modified
                        if status == "tagged" and track_id:
                            db.execute(
                                """INSERT INTO file_transactions
                                   (track_id, action, source_path, dest_path,
                                    sha256_before, sha256_after)
                                   VALUES (?, 'tag', ?, ?, ?, ?)""",
                                (
                                    track_id,
                                    result["file_path"],
                                    result["file_path"],
                                    result.get("sha256_before"),
                                    result.get("sha256_after"),
                                ),
                            )
                except Exception as e:
                    logger.warning(f"Failed to record tag result: {e}")

        # Complete
        if job_id:
            with get_db() as db:
                db.execute(
                    "UPDATE jobs SET status='completed', "
                    "details=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (
                        json.dumps({"tagged": tagger_status["tagged"], "failed": tagger_status["failed"], "skipped": tagger_status["skipped"]}),
                        job_id,
                    ),
                )

        _update_status(phase="complete", current_file=None, running=False)
        _broadcast_sync("stats_update", {"event": "tagger_complete"})

    except Exception as e:
        logger.error(f"Tagger failed: {e}", exc_info=True)
        tagger_status["running"] = False
        tagger_status["phase"] = "failed"
        _broadcast_sync("tagger_progress", dict(tagger_status))

        if job_id:
            try:
                with get_db() as db:
                    db.execute(
                        "UPDATE jobs SET status='failed', error_msg=?, "
                        "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (str(e), job_id),
                    )
            except Exception:
                pass

    finally:
        tagger_status["running"] = False
        _tagger_lock.release()


@router.post("/run")
async def start_tagger(
    path: str | None = None,
    force: bool = False,
    dry_run: bool = False,
):
    """Start the tagger pipeline in a background thread."""
    if tagger_status["running"]:
        return {"ok": False, "error": "Tagger already in progress"}

    music_path = Path(path) if path else Path(os.environ.get("MUSIC_PATH", "/music"))
    t = threading.Thread(
        target=_run_tagger,
        args=(music_path, force, dry_run),
        daemon=True,
    )
    t.start()
    return {"ok": True}


@router.get("/status")
def get_tagger_status():
    """Return current tagger status."""
    return {
        "running": tagger_status["running"],
        "phase": tagger_status["phase"],
        "processed": tagger_status["processed"],
        "total": tagger_status["total"],
        "tagged": tagger_status["tagged"],
        "failed": tagger_status["failed"],
        "skipped": tagger_status["skipped"],
        "current_file": tagger_status["current_file"],
        "elapsed_s": tagger_status["elapsed_s"],
    }


@router.get("/results")
def get_tagger_results(status: str | None = None, limit: int = 200):
    """Return tag_jobs results, optionally filtered by status."""
    with get_db() as db:
        if status:
            rows = db.execute(
                "SELECT * FROM tag_jobs WHERE status = ? ORDER BY updated_at DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM tag_jobs ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return [dict(r) for r in rows]


@router.post("/{job_id}/retry")
def retry_tag_job(job_id: int):
    """Retry a failed tag job."""
    with get_db() as db:
        row = db.execute(
            "SELECT file_path FROM tag_jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if not row:
            return {"ok": False, "error": "Job not found"}

        file_path = row["file_path"]
        if not Path(file_path).exists():
            return {"ok": False, "error": "File no longer exists"}

    from tagger import tag_file

    result = tag_file(file_path, force=True)

    with get_db() as db:
        db.execute(
            """UPDATE tag_jobs SET
               status=?, acoustid_score=?, mb_recording_id=?, mb_release_id=?,
               matched_artist=?, matched_title=?, matched_album=?,
               cover_art_url=?, error_msg=?, updated_at=CURRENT_TIMESTAMP
               WHERE id=?""",
            (
                result["status"],
                result.get("acoustid_score"),
                result.get("mb_recording_id"),
                result.get("mb_release_id"),
                result.get("matched_artist"),
                result.get("matched_title"),
                result.get("matched_album"),
                result.get("cover_art_url"),
                result.get("error_msg"),
                job_id,
            ),
        )

    return {"ok": True, "status": result["status"]}


@router.post("/{job_id}/skip")
def skip_tag_job(job_id: int):
    """Mark a tag job as skipped."""
    with get_db() as db:
        db.execute(
            "UPDATE tag_jobs SET status='skipped', updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (job_id,),
        )
    return {"ok": True}
