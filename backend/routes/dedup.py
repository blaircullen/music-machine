"""
U7 identity-gated dedup — review-only API.

GET  /api/dedup/candidates — detected duplicate groups with the identity gate applied; each
                             group carries auto_eligible + per-track identity state for review.
POST /api/dedup/apply      — trash the inferior copies of ONE reviewed group, by explicit ids.
                             Fail-closed: a real trash needs identity_act_enabled AND
                             dedup_act_enabled (both default false). dry_run reports the plan.

This pass NEVER acts on its own — there is no auto-run loop. Trashing happens only when the
user POSTs explicit ids, and only when both gates are deliberately enabled.
"""

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import database
import dedup_pass

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/dedup", tags=["dedup"])


@router.get("/candidates")
def list_candidates(limit: int | None = None, auto_only: bool = False,
                    include_skipped: bool = False):
    """Return identity-gated dedup candidate groups. ?auto_only=true filters to the
    auto-eligible bucket; ?include_skipped=true surfaces gated-out groups (with skip_reason)."""
    candidates = dedup_pass.find_dedup_candidates(limit=limit, include_skipped=include_skipped)
    if auto_only:
        candidates = [c for c in candidates if c.get("auto_eligible")]
    return {
        "candidates": candidates,
        "count": len(candidates),
        "auto_eligible": sum(1 for c in candidates if c.get("auto_eligible")),
        "identity_act_enabled": database.identity_act_enabled(),
        "dedup_act_enabled": database.dedup_act_enabled(),
    }


class DedupApply(BaseModel):
    keep_id: int
    trash_ids: list[int]
    match_type: str | None = None
    confidence: float | None = None
    dry_run: bool = False


@router.post("/apply")
def apply(req: DedupApply):
    """Trash the inferior copies of one reviewed group by explicit ids."""
    try:
        return dedup_pass.apply_dedup(
            {
                "keep_id": req.keep_id,
                "trash_ids": req.trash_ids,
                "match_type": req.match_type,
                "confidence": req.confidence,
            },
            dry_run=req.dry_run,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
