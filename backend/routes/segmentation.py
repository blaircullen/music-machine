"""
Live & Holiday segmentation API (docs/live-holiday-split-spec.md §5C, §6, §8).

Review-first by policy: a dry-run records candidates (nothing moves); only auto-tier
candidates or explicitly-approved review-tier candidates are physically moved, and only when
the segmentation_move_enabled kill switch is on. Endpoints:

  POST /api/segmentation/dry-run           — classify scope, record candidates + manifest
  GET  /api/segmentation/status            — run heartbeat + last-run manifest
  GET  /api/segmentation/candidates        — list candidates (filter by status/tier)
  POST /api/segmentation/candidates/{id}/approve
  POST /api/segmentation/candidates/{id}/reject
  POST /api/segmentation/apply-approved    — move approved (and optionally auto) candidates
  POST /api/segmentation/undo/{ledger_id}  — reverse a single move
  POST /api/segmentation/undo-run/{run_id} — reverse a whole run
  GET  /api/segmentation/settings          — kill-switch state
  PUT  /api/segmentation/settings          — toggle the two kill switches
"""

import logging
import threading

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from database import get_db, segmentation_move_enabled, segmentation_sweep_enabled
import segmentation_mover as mover
import segmentation_service as svc

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/segmentation", tags=["segmentation"])


# ---------------------------------------------------------------------------
# Dry-run + status
# ---------------------------------------------------------------------------

def _dry_run_worker(limit):
    with svc.run_lock:
        svc.run_state.update({"running": True, "phase": "dry_run", "last_error": None})
        try:
            manifest = svc.run_dry_run(limit=limit)
            svc.run_state["run_id"] = manifest["run_id"]
        except Exception as e:
            svc.run_state["last_error"] = str(e)
            logger.error(f"segmentation dry-run failed: {e}")
        finally:
            svc.run_state.update({"running": False, "phase": "idle"})


@router.post("/dry-run")
def start_dry_run(limit: int | None = None):
    """Kick off a dry-run in the background. Records candidates + manifest; moves nothing."""
    if svc.run_state["running"]:
        return {"ok": False, "error": "a segmentation run is already in progress"}
    t = threading.Thread(target=_dry_run_worker, args=(limit,), daemon=True,
                         name="segmentation-dry-run")
    t.start()
    return {"ok": True, "started": True}


@router.get("/status")
def get_status():
    """Run heartbeat + last-run manifest (mirrors /data/segmentation_last_run.json §6)."""
    with get_db() as db:
        counts = db.execute(
            "SELECT status, confidence_tier, COUNT(*) AS n FROM segmentation_candidates "
            "GROUP BY status, confidence_tier"
        ).fetchall()
        pending = db.execute(
            "SELECT COUNT(*) FROM segmentation_candidates WHERE status='proposed'"
        ).fetchone()[0]
        approved = db.execute(
            "SELECT COUNT(*) FROM segmentation_candidates WHERE status='approved'"
        ).fetchone()[0]
        moved = db.execute(
            "SELECT COUNT(*) FROM segmentation_moves WHERE state='done' AND rolled_back=0"
        ).fetchone()[0]
    return {
        "running": svc.run_state["running"],
        "phase": svc.run_state["phase"],
        "run_id": svc.run_state["run_id"],
        "last_error": svc.run_state["last_error"],
        "last_run": svc.run_state["last_run"],
        "pending": pending,
        "approved": approved,
        "moved": moved,
        "breakdown": [dict(r) for r in counts],
        "move_enabled": segmentation_move_enabled(),
        "sweep_enabled": segmentation_sweep_enabled(),
    }


# ---------------------------------------------------------------------------
# Candidates
# ---------------------------------------------------------------------------

