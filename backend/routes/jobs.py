import logging

from fastapi import APIRouter, HTTPException

from database import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


@router.get("")
@router.get("/")
def list_jobs():
    """Return recent jobs ordered by created_at descending, limited to 100."""
    with get_db() as db:
        rows = db.execute(
            """SELECT id, job_type, status, created_at, updated_at, error_msg, details
               FROM jobs
               ORDER BY created_at DESC
               LIMIT 100"""
        ).fetchall()
    return [dict(r) for r in rows]


@router.post("/{job_id}/retry")
def retry_job(job_id: int):
    """
    Retry a failed job. Currently supports upgrade_search and upgrade_download.
    Resets the relevant queue items and triggers the background worker.
    """
    with get_db() as db:
        job = db.execute(
            "SELECT id, job_type, status FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()

    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job["status"] != "failed":
        return {"ok": False, "error": f"Job is not in failed state (current: {job['status']})"}

    job_type = job["job_type"]

    if job_type == "upgrade_search":
        # Reset failed/skipped queue items so they get searched again
        with get_db() as db:
            db.execute(
                """UPDATE upgrade_queue
                   SET status = 'pending',
                       error_msg = NULL,
                       updated_at = CURRENT_TIMESTAMP
                   WHERE status IN ('failed', 'skipped')"""
            )
            db.execute(
                "UPDATE jobs SET status = 'retrying', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (job_id,),
            )

        import threading
        from routes.upgrades import _run_upgrade_search_worker

        t = threading.Thread(target=_run_upgrade_search_worker, daemon=True)
        t.start()
        return {"ok": True, "job_type": job_type, "action": "search worker started"}

    elif job_type == "upgrade_download":
        # Re-queue approved items that failed
        with get_db() as db:
            db.execute(
                """UPDATE upgrade_queue
                   SET status = 'approved',
                       error_msg = NULL,
                       updated_at = CURRENT_TIMESTAMP
                   WHERE status = 'failed'"""
            )
            db.execute(
                "UPDATE jobs SET status = 'retrying', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (job_id,),
            )

        import threading
        from routes.upgrades import _run_download_worker

        t = threading.Thread(target=_run_download_worker, daemon=True)
        t.start()
        return {"ok": True, "job_type": job_type, "action": "download worker started"}

    elif job_type == "scan":
        import threading
        import os
        from pathlib import Path
        from routes.scan import run_scan

        with get_db() as db:
            db.execute(
                "UPDATE jobs SET status = 'retrying', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (job_id,),
            )

        music_path = Path(os.environ.get("MUSIC_PATH", "/music"))
        t = threading.Thread(target=run_scan, args=(music_path,), daemon=True)
        t.start()
        return {"ok": True, "job_type": job_type, "action": "scan started"}

    else:
        return {"ok": False, "error": f"Retry not supported for job type: {job_type}"}
