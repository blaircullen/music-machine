"""
Live & Holiday segmentation — run orchestration (docs/live-holiday-split-spec.md §6, §10).

Shared by the routes (dry-run trigger, apply-approved) and the nightly sweep loop. Keeps the
policy in one place:
  - dry-run ALWAYS first: classify the scope, compute dest paths, record candidates, write the
    flat manifest — moves nothing (§6).
  - apply: physically move the auto-tier and/or explicitly-approved candidates with the §10
    batch guardrails (abort-on-anomaly), then trigger Plex scans + playlist repair.

A single in-process run lock prevents the sweep and the apply endpoint from racing.
"""

import json
import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from database import get_db
import segmentation_classifier as clf
import segmentation_mover as mover
import segmentation_playlist as seg_pl

logger = logging.getLogger(__name__)

LAST_RUN_FILE = Path(os.environ.get("SEG_LAST_RUN", "/data/segmentation_last_run.json"))

# Batch guardrails (§10) — a direct guard against the AudD bulk-retag incident.
BATCH_SIZE = 50
MAX_ANOMALY_RATE = 0.02          # abort the whole pass if collisions/mismatches exceed 2%
MAX_CONSECUTIVE_FAILURES = 5

# Live/Holiday on-disk roots — moved tracks live here, out of the section-5 sweep scope.
_SEG_ROOTS = (mover.LIVE_ROOT, mover.HOLIDAY_ROOT)

# In-process state for the UI heartbeat.
run_state: dict = {
    "running": False,
    "phase": "idle",
    "run_id": None,
    "last_run": None,
    "last_error": None,
    "last_heartbeat": None,
}

run_lock = threading.Lock()


def _load_last_run():
    try:
        if LAST_RUN_FILE.exists():
            run_state["last_run"] = json.loads(LAST_RUN_FILE.read_text())
    except Exception:
        pass


_load_last_run()


# ---------------------------------------------------------------------------
# Scope selection (§10 incremental)
# ---------------------------------------------------------------------------

def _select_scope(db, watermark: Optional[str], limit: Optional[int]):
    """Active tracks to (re)examine: exclude already-resolved and tracks already under a
    Live/Holiday root; honor the scanned_at > watermark incremental filter."""
    sql = [
        "SELECT t.id, t.file_path, t.artist, t.album_artist, t.album, t.title, t.track_number",
        "FROM tracks t",
        "WHERE t.status='active'",
        # Not already moved (done, not rolled back).
        "AND t.id NOT IN (SELECT track_id FROM segmentation_moves WHERE state='done' AND rolled_back=0)",
        # Not already decided (rejected or moved candidate).
        "AND t.id NOT IN (SELECT track_id FROM segmentation_candidates WHERE status IN ('rejected','moved'))",
    ]
    params: list = []
    # Exclude tracks physically living under the Live/Holiday roots.
    for root in _SEG_ROOTS:
        sql.append("AND t.file_path NOT LIKE ?")
        params.append(root.rstrip("/") + "/%")
    if watermark:
        sql.append("AND t.scanned_at > ?")
        params.append(watermark)
    sql.append("ORDER BY t.id")
    if limit:
        sql.append("LIMIT ?")
        params.append(limit)
    return db.execute("\n".join(sql), params).fetchall()


# ---------------------------------------------------------------------------
# Dry-run (§6)
# ---------------------------------------------------------------------------