@router.get("/candidates")
def list_candidates(status: str = "proposed", tier: str | None = None,
                    limit: int = 500):
    """List candidates. ?status=proposed|approved|rejected|moved|all, ?tier=auto|review."""
    sql = [
        "SELECT c.id, c.track_id, c.source_path, c.dest_path, c.target_library, "
        "c.matched_field, c.matched_pattern, c.confidence_tier, c.confidence_reason, "
        "c.status, c.detected_at, t.artist, t.album, t.title "
        "FROM segmentation_candidates c LEFT JOIN tracks t ON t.id=c.track_id",
    ]
    params: list = []
    clauses = []
    if status != "all":
        clauses.append("c.status=?")
        params.append(status)
    if tier:
        clauses.append("c.confidence_tier=?")
        params.append(tier)
    if clauses:
        sql.append("WHERE " + " AND ".join(clauses))
    sql.append("ORDER BY c.id DESC LIMIT ?")
    params.append(limit)
    with get_db() as db:
        rows = db.execute("\n".join(sql), params).fetchall()
    return {"candidates": [dict(r) for r in rows], "count": len(rows)}


def _set_candidate_status(candidate_id: int, new_status: str) -> dict:
    with get_db() as db:
        row = db.execute(
            "SELECT status FROM segmentation_candidates WHERE id=?", (candidate_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="candidate not found")
        if row["status"] == "moved":
            raise HTTPException(status_code=400, detail="candidate already moved")
        db.execute(
            "UPDATE segmentation_candidates SET status=? WHERE id=?",
            (new_status, candidate_id),
        )
    return {"ok": True, "id": candidate_id, "status": new_status}


@router.post("/candidates/{candidate_id}/approve")
def approve_candidate(candidate_id: int):
    return _set_candidate_status(candidate_id, "approved")


@router.post("/candidates/{candidate_id}/reject")
def reject_candidate(candidate_id: int):
    return _set_candidate_status(candidate_id, "rejected")


# ---------------------------------------------------------------------------
# Apply approved
# ---------------------------------------------------------------------------

def _apply_worker(include_auto: bool):
    with svc.run_lock:
        svc.run_state.update({"running": True, "phase": "apply", "last_error": None})
        try:
            result = svc.apply_moves(include_auto=include_auto, include_approved=True)
            svc.run_state["run_id"] = result.get("run_id")
            svc.run_state["last_apply"] = result
        except Exception as e:
            svc.run_state["last_error"] = str(e)
            logger.error(f"segmentation apply failed: {e}")
        finally:
            svc.run_state.update({"running": False, "phase": "idle"})


@router.post("/apply-approved")
def apply_approved(include_auto: bool = False):
    """Move approved candidates (and, if include_auto, auto-tier proposed ones).

    Gated: refuses unless segmentation_move_enabled is on (defense in depth; the mover also
    checks). Runs in the background with the §10 batch guardrails.
    """
    if not segmentation_move_enabled():
        raise HTTPException(
            status_code=403,
            detail="segmentation_move_enabled is false — enable it in Settings to apply moves",
        )
    if svc.run_state["running"]:
        return {"ok": False, "error": "a segmentation run is already in progress"}
    t = threading.Thread(target=_apply_worker, args=(include_auto,), daemon=True,
                         name="segmentation-apply")
    t.start()
    return {"ok": True, "started": True}


# ---------------------------------------------------------------------------
# Undo
# ---------------------------------------------------------------------------

@router.post("/undo/{ledger_id}")
def undo_move(ledger_id: int):
    result = mover.reverse_move(ledger_id)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("reason", "reverse failed"))
    return result


@router.post("/undo-run/{run_id}")
def undo_run(run_id: str):
    return mover.reverse_run(run_id)


# ---------------------------------------------------------------------------
# Settings (kill switches)
# ---------------------------------------------------------------------------

class SegSettings(BaseModel):
    segmentation_move_enabled: bool | None = None
    segmentation_sweep_enabled: bool | None = None


@router.get("/settings")
def get_seg_settings():
    return {
        "segmentation_move_enabled": segmentation_move_enabled(),
        "segmentation_sweep_enabled": segmentation_sweep_enabled(),
    }


@router.put("/settings")
def update_seg_settings(data: SegSettings):
    with get_db() as db:
        for key in ("segmentation_move_enabled", "segmentation_sweep_enabled"):
            val = getattr(data, key)
            if val is not None:
                db.execute(
                    "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                    (key, "true" if val else "false"),
                )
    return get_seg_settings()
