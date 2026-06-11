"""
identity_sweep.py — Library-wide identity-resolution sweep (U5).

Walk every active track, build evidence, call identity_resolver.resolve(),
and write ONLY track_identity rows.  No tag writes.  No file moves.

Status dict (sweep_status)
--------------------------
Running state lives here; routes read it via get_sweep_status().

AcoustID rate limiting
----------------------
Cloned from fingerprint_engine._rate_limited_acoustid — 3 req/sec sliding
window. Isolated to this module; no shared state with fingerprint_engine.

AudD escalation policy (two-phase per track)
---------------------------------------------
Phase 1: build evidence with audd=None, audd_attempted=False.
         call resolve().
Phase 2: if result.state != "confirmed" and check_budget() → call
         audd_client.identify_track(path) → rebuild evidence → call
         resolve() again with audd result and audd_attempted=True.
The second resolve() result is what gets written to track_identity.

Mirror hard-stop
----------------
mb_local.is_available() checked once at sweep start (hard-stop if False).
Also checked between every track: if it goes down mid-sweep, mark that
track deferred with mirror_available=False and stop (resumable).

Resumable sweep
---------------
Processes tracks that have no track_identity row at RESOLVER_VERSION, plus
tracks whose row is in a non-terminal mechanical state (deferred / error) —
those are re-resolved automatically per the resolver transition map.
Re-run after a stop continues from where it left off.

Lease / epoch fencing
---------------------
acquire_job("identity_sweep") at start; heartbeat every batch (default 50);
assert_lease before each batch's DB writes; release on normal exit.
LeaseLost → stop sweep with stopped_reason="lease_lost".
"""

from __future__ import annotations

import json
import logging
import os
import platform
import threading
import time
from datetime import datetime, timezone
from typing import Optional

import database
import job_locks
import mb_local
from audd_client import check_budget, identify_track
from audio_probe import decoded_duration_ms
from identity_resolver import RESOLVER_VERSION, resolve

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level sweep state (mirrors fingerprint_engine.fp_status pattern)
# ---------------------------------------------------------------------------

sweep_status: dict = {
    "running": False,
    "started_at": None,
    "stopped_at": None,
    "stopped_reason": None,       # "complete" | "stopped" | "mirror_unavailable" | "lease_lost" | "error"
    "total": 0,
    "processed": 0,
    "confirmed": 0,
    "review": 0,
    "unknown": 0,
    "conflict": 0,
    "deferred": 0,
    "error": 0,
    "audd_escalated": 0,
    "last_heartbeat": None,
    "current_file": None,
}

_sweep_lock = threading.Lock()       # guards sweep_status dict only
_sweep_run_lock = threading.Lock()   # guards single concurrent run (held for the whole sweep)
_sweep_stop = threading.Event()
_sweep_thread: Optional[threading.Thread] = None

# ---------------------------------------------------------------------------
# AcoustID rate limiter (3 req/sec, cloned from fingerprint_engine)
# ---------------------------------------------------------------------------

_acoustid_lock = threading.Lock()
_acoustid_timestamps: list[float] = []
ACOUSTID_RATE_LIMIT = 3  # requests/second


def _rate_limited_acoustid(fingerprint: str, duration: float) -> list[dict]:
    from tagger import lookup_acoustid
    with _acoustid_lock:
        now = time.time()
        while _acoustid_timestamps and _acoustid_timestamps[0] < now - 1.0:
            _acoustid_timestamps.pop(0)
        if len(_acoustid_timestamps) >= ACOUSTID_RATE_LIMIT:
            wait_time = 1.0 - (now - _acoustid_timestamps[0])
            if wait_time > 0:
                time.sleep(wait_time)
            _acoustid_timestamps.pop(0)
        _acoustid_timestamps.append(time.time())
    return lookup_acoustid(fingerprint, duration)


# ---------------------------------------------------------------------------
# Status helpers
# ---------------------------------------------------------------------------

def _sw_set(**kwargs) -> None:
    """Thread-safe status dict update."""
    with _sweep_lock:
        sweep_status.update(kwargs)


def _sw_inc(key: str, delta: int = 1) -> None:
    with _sweep_lock:
        sweep_status[key] = sweep_status.get(key, 0) + delta


def get_sweep_status() -> dict:
    with _sweep_lock:
        s = dict(sweep_status)
    # Liveness check — thread died but status stuck on running
    global _sweep_thread
    if s["running"] and _sweep_thread is not None and not _sweep_thread.is_alive():
        _sw_set(running=False, stopped_reason="error",
                stopped_at=_now_iso())
        s = dict(sweep_status)
    return s


def stop_sweep() -> None:
    """Signal the running sweep to stop gracefully."""
    _sweep_stop.set()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Fingerprint cache helper (mirrors Phase A of fingerprint_engine)
