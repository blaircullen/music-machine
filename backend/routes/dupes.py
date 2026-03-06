import logging
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException

from database import get_db
from file_manager import trash_file
from scanner import quality_score

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/dupes", tags=["dupes"])


def _resolve_group_internal(group_id: int, keep_track_id: int) -> int:
    """
    Internal: trash all members of a dupe group except the keeper.
    Records file_transactions for each trashed file.
    Returns number of files moved to trash.
    """
    trash_root = os.environ.get("TRASH_PATH", "/trash")
    music_root = os.environ.get("MUSIC_PATH", "/music")

    with get_db() as db:
        members = db.execute(
            "SELECT track_id FROM dupe_group_members WHERE group_id = ?",
            (group_id,),
        ).fetchall()
        member_ids = [m["track_id"] for m in members]

        moved = 0
        for tid in member_ids:
            if tid == keep_track_id:
                continue
            track = db.execute(
                "SELECT * FROM tracks WHERE id = ? AND status = 'active'", (tid,)
            ).fetchone()
            if not track:
                continue

            file_path = track["file_path"]
            if not Path(file_path).exists():
                # File already gone — just mark as deleted
                db.execute(
                    "UPDATE tracks SET status = 'deleted' WHERE id = ?", (tid,)
                )
                continue

            try:
                dest = trash_file(file_path, trash_root, music_root)
                db.execute(
                    "UPDATE tracks SET status = 'trashed' WHERE id = ?", (tid,)
                )
                db.execute(
                    """INSERT INTO file_transactions
                       (track_id, action, source_path, dest_path, state)
                       VALUES (?, 'trash', ?, ?, 'committed')""",
                    (tid, file_path, dest),
                )
                moved += 1
            except FileNotFoundError:
                # NFS stale cache: exists() returned True but file is gone — treat as deleted
                logger.warning(f"Track {tid} not found at {file_path} during trash — marking deleted")
                db.execute("UPDATE tracks SET status = 'deleted' WHERE id = ?", (tid,))
            except Exception as e:
                logger.error(f"Failed to trash track {tid} at {file_path}: {e}")
                raise

        db.execute(
            "UPDATE dupe_groups SET resolved = 1, kept_track_id = ? WHERE id = ?",
            (keep_track_id, group_id),
        )

    return moved


@router.get("")
@router.get("/")
def list_dupes():
    """
    Return all dupe groups with full track info.
    Each group includes tracks sorted by quality_score desc, with is_winner flag.
    """
    with get_db() as db:
        groups = db.execute(
            """SELECT dg.id, dg.match_type, dg.confidence, dg.resolved, dg.kept_track_id,
                      GROUP_CONCAT(dgm.track_id) as member_ids
               FROM dupe_groups dg
               JOIN dupe_group_members dgm ON dg.id = dgm.group_id
               GROUP BY dg.id
               ORDER BY dg.resolved ASC, dg.confidence DESC"""
        ).fetchall()

        result = []
        for g in groups:
            raw_ids = g["member_ids"] or ""
            member_ids = [int(x) for x in raw_ids.split(",") if x.strip()]
            if not member_ids:
                continue

            placeholders = ",".join("?" * len(member_ids))
            tracks = db.execute(
                f"SELECT * FROM tracks WHERE id IN ({placeholders})",
                member_ids,
            ).fetchall()

            track_list = []
            for t in tracks:
                td = dict(t)
                td["quality_score"] = quality_score(td)
                td["is_winner"] = (td["id"] == g["kept_track_id"])
                track_list.append(td)

            # Sort by quality score descending
            track_list.sort(key=lambda t: t["quality_score"], reverse=True)

            result.append({
                "id": g["id"],
                "confidence": g["confidence"],
                "match_type": g["match_type"],
                "resolved": bool(g["resolved"]),
                "tracks": track_list,
            })

    return result


@router.post("/{group_id}/resolve")
def resolve_dupe(group_id: int):
    """
    Resolve a dupe group by trashing losers. The winner is already recorded
    in dupe_groups.kept_track_id from the analysis phase.
    """
    with get_db() as db:
        group = db.execute(
            "SELECT id, kept_track_id, resolved FROM dupe_groups WHERE id = ?",
            (group_id,),
        ).fetchone()

    if not group:
        raise HTTPException(status_code=404, detail="Dupe group not found")
    if group["resolved"]:
        return {"ok": True, "moved": 0, "already_resolved": True}

    moved = _resolve_group_internal(group_id, group["kept_track_id"])
    return {"ok": True, "moved": moved}


@router.post("/resolve-all")
def resolve_all_dupes():
    """Resolve all unresolved dupe groups."""
    with get_db() as db:
        groups = db.execute(
            "SELECT id, kept_track_id FROM dupe_groups WHERE resolved = 0"
        ).fetchall()

    resolved = 0
    errors = 0
    for g in groups:
        try:
            _resolve_group_internal(g["id"], g["kept_track_id"])
            resolved += 1
        except Exception as e:
            logger.error(f"resolve-all: failed group {g['id']}: {e}")
            errors += 1

    return {"resolved": resolved, "errors": errors}