def run_dry_run(watermark: Optional[str] = None, limit: Optional[int] = None,
                run_id: Optional[str] = None) -> dict:
    """Classify the scope and record candidates without moving anything (§6).

    Returns the manifest dict (also written to LAST_RUN_FILE). Re-proposes only fresh
    candidates: existing proposed rows for a track are refreshed; approved/rejected/moved
    rows are left untouched (a rejected track is not re-proposed unless its metadata changed).
    """
    run_id = run_id or mover.new_run_id()
    counts = {"live_auto": 0, "live_review": 0, "holiday_auto": 0, "holiday_review": 0}
    samples: list[dict] = []
    dest_seen: dict[str, int] = {}
    collisions: list[dict] = []
    recorded = 0

    with get_db() as db:
        rows = _select_scope(db, watermark, limit)

    for row in rows:
        with get_db() as db:
            matched, target, field, pattern, tier, reason = clf.classify_track(db, row)
        if not matched:
            continue

        dest = mover.build_dest_path(row, target) if target else None
        if dest is None:
            tier = "review"
            reason = (reason or "") + "; cannot compute dest path (missing artist/album)"

        # Within-run dest collision → force review (§5C: any within-batch collision → review).
        if dest is not None:
            if dest in dest_seen:
                tier = "review"
                reason = (reason or "") + "; within-run dest collision"
                collisions.append({"dest_path": dest, "track_id": row["id"]})
            dest_seen[dest] = row["id"]

        counts[f"{target}_{tier}"] = counts.get(f"{target}_{tier}", 0) + 1

        # Upsert: clear any stale 'proposed' row for this track, insert fresh. Leave
        # approved/rejected/moved rows alone.
        with get_db() as db:
            db.execute(
                "DELETE FROM segmentation_candidates WHERE track_id=? AND status='proposed'",
                (row["id"],),
            )
            # Skip re-proposing a track that was already rejected/approved/moved.
            existing = db.execute(
                "SELECT 1 FROM segmentation_candidates WHERE track_id=? "
                "AND status IN ('approved','rejected','moved') LIMIT 1",
                (row["id"],),
            ).fetchone()
            if existing:
                continue
            db.execute(
                """INSERT INTO segmentation_candidates
                       (track_id, source_path, dest_path, target_library, matched_field,
                        matched_pattern, confidence_tier, confidence_reason, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'proposed')""",
                (row["id"], row["file_path"], dest, target, field, pattern, tier, reason),
            )
        recorded += 1
        if len(samples) < 50:
            samples.append({
                "track_id": row["id"],
                "src": row["file_path"],
                "dest": dest,
                "target_library": target,
                "tier": tier,
                "matched_field": field,
                "matched_pattern": pattern,
            })

    manifest = {
        "run_id": run_id,
        "timestamp": datetime.now().isoformat(),
        "mode": "dry_run",
        "scope_tracks": len(rows),
        "recorded": recorded,
        "counts": counts,
        "collisions": collisions,
        "sample_moves": samples,
    }
    try:
        LAST_RUN_FILE.parent.mkdir(parents=True, exist_ok=True)
        LAST_RUN_FILE.write_text(json.dumps(manifest, indent=2))
    except Exception as e:
        logger.warning(f"failed to write segmentation manifest: {e}")
    run_state["last_run"] = manifest
    return manifest


# ---------------------------------------------------------------------------
# Apply (§7 + §10 guardrails)
# ---------------------------------------------------------------------------

