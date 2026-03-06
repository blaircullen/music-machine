import logging
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException

from database import get_db
from file_manager import empty_trash, restore_file

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/trash", tags=["trash"])


@router.get("")
@router.get("/")
def list_trash():
    """
    Return all trashed files from file_transactions joined with tracks.
    """
    with get_db() as db:
        rows = db.execute(
            """SELECT ft.id, ft.track_id, ft.source_path as original_path,
                      ft.dest_path as trash_path, ft.performed_at as moved_at,
                      t.artist, t.album, t.title, t.format
               FROM file_transactions ft
               JOIN tracks t ON ft.track_id = t.id
               WHERE ft.action IN ('trash', 'upgrade')
                 AND ft.state = 'committed'
                 AND t.status IN ('trashed', 'upgraded')
               ORDER BY ft.performed_at DESC"""
        ).fetchall()

    result = []
    for row in rows:
        item = dict(row)
        # Add file_size from disk if the trash file still exists
        trash_path = item.get("trash_path") or ""
        try:
            if trash_path and Path(trash_path).exists():
                item["file_size"] = Path(trash_path).stat().st_size
            else:
                item["file_size"] = None
        except Exception:
            item["file_size"] = None
        result.append(item)

    return result


@router.post("/{transaction_id}/restore")
def restore_trash(transaction_id: int):
    """Restore a trashed file to its original location."""
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM file_transactions WHERE id = ?", (transaction_id,)
        ).fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Transaction not found")

    trash_path = row["dest_path"]
    original_path = row["source_path"]

    if not trash_path or not Path(trash_path).exists():
        raise HTTPException(
            status_code=400,
            detail=f"Trash file not found at: {trash_path}",
        )

    success = restore_file(trash_path, original_path)
    if not success:
        raise HTTPException(status_code=500, detail="Restore failed")

    with get_db() as db:
        db.execute(
            "UPDATE tracks SET status = 'active' WHERE id = ?", (row["track_id"],)
        )
        db.execute(
            "UPDATE file_transactions SET state = 'rolled_back' WHERE id = ?",
            (transaction_id,),
        )

    return {"ok": True, "restored_to": original_path}


@router.post("/empty")
def empty_trash_endpoint():
    """Permanently delete all files in trash. Returns count deleted."""
    trash_root = os.environ.get("TRASH_PATH", "/trash")

    try:
        deleted = empty_trash(trash_root)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to empty trash: {e}")

    # Mark all trashed tracks as deleted in the DB
    with get_db() as db:
        db.execute(
            "UPDATE tracks SET status = 'deleted' WHERE status = 'trashed'"
        )
        # Mark transactions as rolled_back (files are gone)
        db.execute(
            """UPDATE file_transactions
               SET state = 'rolled_back'
               WHERE action IN ('trash', 'upgrade')
                 AND state = 'committed'"""
        )

    return {"ok": True, "deleted": deleted}


@router.get("/stats")
def trash_stats():
    """Return count and total size of trashed files."""
    trash_root = os.environ.get("TRASH_PATH", "/trash")
    trash = Path(trash_root)

    count = 0
    size_bytes = 0

    if trash.exists():
        for f in trash.rglob("*"):
            if f.is_file():
                try:
                    size_bytes += f.stat().st_size
                    count += 1
                except Exception:
                    pass

    return {"count": count, "size_bytes": size_bytes}