# ---------------------------------------------------------------------------

def _get_or_compute_fingerprint(track_id: int, file_path: str) -> tuple[Optional[str], Optional[float]]:
    """Return (fingerprint, duration_seconds) from DB cache or fpcalc."""
    with database.get_db() as db:
        row = db.execute(
            "SELECT fingerprint, duration FROM tracks WHERE id = ?",
            (track_id,),
        ).fetchone()

    if row and row["fingerprint"] and row["duration"]:
        return row["fingerprint"], float(row["duration"])

    # Not cached — run fpcalc
    try:
        from tagger import generate_fingerprint_with_duration
        fp, dur = generate_fingerprint_with_duration(file_path)
        if fp and dur:
            # Cache to DB (same as fingerprint_engine Phase A)
            with database.get_db() as db:
                db.execute(
                    "UPDATE tracks SET fingerprint = ?, duration = ? WHERE id = ?",
                    (fp, dur, track_id),
                )
            return fp, dur
    except Exception as e:
        logger.debug(f"fpcalc failed for {file_path}: {e}")

    return None, None


# ---------------------------------------------------------------------------
# Enrich top AcoustID candidates from MB mirror
# ---------------------------------------------------------------------------

def _enrich_candidates(candidates: list[dict]) -> list[dict]:
    """
    Add artist/title/length_ms/isrcs/release_id/album/date/track_no
    from MB mirror for up to the top-5 candidates.
    Candidates already have score/recording_id from tagger.lookup_acoustid;
    we fill in the metadata fields the resolver expects.
    """
    enriched = []
    for i, cand in enumerate(candidates):
        if i >= 5:
            # Pass through remaining candidates without enrichment
            enriched.append(cand)
            continue

        recording_id = cand.get("recording_id")
        if not recording_id:
            enriched.append(cand)
            continue

        try:
            meta = mb_local.get_recording_metadata(recording_id)
        except Exception:
            meta = None

        if meta:
            merged = dict(cand)
            merged["artist"] = meta.get("artist", "")
            merged["title"] = meta.get("title", "")
            merged["length_ms"] = meta.get("length_ms")
            merged["isrcs"] = meta.get("isrcs", [])
            merged["release_id"] = meta.get("release_id", "")
            merged["album"] = meta.get("album", "")
            merged["date"] = meta.get("date", "")
            merged["track_no"] = meta.get("track_number")
            enriched.append(merged)
        else:
            enriched.append(cand)

    return enriched


# ---------------------------------------------------------------------------
# Build evidence dict for a single track
# ---------------------------------------------------------------------------

def _build_evidence(
    track_id: int,
    file_path: str,
    existing_artist: str,
    existing_title: str,
    mirror_available: bool,
    audd_result: Optional[dict] = None,
    audd_attempted: bool = False,
    audd_budget_exhausted: bool = False,
) -> dict:
    """Gather all evidence signals for one track."""
    file_exists = os.path.isfile(file_path)

    # Duration from audio probe
    duration_ms: Optional[int] = None
    if file_exists:
        try:
            duration_ms = decoded_duration_ms(file_path)
        except Exception:
            pass

    # Fingerprint + AcoustID candidates
    acoustid_candidates: list[dict] = []
    fingerprint_ok = False
    if file_exists and mirror_available:
        fp, dur_sec = _get_or_compute_fingerprint(track_id, file_path)
        if fp and dur_sec:
            fingerprint_ok = True
            try:
                raw_candidates = _rate_limited_acoustid(fp, dur_sec)
                acoustid_candidates = _enrich_candidates(raw_candidates)
            except Exception as e:
                logger.debug(f"AcoustID lookup failed for {file_path}: {e}")

    # isrc_recording_map — only if audd result carries an ISRC
    isrc_recording_map: dict = {}
    if audd_result and audd_result.get("isrc") and mirror_available:
        isrc = audd_result["isrc"]
        try:
            recording_ids = mb_local.recordings_for_isrc(isrc)
            if recording_ids:
                isrc_recording_map = {isrc: recording_ids}
        except Exception:
            pass

    return {
        "file_exists": file_exists,
        "fingerprint_ok": fingerprint_ok,
        "duration_ms": duration_ms,
        "existing_artist": existing_artist or "",
        "existing_title": existing_title or "",
        "acoustid_candidates": acoustid_candidates,
        "audd": audd_result,
        "audd_attempted": audd_attempted,
        "audd_budget_exhausted": audd_budget_exhausted,
        "mirror_available": mirror_available,
        "isrc_recording_map": isrc_recording_map,
    }


# ---------------------------------------------------------------------------
# Write verdict to track_identity
# ---------------------------------------------------------------------------

