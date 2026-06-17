"""
dedup_pass.py — U7 identity-gated dedup PASS (review-only by default).

`dedup.py` stays PURE detection (group / rank / classify). THIS module is the identity-aware
caller that the Phase-1 safety rails require:

  - find_dedup_candidates(): runs dedup.find_duplicates() over the active library, then JOINs
    track_identity to GATE which groups may act and classify each group auto_eligible vs review.
  - apply_dedup(): performs the reversible trash of inferior copies via file_txn.trash_file_txn
    (journaled / restorable) and records a dedup_actions row. NO os.rename / shutil.move here.

Default behavior is REVIEW-ONLY (CLAUDE.md hard rule: "No automated file actions — user wants
to review all duplicate resolutions manually"). apply_dedup() FAILS CLOSED: a non-dry-run trash
requires BOTH database.identity_act_enabled() AND database.dedup_act_enabled() to be true. The
file_txn chokepoint independently re-checks identity_act_enabled(), so the two gates are belt +
suspenders, exactly as the spec requires.
"""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
from typing import Optional

import database
import dedup
import file_txn
from scanner import quality_score

logger = logging.getLogger(__name__)

# Library root is also the file_txn trash parent (<root>/.m2-trash). Same NFS device → os.rename
# within the same filesystem (the cross-device guard in file_txn enforces this).
MUSIC_ROOT = Path(os.environ.get("MUSIC_PATH", "/music"))

LOSSLESS_FORMATS = {"flac", "alac", "wav"}
LOSSY_FORMATS = {"mp3", "aac", "m4a", "ogg", "wma", "opus"}


def _sha256(path: Path) -> Optional[str]:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _load_identity_map() -> dict:
    """track_id -> {state, mb_recording_id} for every resolved track."""
    out: dict[int, dict] = {}
    with database.get_db() as db:
        rows = db.execute(
            "SELECT track_id, state, mb_recording_id FROM track_identity"
        ).fetchall()
    for r in rows:
        out[r["track_id"]] = {"state": r["state"], "mb_recording_id": r["mb_recording_id"]}
    return out


def _track_summary(t: dict, identity_map: dict) -> dict:
    ident = identity_map.get(t["id"]) or {}
    return {
        "id": t["id"],
        "file_path": t.get("file_path"),
        "format": (t.get("format") or "").lower(),
        "quality_score": quality_score(t),
        "artist": t.get("artist"),
        "title": t.get("title"),
        "album": t.get("album"),
        "duration": t.get("duration"),
        "identity_state": ident.get("state"),
        "mb_recording_id": ident.get("mb_recording_id"),
    }


def _classify_group(members: list[dict], match_type: str, confidence: float,
                    identity_map: dict) -> dict:
    """
    Apply the identity gate to one detected duplicate group.

    Returns a candidate dict with:
      skipped / skip_reason — group is gated OUT (do not surface for action)
      keep / trash          — identity-aware keeper + inferior copies
      auto_eligible         — safest auto-apply bucket (fingerprint + lossless-keeps-lossy +
                              same resolved identity)
    """
    states = {m["id"]: (identity_map.get(m["id"]) or {}).get("state") for m in members}
    recs = {
        (identity_map.get(m["id"]) or {}).get("mb_recording_id")
        for m in members
    }
    distinct_recordings = {r for r in recs if r}

    skip_reason = None
    if any(states[m["id"]] == "conflict" for m in members):
        skip_reason = "conflict"
    elif len(distinct_recordings) > 1:
        # Members resolve to DIFFERENT recordings (live/alt/cover) — never cross-trash.
        skip_reason = "divergent_recordings"

    # Keep = highest quality_score among members; on a tie, prefer a 'confirmed' identity.
    keep = max(
        members,
        key=lambda m: (quality_score(m), 1 if states[m["id"]] == "confirmed" else 0),
    )
    trash = [m for m in members if m["id"] != keep["id"]]

    keep_fmt = (keep.get("format") or "").lower()
    # auto_eligible = the safest auto-apply bucket. A 'fingerprint' match (Chromaprint ≥0.75)
    # IS the positive same-recording evidence here — that is what "same resolved identity" means
    # for the canonical Oasis case, whose members are not necessarily resolved in track_identity.
    # Belt + suspenders: 'conflict' already skips the group; a member the resolver explicitly
    # flagged for human 'review' is excluded from the auto bucket (it stays a manual candidate).
    auto_eligible = (
        skip_reason is None
        and match_type == "fingerprint"
        and keep_fmt in LOSSLESS_FORMATS
        and len(trash) > 0
        and all((t.get("format") or "").lower() in LOSSY_FORMATS for t in trash)
        and not any(states[m["id"]] == "review" for m in members)
    )

    return {
        "match_type": match_type,
        "confidence": confidence,
        "skipped": skip_reason is not None,
        "skip_reason": skip_reason,
        "auto_eligible": auto_eligible,
        "keep_id": keep["id"],
        "keep": _track_summary(keep, identity_map),
        "trash_ids": [t["id"] for t in trash],
        "trash": [_track_summary(t, identity_map) for t in trash],
    }


