"""
Live & Holiday segmentation mover (docs/live-holiday-split-spec.md §7, §8).

Physically relocates one track at a time into a Live/Holiday library root with a
copy → verify(sha256) → commit(single DB transaction) → unlink discipline, so the source
file survives until the DB path update commits (the concrete resolution of Issue 3 — a
DB-update failure rolls back the file relocation, unlike reorg_worker which only warns).
Includes a startup/sweep reconciler for pending ledger rows and a fully-gated
reverse_move() for single and whole-run undo (Issue 5).

FAIL-CLOSED by design: every move is guarded by a mount sentinel + st_dev check (§4), the
segmentation_move_enabled kill switch, and a never-overwrite check. In a dev environment
without the NAS binds these guards abort cleanly with zero files moved — that is correct
behaviour, not a bug.
"""

import logging
import os
import shutil
import threading
import uuid
from pathlib import Path
from typing import Optional

from database import get_db, segmentation_move_enabled
from file_manager import compute_sha256
from reorg_worker import sanitize

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Target roots — CONFIGURABLE. [VERIFY AT BUILD] spec §4.
#
# The two new folders MUST live OUTSIDE the exact on-disk root Plex section 5 indexes, or
# Plex keeps indexing them into the main library and moved tracks still surface in shuffle,
# defeating the feature. The exact section-5 root on Beast (/mnt/nas/music vs
# /mnt/nas/music/FLAC vs other) is [VERIFY AT BUILD] and CANNOT be confirmed from this repo —
# do NOT create the NAS folders or edit docker-compose here. These are the container-side
# bind-mount targets (suggested siblings of the section-5 root, e.g. Beast
# /mnt/nas/Live Performances → /live_performances). Override via env at deploy time once the
# real root is confirmed.
# ---------------------------------------------------------------------------
LIVE_ROOT = os.environ.get("SEG_LIVE_ROOT", "/live_performances")
HOLIDAY_ROOT = os.environ.get("SEG_HOLIDAY_ROOT", "/holiday")

# The main music share device, used for the st_dev guard (container sees NAS at /music).
MUSIC_MOUNT = os.environ.get("MUSIC_PATH", "/music")

# Sentinel file that must be present NAS-side in each target root before any move.
MOUNT_SENTINEL = ".mm_mount_ok"

# Process-wide mover lock so the sweep and the "apply approved" endpoint never race the same
# track (§10). Also serializes the reconciler.
mover_lock = threading.Lock()


def target_root(target_library: str) -> str:
    if target_library == "live":
        return LIVE_ROOT
    if target_library == "holiday":
        return HOLIDAY_ROOT
    raise ValueError(f"unknown target_library: {target_library!r}")


# ---------------------------------------------------------------------------
# Guards (§4, §7 step 1)
# ---------------------------------------------------------------------------

def check_mount_guard(target_library: str) -> tuple[bool, str]:
    """Mount sentinel + st_dev check for one target root. Returns (ok, reason).

    Fails closed: if the sentinel is missing or the dest root is not on the same device as
    the /music NAS share (i.e. the bind mount is absent and the path would resolve into the
    container's ephemeral overlay), abort — otherwise a move would write into overlay and the
    source would be lost. Any stat error is a failure.
    """
    root = target_root(target_library)
    try:
        sentinel = os.path.join(root, MOUNT_SENTINEL)
        if not os.path.exists(sentinel):
            return False, f"mount sentinel missing: {sentinel} (NAS bind not present?)"
        dest_dev = os.stat(root).st_dev
        music_dev = os.stat(MUSIC_MOUNT).st_dev
        if dest_dev != music_dev:
            return False, (
                f"st_dev mismatch: {root} (dev {dest_dev}) != {MUSIC_MOUNT} (dev {music_dev}) "
                "— target root not on the NAS device, refusing to move into overlay"
            )
        return True, "ok"
    except Exception as e:
        return False, f"mount guard stat failed for {root}: {e}"


# ---------------------------------------------------------------------------
# Dest path (§7 step 4) — Artist/Album/NN - Title.ext under the target root
# ---------------------------------------------------------------------------

def build_dest_path(track_row, target_library: str) -> Optional[str]:
    """Build the dest path under the target library root, preserving the reorg structure.

    Uses DB metadata (album_artist preferred, then artist), mirroring reorg_worker sanitize
    conventions. Returns None if artist or album is missing (cannot place safely).
    """
    root = target_root(target_library)
    artist = (track_row["album_artist"] or track_row["artist"] or "").strip()
    album = (track_row["album"] or "").strip()
    title = (track_row["title"] or "").strip()
    src = track_row["file_path"]
    ext = Path(src).suffix

    if not artist or not album:
        return None

    artist = sanitize(artist)
    album = sanitize(album)

    track_no = track_row["track_number"] if "track_number" in track_row.keys() else None
    if title:
        title = sanitize(title)
        if track_no:
            filename = f"{str(track_no).zfill(2)} - {title}{ext}"
        else:
            filename = f"{title}{ext}"
    else:
        filename = Path(src).name

    return os.path.join(root, artist, album, filename)