def apply_moves(include_auto: bool, include_approved: bool, run_id: Optional[str] = None) -> dict:
    """Physically move eligible candidates with §10 batch guardrails, then Plex scan + repair.

    include_auto: move confidence_tier='auto' proposed candidates (nightly sweep path).
    include_approved: move status='approved' candidates (apply-approved endpoint path).
    """
    run_id = run_id or mover.new_run_id()

    # Reconcile any interrupted prior move first (§7).
    mover.reconcile_pending()

    # Select the candidates to move.
    where = []
    if include_auto:
        where.append("(status='proposed' AND confidence_tier='auto')")
    if include_approved:
        where.append("status='approved'")
    if not where:
        return {"ok": False, "reason": "no selection", "run_id": run_id}

    with get_db() as db:
        candidates = db.execute(
            "SELECT id, track_id, source_path, dest_path, target_library, matched_pattern, "
            "confidence_tier FROM segmentation_candidates "
            f"WHERE ({' OR '.join(where)}) ORDER BY id"
        ).fetchall()

    total = len(candidates)
    if total == 0:
        return {"ok": True, "moved": 0, "reason": "nothing to move", "run_id": run_id,
                "total": 0}

    # Snapshot affected playlists BEFORE the first move (§9).
    move_track_set = [{"track_id": c["track_id"], "file_path": c["source_path"]}
                      for c in candidates]
    try:
        seg_pl.snapshot_playlists(run_id, move_track_set)
    except Exception as e:
        logger.warning(f"playlist snapshot failed (continuing): {e}")

    moved = 0
    anomalies = 0
    consecutive_failures = 0
    aborted = False
    abort_reason = None
    targets_touched = set()
    details = []

    for idx, c in enumerate(candidates):
        tier = "reviewed" if c["confidence_tier"] != "auto" else "auto"
        res = mover.move_track(
            track_id=c["track_id"],
            target_library=c["target_library"],
            matched_pattern=c["matched_pattern"] or "",
            tier=tier,
            run_id=run_id,
            candidate_id=c["id"],
        )
        details.append({"candidate_id": c["id"], **res})
        if res.get("ok"):
            moved += 1
            consecutive_failures = 0
            targets_touched.add(c["target_library"])
        else:
            # A kill-switch/guard failure is a hard stop for the whole pass, not an anomaly tally.
            if "kill switch" in res.get("reason", "") or "mount guard" in res.get("reason", ""):
                aborted = True
                abort_reason = res.get("reason")
                break
            anomalies += 1
            consecutive_failures += 1

        # §10 guardrails, evaluated each batch boundary and on consecutive failures.
        if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            aborted = True
            abort_reason = f"{consecutive_failures} consecutive failures"
            break
        processed = idx + 1
        if processed % BATCH_SIZE == 0:
            rate = anomalies / processed
            if rate > MAX_ANOMALY_RATE:
                aborted = True
                abort_reason = f"anomaly rate {rate:.3f} > {MAX_ANOMALY_RATE}"
                break

    # Post-move: Plex scans for touched sections + section-5 refresh/emptyTrash, then repair.
    plex_result = _post_move_plex(run_id, targets_touched)

    result = {
        "ok": not aborted,
        "run_id": run_id,
        "total": total,
        "moved": moved,
        "anomalies": anomalies,
        "aborted": aborted,
        "abort_reason": abort_reason,
        "plex": plex_result,
        "details": details,
    }
    logger.info(f"segmentation apply run={run_id}: moved={moved}/{total} "
                f"anomalies={anomalies} aborted={aborted} ({abort_reason})")
    return result


def _post_move_plex(run_id: str, targets_touched: set) -> dict:
    """Trigger target-section scans + section-5 refresh/emptyTrash, then playlist repair (§9,§10).

    Wrapped end-to-end: in a dev env without Plex this records the failure and returns rather
    than raising — moves are already committed.
    """
    out = {"section5_refresh": False, "empty_trash": False, "target_scans": [],
           "repair": None, "error": None}
    if not targets_touched:
        return out
    try:
        import plex_playlist_sync as pps
        section5 = pps.MUSIC_SECTION_ID
        # Trigger target-section scans (best-effort — section ids are [VERIFY AT BUILD]).
        for target in targets_touched:
            section = seg_pl._section_for(target)
            if section:
                try:
                    pps._plex_get(f"/library/sections/{section}/refresh")
                    out["target_scans"].append(section)
                except Exception as e:
                    logger.warning(f"target section {section} scan failed: {e}")
        # Section 5: refresh so the vacated track leaves the index, then emptyTrash.
        try:
            pps._plex_get(f"/library/sections/{section5}/refresh")
            out["section5_refresh"] = True
            pps.wait_for_plex_scan(timeout=120)
        except Exception as e:
            logger.warning(f"section-5 refresh failed: {e}")
        try:
            pps._plex_put(f"/library/sections/{section5}/emptyTrash")
            out["empty_trash"] = True
        except Exception as e:
            logger.warning(f"section-5 emptyTrash failed: {e}")
        # Wait for target-section scans then repair playlists.
        try:
            pps.wait_for_plex_scan(timeout=120)
        except Exception:
            pass
        out["repair"] = seg_pl.repair_playlists(run_id)
    except Exception as e:
        out["error"] = str(e)
        logger.warning(f"post-move Plex/repair failed (moves already committed): {e}")
    return out