def find_dedup_candidates(limit: Optional[int] = None,
                          include_skipped: bool = False) -> list[dict]:
    """
    Detect duplicate groups across the ACTIVE library and apply the identity gate.

    `limit` caps the number of active tracks loaded (for dry-run sampling); None = whole library.
    Default returns only ACTIONABLE (non-skipped) candidates. include_skipped=True also returns
    gated-out groups (flagged with skip_reason) for transparency in the review UI.
    """
    with database.get_db() as db:
        if limit:
            tracks = db.execute(
                "SELECT * FROM tracks WHERE status = 'active' LIMIT ?", (limit,)
            ).fetchall()
        else:
            tracks = db.execute(
                "SELECT * FROM tracks WHERE status = 'active'"
            ).fetchall()
    track_dicts = [dict(t) for t in tracks]

    identity_map = _load_identity_map()
    groups = dedup.find_duplicates(track_dicts)

    candidates = []
    skipped_count = 0
    for g in groups:
        cand = _classify_group(g["tracks"], g["match_type"], g["confidence"], identity_map)
        if cand["skipped"]:
            skipped_count += 1
            if not include_skipped:
                continue
        candidates.append(cand)

    logger.info(
        "find_dedup_candidates: %d groups detected, %d gated out (skipped), "
        "%d auto-eligible",
        len(groups), skipped_count,
        sum(1 for c in candidates if c.get("auto_eligible")),
    )
    return candidates


def _verify_request(keep_id: int, trash_ids: list[int]) -> tuple[str, float]:
    """
    Re-derive the duplicate group from the requested ids and confirm the request is legitimate
    BEFORE any trash. The route hands explicit ids; this is the server-side authority that the
    ids actually form one detected duplicate group, the gate passes, and `keep_id` is the
    COMPUTED keeper (so a client can never trash the lossless keeper or cross-trash a non-dupe).

    Returns (match_type, confidence) on success; raises ValueError otherwise.
    """
    ids = [keep_id] + list(trash_ids)
    placeholders = ",".join("?" * len(ids))
    with database.get_db() as db:
        rows = [
            dict(r)
            for r in db.execute(
                f"SELECT * FROM tracks WHERE id IN ({placeholders}) AND status = 'active'", ids
            ).fetchall()
        ]
    if len(rows) != len(set(ids)):
        raise ValueError("one or more tracks are missing or not active")

    identity_map = _load_identity_map()
    for g in dedup.find_duplicates(rows):
        if {t["id"] for t in g["tracks"]} != set(ids):
            continue
        cand = _classify_group(g["tracks"], g["match_type"], g["confidence"], identity_map)
        if cand["skipped"]:
            raise ValueError(f"group is gated out ({cand['skip_reason']}) — refusing")
        if cand["keep_id"] != keep_id:
            raise ValueError(
                f"keep_id {keep_id} is not the computed keeper ({cand['keep_id']}) — refusing"
            )
        if set(cand["trash_ids"]) != set(trash_ids):
            raise ValueError("trash_ids do not match the computed inferior set — refusing")
        return cand["match_type"], cand["confidence"]
    raise ValueError("requested ids do not form a single duplicate group — refusing")