# ---------------------------------------------------------------------------
# Move one track (§7 ordered steps)
# ---------------------------------------------------------------------------

def move_track(
    track_id: int,
    target_library: str,
    matched_pattern: str,
    tier: str,
    run_id: str,
    candidate_id: Optional[int] = None,
    old_rating_key: Optional[str] = None,
) -> dict:
    """Execute one segmentation move. Returns a result dict with 'ok' and 'reason'.

    Ordered per §7: guards → claim → intent ledger → copy → verify(sha) → single committed
    DB transaction (delete stale dest row, update tracks.file_path, mark ledger done) →
    unlink source. The source is NEVER unlinked before the transaction commits.
    """
    with mover_lock:
        return _move_track_locked(
            track_id, target_library, matched_pattern, tier, run_id,
            candidate_id, old_rating_key,
        )


def _reopen_candidate(candidate_id: Optional[int], note: str) -> None:
    if candidate_id is None:
        return
    try:
        with get_db() as db:
            db.execute(
                "UPDATE segmentation_candidates SET status='proposed' WHERE id=? AND status!='moved'",
                (candidate_id,),
            )
    except Exception as e:
        logger.warning(f"failed to reopen candidate {candidate_id}: {e} ({note})")


def _clear_claim(track_id: int) -> None:
    try:
        with get_db() as db:
            db.execute("UPDATE tracks SET move_status=NULL WHERE id=?", (track_id,))
    except Exception as e:
        logger.warning(f"failed to clear move_status for track {track_id}: {e}")


