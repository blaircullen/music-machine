"""
Review-queue endpoints (Phase 2): approve/reject a resolved identity and apply
approved corrections. Separate router so the large identity.py stays untouched.

Prefix: /api/identity
- POST /track/{id}/review   {decision: 'approve'|'reject'}  -> marks reviewed_*
- POST /apply-approved      {fields?, limit?}               -> gated batch apply
"""
import logging

from fastapi import APIRouter
from pydantic import BaseModel

from database import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/identity", tags=["identity-review"])

_ALLOWED_FIELDS = ("artist", "title", "album")


class ReviewDecision(BaseModel):
    decision: str  # 'approve' | 'reject'


class ApplyApprovedBody(BaseModel):
    fields: list[str] | None = None
    limit: int | None = None


@router.post("/track/{track_id}/review")
def review_track(track_id: int, body: ReviewDecision):
    """Mark a review row approved or rejected. Approved rows feed apply-approved;
    rejected rows are recorded (reviewed_by='rejected') and never auto-applied."""
    if body.decision not in ("approve", "reject"):
        return {"ok": False, "error": "decision must be 'approve' or 'reject'"}
    reviewed_by = "approved" if body.decision == "approve" else "rejected"
    with get_db() as db:
        cur = db.execute(
            "UPDATE track_identity SET reviewed_by=?, reviewed_at=datetime('now') "
            "WHERE track_id=?",
            (reviewed_by, track_id),
        )
    if cur.rowcount == 0:
        return {"ok": False, "error": "no identity row for that track"}
    return {"ok": True, "track_id": track_id, "decision": body.decision}


@router.post("/apply-approved")
def apply_approved(body: ApplyApprovedBody):
    """Apply corrections for all approved rows. Flips the kill switch on only for
    the duration (always re-locked), routes through the verified correction_pass."""
    import correction_pass as cp

    fields = tuple(body.fields) if body.fields else _ALLOWED_FIELDS
    bad = set(fields) - set(_ALLOWED_FIELDS)
    if bad:
        return {"ok": False, "error": f"invalid fields: {sorted(bad)}"}

    if body.limit is not None and body.limit <= 0:
        return {"ok": False, "error": "limit must be a positive integer"}

    # NOTE: single-reviewer tool — we select approved ids then apply them. A
    # concurrent reject between SELECT and apply (TOCTOU) is not guarded; not a
    # concern for one user, and every write is reversible regardless.
    sql = ("SELECT track_id FROM track_identity "
           "WHERE reviewed_by='approved' AND reviewed_at IS NOT NULL "
           "ORDER BY track_id")
    if body.limit is not None:
        sql += f" LIMIT {int(body.limit)}"
    with get_db() as db:
        ids = [r["track_id"] for r in db.execute(sql).fetchall()]
    if not ids:
        return {"ok": True, "summary": None, "note": "no approved rows pending"}

    def set_switch(value: str) -> None:
        with get_db() as db:
            cur = db.execute(
                "UPDATE settings SET value=? WHERE key='identity_act_enabled'",
                (value,),
            )
        if cur.rowcount == 0:
            raise RuntimeError("identity_act_enabled settings row missing")

    try:
        set_switch("true")
        res = cp.run(mode="review", dry_run=False, track_ids=ids, fields=fields)
    finally:
        try:
            set_switch("false")
        except Exception:
            logger.exception("FAILED to re-lock kill switch after apply-approved")
            raise

    return {"ok": True, "requested": len(ids), "summary": res.summary(),
            "errors": res.errors[:20]}