def apply_dedup(group: dict, *, dry_run: bool = False) -> dict:
    """
    Trash the inferior copies of ONE reviewed group.

    `group` = {"keep_id": int, "trash_ids": [int, ...]}. Paths are loaded FRESH from the DB by id;
    the group is RE-VERIFIED server-side (_verify_request) — the caller's match_type/confidence are
    ignored in favor of the freshly computed values.

    FAIL CLOSED: a real (non-dry-run) trash requires BOTH database.identity_act_enabled() AND
    database.dedup_act_enabled(). Raises PermissionError otherwise (the route maps this to 403).
    Each trash goes through file_txn.trash_file_txn (journaled, restorable) and is logged to
    dedup_actions. Returns a per-track outcome report.
    """
    keep_id = int(group["keep_id"])
    trash_ids = [int(x) for x in (group.get("trash_ids") or [])]

    if not trash_ids:
        raise ValueError("trash_ids is empty — nothing to do")
    if keep_id in trash_ids:
        raise ValueError(f"keep_id {keep_id} is also in trash_ids — refusing")

    # Authoritative server-side re-check (overrides any caller-supplied match_type/confidence).
    match_type, confidence = _verify_request(keep_id, trash_ids)

    enabled = database.identity_act_enabled() and database.dedup_act_enabled()
    if not dry_run and not enabled:
        raise PermissionError(
            "dedup auto-apply blocked: requires identity_act_enabled AND dedup_act_enabled "
            "(both default false; this pass ships review-only)"
        )

    applied, skipped, errors = [], [], []

    for tid in trash_ids:
        with database.get_db() as db:
            row = db.execute(
                "SELECT id, file_path FROM tracks WHERE id = ? AND status = 'active'", (tid,)
            ).fetchone()
        if not row:
            skipped.append({"trashed_id": tid, "reason": "not_active_or_missing"})
            continue

        path = Path(row["file_path"])
        if not path.exists():
            skipped.append({"trashed_id": tid, "reason": "file_gone"})
            continue

        if dry_run:
            applied.append({"trashed_id": tid, "path": str(path), "dry_run": True})
            continue

        sha_before = _sha256(path)

        def _db_update(_tid=tid):
            with database.get_db() as db:
                db.execute("UPDATE tracks SET status = 'trashed' WHERE id = ?", (_tid,))

        try:
            result = file_txn.trash_file_txn(
                path,
                MUSIC_ROOT,
                # sha + exists revalidation: abort cleanly if the file changed since we hashed it.
                revalidate=lambda p=path, s=sha_before: p.exists() and _sha256(p) == s,
                db_update=_db_update,
                meta={
                    "keep_id": keep_id,
                    "trashed_id": tid,
                    "reason": "dedup",
                    "match_type": match_type,
                    "confidence": confidence,
                },
            )
        except file_txn.KillSwitchDisabled as exc:
            # identity_act_enabled flipped false between the gate check and the move.
            errors.append({"trashed_id": tid, "error": f"kill_switch: {exc}"})
            continue
        except Exception as exc:  # noqa: BLE001 — surface, never crash the batch
            logger.exception("apply_dedup trash failed for track %s", tid)
            errors.append({"trashed_id": tid, "error": str(exc)})
            continue

        if result.status != "finalized":
            errors.append({"trashed_id": tid, "error": f"op {result.status}"})
            continue

        with database.get_db() as db:
            db.execute(
                """INSERT INTO dedup_actions
                       (keep_id, trashed_id, match_type, confidence, sha_before)
                   VALUES (?, ?, ?, ?, ?)""",
                (keep_id, tid, match_type, confidence, sha_before),
            )

        applied.append({
            "trashed_id": tid,
            "path": str(path),
            "trash_path": str(result.trash_path) if result.trash_path else None,
            "op_id": result.op_id,
        })

    return {
        "dry_run": dry_run,
        "keep_id": keep_id,
        "applied": applied,
        "skipped": skipped,
        "errors": errors,
    }