def _move_track_locked(
    track_id, target_library, matched_pattern, tier, run_id, candidate_id, old_rating_key,
) -> dict:
    # --- Step 1: guards (abort — zero files moved) --------------------------
    if not segmentation_move_enabled():
        return {"ok": False, "reason": "segmentation_move_enabled is false (kill switch)"}

    ok, reason = check_mount_guard(target_library)
    if not ok:
        return {"ok": False, "reason": f"mount guard failed: {reason}"}

    with get_db() as db:
        track = db.execute(
            "SELECT id, file_path, artist, album_artist, album, title, track_number, "
            "sha256, status FROM tracks WHERE id=?",
            (track_id,),
        ).fetchone()
    if track is None:
        return {"ok": False, "reason": f"track {track_id} not found"}
    if track["status"] != "active":
        return {"ok": False, "reason": f"track {track_id} not active (status={track['status']})"}

    src = track["file_path"]
    if not os.path.exists(src):
        return {"ok": False, "reason": f"source file missing: {src}"}

    dest = build_dest_path(track, target_library)
    if dest is None:
        return {"ok": False, "reason": "cannot build dest path (missing artist/album)"}

    # never-overwrite: refuse if a physical file already occupies dest.
    if os.path.exists(dest):
        _reopen_candidate(candidate_id, "dest collision")
        return {"ok": False, "reason": f"dest already exists (never-overwrite): {dest}",
                "collision": True}

    # --- Step 2: claim ------------------------------------------------------
    with get_db() as db:
        claimed = db.execute(
            "UPDATE tracks SET move_status='claiming' WHERE id=? AND move_status IS NULL",
            (track_id,),
        )
        if claimed.rowcount == 0:
            return {"ok": False, "reason": "track claimed by another mover (move_status set)"}

    # sha_before: prefer stored sha256, else hash the live file.
    sha_before = track["sha256"] or compute_sha256(src)

    # --- Step 3: intent ledger row (state='pending') ------------------------
    with get_db() as db:
        cur = db.execute(
            """INSERT INTO segmentation_moves
                   (track_id, source_path, dest_path, target_library, matched_pattern,
                    confidence_tier, old_rating_key, sha_before, run_id, state)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')""",
            (track_id, src, dest, target_library, matched_pattern, tier,
             old_rating_key, sha_before, run_id),
        )
        ledger_id = cur.lastrowid
        db.execute("UPDATE tracks SET move_status='moving' WHERE id=?", (track_id,))

    # --- Step 4: copy -------------------------------------------------------
    try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy2(src, dest)
    except Exception as e:
        _cleanup_dest(dest)
        _clear_claim(track_id)
        _reopen_candidate(candidate_id, "copy failed")
        return {"ok": False, "reason": f"copy failed: {e}", "ledger_id": ledger_id}

    # --- Step 5: verify -----------------------------------------------------
    try:
        sha_after = compute_sha256(dest)
        same_size = os.path.getsize(dest) == os.path.getsize(src)
    except Exception as e:
        _cleanup_dest(dest)
        _clear_claim(track_id)
        return {"ok": False, "reason": f"verify stat failed: {e}", "ledger_id": ledger_id}

    if sha_after != sha_before or not same_size:
        _cleanup_dest(dest)
        _clear_claim(track_id)  # leave ledger 'pending' for the reconciler
        return {"ok": False,
                "reason": f"sha/size mismatch (before={sha_before[:12]} after={sha_after[:12]})",
                "ledger_id": ledger_id}

    # --- Step 6: single committed DB transaction ---------------------------
    try:
        with get_db() as db:
            # Clear a stale (non-active) row at dest to avoid UNIQUE(file_path) violation.
            db.execute(
                "DELETE FROM tracks WHERE file_path=? AND status != 'active'",
                (dest,),
            )
            db.execute(
                "UPDATE tracks SET file_path=?, scanned_at=CURRENT_TIMESTAMP, move_status=NULL "
                "WHERE file_path=? AND status='active'",
                (dest, src),
            )
            db.execute(
                "UPDATE segmentation_moves SET state='done', sha_after=? WHERE id=?",
                (sha_after, ledger_id),
            )
    except Exception as e:
        # Roll back the file move: delete dest copy, leave source + DB untouched, reopen.
        _cleanup_dest(dest)
        _clear_claim(track_id)
        _reopen_candidate(candidate_id, "db commit failed")
        logger.error(f"segmentation move DB commit failed ({src} -> {dest}): {e}")
        return {"ok": False, "reason": f"DB commit failed, move rolled back: {e}",
                "ledger_id": ledger_id}

    # --- Step 7: unlink source (only after commit) --------------------------
    try:
        os.unlink(src)
    except Exception as e:
        # Harmless duplicate; reconciler/DB already points at dest.
        logger.warning(f"source unlink failed after commit ({src}): {e} — harmless duplicate")

    # --- Step 8: mark candidate moved ---------------------------------------
    if candidate_id is not None:
        try:
            with get_db() as db:
                db.execute(
                    "UPDATE segmentation_candidates SET status='moved' WHERE id=?",
                    (candidate_id,),
                )
        except Exception as e:
            logger.warning(f"failed to mark candidate {candidate_id} moved: {e}")

    return {"ok": True, "reason": "moved", "ledger_id": ledger_id,
            "dest_path": dest, "sha_after": sha_after}


def _cleanup_dest(dest: str) -> None:
    try:
        if os.path.exists(dest):
            os.unlink(dest)
    except Exception as e:
        logger.warning(f"failed to clean up dest copy {dest}: {e}")


# ---------------------------------------------------------------------------
# Reconciler (§7) — run at container start and at each sweep entry
# ---------------------------------------------------------------------------

def reconcile_pending() -> dict:
    """Reconcile every ledger row still state='pending'.

    If tracks.file_path already equals dest → roll forward (mark done). Otherwise delete any
    dest orphan, clear the claim, reopen the candidate. No data loss either way.
    """
    stats = {"rolled_forward": 0, "reopened": 0}
    with mover_lock:
        with get_db() as db:
            rows = db.execute(
                "SELECT id, track_id, source_path, dest_path FROM segmentation_moves "
                "WHERE state='pending' AND rolled_back=0"
            ).fetchall()
        for row in rows:
            ledger_id = row["id"]
            track_id = row["track_id"]
            dest = row["dest_path"]
            with get_db() as db:
                track = db.execute(
                    "SELECT file_path FROM tracks WHERE id=?", (track_id,)
                ).fetchone()
            if track is not None and track["file_path"] == dest:
                # DB already points at dest → committed, just finalize the ledger.
                sha_after = None
                try:
                    if os.path.exists(dest):
                        sha_after = compute_sha256(dest)
                except Exception:
                    pass
                with get_db() as db:
                    db.execute(
                        "UPDATE segmentation_moves SET state='done', sha_after=COALESCE(?, sha_after) "
                        "WHERE id=?",
                        (sha_after, ledger_id),
                    )
                    db.execute("UPDATE tracks SET move_status=NULL WHERE id=?", (track_id,))
                stats["rolled_forward"] += 1
            else:
                # Never committed → delete dest orphan, clear claim, reopen candidate.
                _cleanup_dest(dest)
                _clear_claim(track_id)
                with get_db() as db:
                    db.execute(
                        "UPDATE segmentation_candidates SET status='proposed' "
                        "WHERE track_id=? AND status NOT IN ('moved','rejected')",
                        (track_id,),
                    )
                stats["reopened"] += 1
        logger.info(f"segmentation reconcile: {stats}")
    return stats