def _write_identity(db, track_id: int, verdict: dict) -> None:
    db.execute(
        """
        INSERT OR REPLACE INTO track_identity (
            track_id, state, mb_recording_id, mb_release_id,
            isrc, artist, title, album, date, track_no,
            tier, evidence, divergent, decided_at, resolver_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            track_id,
            verdict["state"],
            verdict.get("mb_recording_id"),
            verdict.get("mb_release_id"),
            verdict.get("isrc"),
            verdict.get("artist"),
            verdict.get("title"),
            verdict.get("album"),
            verdict.get("date"),
            verdict.get("track_no"),
            verdict.get("tier"),
            json.dumps(verdict.get("evidence", {})),
            1 if verdict.get("divergent") else 0,
            _now_iso(),
            verdict.get("resolver_version", RESOLVER_VERSION),
        ),
    )


# ---------------------------------------------------------------------------
# Main sweep engine
# ---------------------------------------------------------------------------

BATCH_SIZE = 50
JOB_NAME = "identity_sweep"
LEASE_SECONDS = 300


def run_sweep(dry_run: bool = False) -> None:
    """
    Walk the library and populate track_identity.

    This function is designed to run in a background thread.
    Acquires _sweep_run_lock (non-blocking) to prevent concurrent sweeps —
    NOT _sweep_lock, which the status helpers need while the sweep runs.
    """
    if not _sweep_run_lock.acquire(blocking=False):
        logger.warning("identity_sweep: already running, skipping")
        return

    try:
        _run_sweep_inner(dry_run=dry_run)
    finally:
        _sweep_run_lock.release()


def _run_sweep_inner(dry_run: bool = False) -> None:
    _sweep_stop.clear()
    _sw_set(
        running=True,
        started_at=_now_iso(),
        stopped_at=None,
        stopped_reason=None,
        total=0,
        processed=0,
        confirmed=0,
        review=0,
        unknown=0,
        conflict=0,
        deferred=0,
        error=0,
        audd_escalated=0,
        last_heartbeat=_now_iso(),
        current_file=None,
    )

    # --- Mirror hard-stop ---
    try:
        if not mb_local.is_available():
            logger.error("identity_sweep: MB mirror unavailable — aborting")
            _sw_set(running=False, stopped_reason="mirror_unavailable",
                    stopped_at=_now_iso())
            return
    except Exception:
        logger.error("identity_sweep: mb_local.is_available() raised — aborting")
        _sw_set(running=False, stopped_reason="mirror_unavailable",
                stopped_at=_now_iso())
        return

    # --- Acquire job lock ---
    lease = job_locks.acquire_job(JOB_NAME, lease_seconds=LEASE_SECONDS)
    if lease is None:
        logger.warning("identity_sweep: could not acquire job lock — another process holds it")
        _sw_set(running=False, stopped_reason="lease_lost", stopped_at=_now_iso())
        return

    logger.info(f"identity_sweep: acquired lease epoch={lease.epoch} owner={lease.owner_id}")

    try:
        _run_sweep_with_lease(lease, dry_run=dry_run)
    except job_locks.LeaseLost as e:
        logger.error(f"identity_sweep: lease lost mid-sweep: {e}")
        _sw_set(running=False, stopped_reason="lease_lost", stopped_at=_now_iso())
    except Exception as e:
        logger.exception(f"identity_sweep: unexpected error: {e}")
        _sw_set(running=False, stopped_reason="error", stopped_at=_now_iso())
    finally:
        try:
            job_locks.release(lease)
        except Exception:
            pass


def _run_sweep_with_lease(lease: job_locks.Lease, dry_run: bool = False) -> None:
    # Count total tracks to process
    with database.get_db() as db:
        total = db.execute(
            """
            SELECT COUNT(*) FROM tracks t
            LEFT JOIN track_identity ti
                ON ti.track_id = t.id AND ti.resolver_version = ?
            WHERE t.status = 'active'
              AND (ti.track_id IS NULL OR ti.state IN ('deferred', 'error'))
            """,
            (RESOLVER_VERSION,),
        ).fetchone()[0]

    _sw_set(total=total)
    logger.info(f"identity_sweep: {total} tracks to process (RESOLVER_VERSION={RESOLVER_VERSION})")

    processed = 0
    batch_num = 0
    last_id = 0  # keyset cursor — guarantees forward progress even when
                 # nothing is written (dry_run) or a write doesn't stick

    while not _sweep_stop.is_set():
        # Fetch next batch of unresolved tracks
        with database.get_db() as db:
            rows = db.execute(
                """
                SELECT t.id, t.file_path, t.artist, t.title
                FROM tracks t
                LEFT JOIN track_identity ti
                    ON ti.track_id = t.id AND ti.resolver_version = ?
                WHERE t.status = 'active'
                  AND (ti.track_id IS NULL OR ti.state IN ('deferred', 'error'))
                  AND t.id > ?
                ORDER BY t.id
                LIMIT ?
                """,
                (RESOLVER_VERSION, last_id, BATCH_SIZE),
            ).fetchall()

        if not rows:
            break  # All done
        last_id = rows[-1]["id"]

        # Heartbeat every batch
        if not job_locks.heartbeat(lease):
            raise job_locks.LeaseLost(f"heartbeat returned False for job '{lease.job_name}'")
        _sw_set(last_heartbeat=_now_iso())

        batch_results: list[tuple[int, dict]] = []

        for row in rows:
            if _sweep_stop.is_set():
                break

            track_id = row["id"]
            file_path = row["file_path"]
            existing_artist = row["artist"] or ""
            existing_title = row["title"] or ""

            _sw_set(current_file=file_path)

            # Mid-sweep mirror check
            if not mb_local.is_available():
                logger.warning(f"identity_sweep: mirror went down at {file_path} — stopping")
                # Mark this track deferred (mirror_available=False) before stopping
                deferred_evidence = _build_evidence(
                    track_id, file_path, existing_artist, existing_title,
                    mirror_available=False,
                )
                deferred_verdict = resolve(deferred_evidence)
                batch_results.append((track_id, deferred_verdict))
                _sw_inc("deferred")

                if not dry_run:
                    # assert_lease then write
                    job_locks.assert_lease(lease)
                    with database.get_db() as db:
                        _write_identity(db, track_id, deferred_verdict)

                _sw_set(running=False, stopped_reason="mirror_unavailable",
                        stopped_at=_now_iso())
                return

            try:
                verdict = _resolve_track(
                    track_id, file_path,
                    existing_artist, existing_title,
                    check_budget=check_budget,
                    identify_track=identify_track,
                )
            except Exception as e:
                logger.warning(f"identity_sweep: error resolving {file_path}: {e}")
                error_evidence = {
                    "file_exists": os.path.isfile(file_path),
                    "fingerprint_ok": False,
                    "duration_ms": None,
                    "existing_artist": existing_artist,
                    "existing_title": existing_title,
                    "acoustid_candidates": [],
                    "audd": None,
                    "audd_attempted": False,
                    "audd_budget_exhausted": False,
                    "mirror_available": True,
                    "isrc_recording_map": {},
                    "error": str(e),
                }
                verdict = resolve({**error_evidence, "file_exists": False})
                verdict["state"] = "error"

            batch_results.append((track_id, verdict))
            state = verdict.get("state", "error")
            _sw_inc(state if state in ("confirmed", "review", "unknown",
                                        "conflict", "deferred", "error") else "error")
            processed += 1

        # --- assert_lease before writing the batch ---
        job_locks.assert_lease(lease)

        if not dry_run:
            with database.get_db() as db:
                for track_id, verdict in batch_results:
                    _write_identity(db, track_id, verdict)

        _sw_set(processed=processed)
        batch_num += 1

        if _sweep_stop.is_set():
            break

    stop_reason = "stopped" if _sweep_stop.is_set() else "complete"
    _sw_set(
        running=False,
        stopped_reason=stop_reason,
        stopped_at=_now_iso(),
        current_file=None,
    )
    logger.info(
        f"identity_sweep: {stop_reason} — "
        f"processed={processed} "
        f"confirmed={sweep_status['confirmed']} "
        f"review={sweep_status['review']} "
        f"unknown={sweep_status['unknown']} "
        f"conflict={sweep_status['conflict']} "
        f"deferred={sweep_status['deferred']} "
        f"error={sweep_status['error']}"
    )


def _resolve_track(
    track_id: int,
    file_path: str,
    existing_artist: str,
    existing_title: str,
    check_budget,
    identify_track,
) -> dict:
    """
    Two-phase AudD escalation:
    1. Resolve without AudD.
    2. If not confirmed and budget available → call AudD → resolve again.
    """
    # Phase 1 — no AudD
    evidence1 = _build_evidence(
        track_id, file_path, existing_artist, existing_title,
        mirror_available=True,
        audd_result=None,
        audd_attempted=False,
        audd_budget_exhausted=not check_budget(),
    )
    verdict1 = resolve(evidence1)

    if verdict1["state"] == "confirmed":
        return verdict1

    # Phase 2 — AudD escalation
    if not check_budget():
        # Budget exhausted; rebuild evidence with flag set
        evidence2 = _build_evidence(
            track_id, file_path, existing_artist, existing_title,
            mirror_available=True,
            audd_result=None,
            audd_attempted=False,
            audd_budget_exhausted=True,
        )
        return resolve(evidence2)

    audd_result = identify_track(file_path)
    _sw_inc("audd_escalated")

    evidence2 = _build_evidence(
        track_id, file_path, existing_artist, existing_title,
        mirror_available=True,
        audd_result=audd_result,
        audd_attempted=True,
        audd_budget_exhausted=False,
    )
    return resolve(evidence2)