# ---------------------------------------------------------------------------
# Reverse move (§8) — gate on four invariants, then invert
# ---------------------------------------------------------------------------

def reverse_move(ledger_id: int) -> dict:
    """Undo a single move by ledger id. Gated on four invariants (§8); refuses with a clear
    reason if any fails (never collapses to null/false — repo error-evidence rule)."""
    with mover_lock:
        return _reverse_move_locked(ledger_id)


def _reverse_move_locked(ledger_id: int) -> dict:
    with get_db() as db:
        row = db.execute(
            "SELECT id, track_id, source_path, dest_path, sha_after, rolled_back "
            "FROM segmentation_moves WHERE id=?",
            (ledger_id,),
        ).fetchone()
    if row is None:
        return {"ok": False, "reason": f"ledger row {ledger_id} not found"}

    track_id = row["track_id"]
    source_path = row["source_path"]
    dest = row["dest_path"]

    # Invariant 1: not already undone.
    if row["rolled_back"]:
        return {"ok": False, "reason": "already rolled back"}

    # Invariant 2: dest exists AND sha matches sha_after (don't clobber a downstream rewrite).
    if not os.path.exists(dest):
        return {"ok": False, "reason": f"dest missing, cannot reverse: {dest}"}
    if row["sha_after"]:
        try:
            if compute_sha256(dest) != row["sha_after"]:
                return {"ok": False,
                        "reason": "dest sha != sha_after — file changed downstream, refusing"}
        except Exception as e:
            return {"ok": False, "reason": f"failed to hash dest for reverse: {e}"}

    # Invariant 3: source slot free on disk AND no tracks row occupies source_path.
    if os.path.exists(source_path):
        return {"ok": False, "reason": f"source slot occupied on disk: {source_path}"}
    with get_db() as db:
        occ = db.execute(
            "SELECT id FROM tracks WHERE file_path=?", (source_path,)
        ).fetchone()
    if occ is not None and occ["id"] != track_id:
        return {"ok": False, "reason": f"another tracks row occupies source_path: {source_path}"}

    # Invariant 4: the tracks row still holds file_path = dest.
    with get_db() as db:
        track = db.execute("SELECT file_path FROM tracks WHERE id=?", (track_id,)).fetchone()
    if track is None:
        return {"ok": False, "reason": f"track {track_id} gone"}
    if track["file_path"] != dest:
        return {"ok": False,
                "reason": f"track file_path ({track['file_path']}) != dest ({dest})"}

    # Invert: move dest -> source_path, update DB, mark rolled_back.
    try:
        os.makedirs(os.path.dirname(source_path), exist_ok=True)
        shutil.move(dest, source_path)
    except Exception as e:
        return {"ok": False, "reason": f"reverse move failed: {e}"}

    try:
        with get_db() as db:
            db.execute(
                "UPDATE tracks SET file_path=?, scanned_at=CURRENT_TIMESTAMP WHERE id=?",
                (source_path, track_id),
            )
            db.execute(
                "UPDATE segmentation_moves SET rolled_back=1, rolled_back_at=CURRENT_TIMESTAMP "
                "WHERE id=?",
                (ledger_id,),
            )
    except Exception as e:
        # File already moved back; DB update failed — surface loudly for manual handling.
        logger.error(f"reverse_move DB update failed for ledger {ledger_id}: {e}")
        return {"ok": False, "reason": f"file restored but DB update failed: {e}",
                "needs_manual": True}

    return {"ok": True, "reason": "reversed", "track_id": track_id,
            "restored_path": source_path}


def reverse_run(run_id: str) -> dict:
    """Undo every done, not-rolled-back move in a run (whole-run undo, §8)."""
    with get_db() as db:
        rows = db.execute(
            "SELECT id FROM segmentation_moves WHERE run_id=? AND state='done' AND rolled_back=0",
            (run_id,),
        ).fetchall()
    results = {"reversed": 0, "failed": 0, "details": []}
    for row in rows:
        res = reverse_move(row["id"])
        if res.get("ok"):
            results["reversed"] += 1
        else:
            results["failed"] += 1
        results["details"].append({"ledger_id": row["id"], **res})
    return results


def new_run_id() -> str:
    """Generate a run id grouping a sweep/bulk run."""
    return uuid.uuid4().hex[:16]
