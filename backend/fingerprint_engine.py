"""
Fingerprint Engine — Two-pass audio fingerprint verification with confidence
scoring, tag backup, and state machine.

Orchestrates: fpcalc → AcoustID → local MusicBrainz mirror → AudD fallback →
disambiguation → confidence scoring → tag write → Plex refresh.
"""

import json
import logging
import os
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from database import get_db
from tagger import generate_fingerprint_with_duration, lookup_acoustid, write_metadata
from tag_backup import snapshot_tags
from audd_client import identify_track as audd_identify, check_budget as audd_check_budget
from genre_normalizer import normalize_genre
from cover_art import fetch_cover_art
from disambiguator import select_best_release, build_dir_lock

logger = logging.getLogger(__name__)

CONCURRENCY = 12  # NFS-validated sweet spot from sonic analyzer work
AIR_CHECK_SKIP = "Unknown Artist/Unknown Album"

# Status tracking for the fingerprint engine
fp_status = {
    "running": False,
    "phase": "idle",       # idle, scanning, pass1, pass2, writing, refreshing, complete, failed
    "processed": 0,
    "total": 0,
    "matched": 0,
    "auto_approved": 0,
    "flagged": 0,
    "unmatched": 0,
    "failed": 0,
    "elapsed_s": 0,
    "started_at": None,
    "current_file": None,
    "dry_run": False,
}

_fp_lock = threading.Lock()
_fp_stop = threading.Event()


def get_status() -> dict:
    """Return current fingerprint engine status."""
    s = dict(fp_status)
    if s.get("started_at"):
        s["elapsed_s"] = int(time.time() - s["started_at"])
    return s


def stop():
    """Signal the engine to stop."""
    _fp_stop.set()


def run_full_audit(dry_run: bool = False):
    """Process all unprocessed tracks. Main entry point."""
    if not _fp_lock.acquire(blocking=False):
        logger.warning("Fingerprint engine already running")
        return

    try:
        _fp_stop.clear()
        _reset_status(dry_run)

        # Clean up stale fingerprinting rows (previous crash)
        _cleanup_stale_rows()

        # Get all tracks to process
        with get_db() as db:
            rows = db.execute("""
                SELECT t.id, t.file_path, t.artist, t.title, t.album,
                       t.album_artist, t.fingerprint
                FROM tracks t
                LEFT JOIN fingerprint_results fr ON t.id = fr.track_id
                WHERE fr.id IS NULL
                  AND t.status = 'active'
                  AND t.file_path NOT LIKE ?
                ORDER BY t.file_path
            """, (f"%/{AIR_CHECK_SKIP}/%",)).fetchall()

        tracks = [dict(r) for r in rows]
        fp_status["total"] = len(tracks)
        fp_status["phase"] = "pass1"
        logger.info(f"Fingerprint audit: {len(tracks)} tracks to process (dry_run={dry_run})")

        if not tracks:
            fp_status["phase"] = "complete"
            fp_status["running"] = False
            return

        # Group by directory for album-level locking
        dir_groups = _group_by_directory(tracks)

        # Process in parallel by directory
        for dir_path, dir_tracks in dir_groups.items():
            if _fp_stop.is_set():
                break
            _process_directory(dir_path, dir_tracks, dry_run)

        # Pass 2: AudD for unmatched tracks
        if not _fp_stop.is_set() and not dry_run:
            _run_pass2()

        # Plex refresh for modified albums
        if not _fp_stop.is_set() and not dry_run:
            _plex_refresh_modified()

        fp_status["phase"] = "stopped" if _fp_stop.is_set() else "complete"
        logger.info(
            f"Fingerprint audit complete: matched={fp_status['matched']}, "
            f"auto_approved={fp_status['auto_approved']}, "
            f"flagged={fp_status['flagged']}, "
            f"unmatched={fp_status['unmatched']}, "
            f"failed={fp_status['failed']}"
        )

    except Exception as e:
        logger.error(f"Fingerprint engine failed: {e}", exc_info=True)
        fp_status["phase"] = "failed"
    finally:
        fp_status["running"] = False
        _fp_lock.release()


def run_incremental():
    """Process only new tracks (not in fingerprint_results). For nightly batch."""
    run_full_audit(dry_run=False)


def _reset_status(dry_run: bool):
    fp_status.update({
        "running": True,
        "phase": "scanning",
        "processed": 0,
        "total": 0,
        "matched": 0,
        "auto_approved": 0,
        "flagged": 0,
        "unmatched": 0,
        "failed": 0,
        "elapsed_s": 0,
        "started_at": time.time(),
        "current_file": None,
        "dry_run": dry_run,
    })


def _cleanup_stale_rows():
    """Mark stale 'fingerprinting' rows as failed (previous crash)."""
    try:
        with get_db() as db:
            stale_threshold = time.time() - 3600  # 1 hour
            db.execute(
                "UPDATE fingerprint_results SET status='failed', "
                "error_message='Orphaned: engine restarted', "
                "updated_at=CURRENT_TIMESTAMP "
                "WHERE status='fingerprinting'"
            )
    except Exception as e:
        logger.warning(f"Stale cleanup failed: {e}")


def _group_by_directory(tracks: list[dict]) -> dict[str, list[dict]]:
    """Group tracks by parent directory."""
    groups: dict[str, list[dict]] = {}
    for t in tracks:
        d = str(Path(t["file_path"]).parent)
        groups.setdefault(d, []).append(t)
    return groups


def _process_directory(dir_path: str, tracks: list[dict], dry_run: bool):
    """Process all tracks in a single directory with album-level locking."""
    dir_results = []
    dir_lock = None

    for track in tracks:
        if _fp_stop.is_set():
            return

        fp_status["current_file"] = track["file_path"]
        result = _process_track(track, dry_run, dir_lock)
        fp_status["processed"] += 1
        dir_results.append(result)

        # Update dir lock after each result
        if not dir_lock and len(dir_results) >= 2:
            dir_lock = build_dir_lock(dir_results)


def _process_track(track: dict, dry_run: bool, dir_lock: dict | None) -> dict:
    """
    Process a single track through the fingerprint pipeline.

    State machine:
    1. fpcalc → chromaprint fingerprint
    2. AcoustID lookup → MB local mirror query
    3. Disambiguate multi-match results
    4. Compute composite confidence
    5. If ≥95%: snapshot + auto-write tags
    6. If 50-94%: flag for manual review
    7. If <50%: mark unmatched for Pass 2
    """
    track_id = track["id"]
    file_path = track["file_path"]

    # Create initial fingerprint_results row
    fp_result_id = _create_fp_result(track_id)
    if not fp_result_id:
        fp_status["failed"] += 1
        return {"track_id": track_id, "status": "failed"}

    try:
        # Update status
        _update_fp_status(fp_result_id, "fingerprinting")

        # Step 1: Generate fingerprint (reuse existing if available)
        fingerprint = track.get("fingerprint")
        duration = None

        if not fingerprint:
            fingerprint, duration = generate_fingerprint_with_duration(file_path)
            if not fingerprint:
                _update_fp_status(fp_result_id, "failed", error="Could not generate fingerprint")
                fp_status["failed"] += 1
                return {"track_id": track_id, "status": "failed"}

        # Store chromaprint
        with get_db() as db:
            db.execute(
                "UPDATE fingerprint_results SET chromaprint=? WHERE id=?",
                (fingerprint, fp_result_id),
            )

        # Step 2: AcoustID lookup
        if not duration:
            # Get duration from tracks table or re-derive
            with get_db() as db:
                dur_row = db.execute(
                    "SELECT duration FROM tracks WHERE id=?", (track_id,)
                ).fetchone()
                duration = dur_row["duration"] if dur_row else 0

        matches = lookup_acoustid(fingerprint, duration or 0)

        if not matches:
            _update_fp_status(fp_result_id, "unmatched", error="No AcoustID match")
            fp_status["unmatched"] += 1
            return {"track_id": track_id, "status": "unmatched"}

        best_score = matches[0]["score"]

        # Step 3: Get metadata from local MusicBrainz mirror (or public API)
        recording_id = matches[0]["recording_id"]
        metadata = _get_mb_metadata(recording_id)

        if not metadata:
            _update_fp_status(fp_result_id, "unmatched", error="MusicBrainz lookup failed")
            fp_status["unmatched"] += 1
            return {"track_id": track_id, "status": "unmatched"}

        # Step 4: Disambiguate if multiple candidates
        # For now, we use the top match; disambiguator refines release selection
        existing_tags = {
            "artist": track.get("artist"),
            "title": track.get("title"),
            "album": track.get("album"),
        }

        # Apply dir lock if available
        if dir_lock and metadata.get("release_id") != dir_lock.get("release_id"):
            metadata["album"] = dir_lock.get("album", metadata.get("album", ""))
            metadata["release_id"] = dir_lock.get("release_id", metadata.get("release_id"))
            metadata["release_group_id"] = dir_lock.get("release_group_id", metadata.get("release_group_id"))

        # Step 5: Genre normalization
        genre_tags = metadata.get("genre_tags", [])
        matched_genre = normalize_genre(genre_tags) if genre_tags else "Other"
        matched_genre_raw = json.dumps([t.get("tag", t) if isinstance(t, dict) else t for t in genre_tags[:5]])

        # Step 6: Compute confidence
        confidence = _compute_confidence(best_score, metadata)

        # Step 7: Determine action based on confidence
        # Get confidence threshold from settings
        auto_threshold = _get_setting("fp_auto_threshold", 0.95)
        review_threshold = _get_setting("fp_review_threshold", 0.50)

        # Store the match result
        with get_db() as db:
            db.execute("""
                UPDATE fingerprint_results SET
                    acoustid_score=?, acoustid_recording_id=?,
                    acoustid_release_id=?,
                    composite_confidence=?, match_source='acoustid',
                    matched_artist=?, matched_title=?, matched_album=?,
                    matched_album_artist=?, matched_year=?,
                    matched_track_number=?, matched_disc_number=?,
                    matched_genre=?, matched_genre_raw=?,
                    matched_isrc=?, matched_label=?, matched_composer=?,
                    matched_cover_art_url=?,
                    matched_dsp_ids=?,
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=?
            """, (
                best_score, recording_id,
                metadata.get("release_id"),
                confidence,
                metadata.get("artist"), metadata.get("title"), metadata.get("album"),
                metadata.get("album_artist"), metadata.get("date") or metadata.get("year"),
                metadata.get("track_number"), metadata.get("disc_number"),
                matched_genre, matched_genre_raw,
                metadata.get("isrc"), metadata.get("label"), metadata.get("composer"),
                metadata.get("cover_art_url"),
                json.dumps(metadata.get("dsp_ids")) if metadata.get("dsp_ids") else None,
                fp_result_id,
            ))

        fp_status["matched"] += 1

        if confidence >= auto_threshold:
            # Auto-approve: snapshot + write tags (unless dry run)
            if dry_run:
                _update_fp_status(fp_result_id, "auto_approved")
            else:
                _auto_fix_track(track_id, file_path, fp_result_id, metadata,
                                matched_genre, recording_id)
            fp_status["auto_approved"] += 1
            return {
                "track_id": track_id,
                "status": "auto_approved",
                "confidence": confidence,
                "release_id": metadata.get("release_id"),
                "album": metadata.get("album"),
                "release_group_id": metadata.get("release_group_id"),
            }

        elif confidence >= review_threshold:
            # Flag for manual review
            _update_fp_status(fp_result_id, "flagged")
            fp_status["flagged"] += 1
            return {
                "track_id": track_id,
                "status": "flagged",
                "confidence": confidence,
            }

        else:
            # Low confidence — mark for Pass 2
            _update_fp_status(fp_result_id, "pass2_pending")
            fp_status["unmatched"] += 1
            return {
                "track_id": track_id,
                "status": "pass2_pending",
                "confidence": confidence,
            }

    except Exception as e:
        logger.error(f"Track {track_id} processing failed: {e}", exc_info=True)
        _update_fp_status(fp_result_id, "failed", error=str(e))
        fp_status["failed"] += 1
        return {"track_id": track_id, "status": "failed"}

    finally:
        # Rate limit between tracks (courtesy delay)
        time.sleep(0.1)


def _get_mb_metadata(recording_id: str) -> dict | None:
    """Try local MB mirror first, fall back to public API."""
    try:
        import mb_local
        if mb_local.is_available():
            metadata = mb_local.get_recording_metadata(recording_id)
            if metadata:
                return metadata
    except ImportError:
        pass

    # Fall back to public MusicBrainz API
    from tagger import lookup_musicbrainz
    metadata = lookup_musicbrainz(recording_id)
    if metadata:
        # Add missing fields that the public API doesn't return
        metadata.setdefault("album_artist", metadata.get("artist"))
        metadata.setdefault("isrc", None)
        metadata.setdefault("label", None)
        metadata.setdefault("composer", None)
        metadata.setdefault("genre_tags", [])
        metadata.setdefault("disc_number", None)
    return metadata


def _compute_confidence(acoustid_score: float, metadata: dict) -> float:
    """
    Normalized 0-1 confidence score.

    Components:
    - fingerprint_score (0-1): AcoustID score
    - metadata_completeness (0-1): fraction of target fields present
    - disambiguation_clarity (0-1): 1.0 if rich metadata
    """
    completeness_fields = [
        "artist", "title", "album", "date", "track_number",
        "isrc", "label", "composer",
    ]
    present = sum(1 for f in completeness_fields if metadata.get(f))
    completeness = present / len(completeness_fields)

    # Clarity: penalize if critical fields are missing
    clarity = 1.0
    if not metadata.get("album"):
        clarity -= 0.3
    if not metadata.get("artist"):
        clarity -= 0.4

    confidence = (acoustid_score * 0.7) + (completeness * 0.2) + (clarity * 0.1)
    return round(min(1.0, max(0.0, confidence)), 4)


def _auto_fix_track(
    track_id: int,
    file_path: str,
    fp_result_id: int,
    metadata: dict,
    genre: str,
    recording_id: str,
):
    """Snapshot existing tags, write new tags, update status."""
    # Snapshot existing tags
    snap_id = snapshot_tags(track_id, file_path, fp_result_id)
    if snap_id is None:
        logger.warning(f"Snapshot failed for track {track_id}, skipping auto-fix")
        _update_fp_status(fp_result_id, "flagged")
        return

    # Fetch cover art
    art_bytes = None
    art_mime = "image/jpeg"
    rg_id = metadata.get("release_group_id")
    cover_url = metadata.get("cover_art_url")
    art_result = fetch_cover_art(rg_id, cover_url)
    if art_result:
        art_bytes, art_mime = art_result

    # Build tag dict
    tag_data = {
        "artist": metadata.get("artist"),
        "title": metadata.get("title"),
        "album": metadata.get("album"),
        "date": str(metadata.get("date") or metadata.get("year", ""))[:4] or None,
        "track_number": metadata.get("track_number"),
        "total_tracks": metadata.get("total_tracks"),
    }

    try:
        sha_before, sha_after = write_metadata(
            file_path, tag_data, art_bytes, recording_id, art_mime
        )
        _update_fp_status(fp_result_id, "tag_written")
        logger.debug(f"Auto-fixed: {file_path}")
    except Exception as e:
        logger.error(f"Tag write failed for {file_path}: {e}")
        # Rollback from snapshot
        from tag_backup import rollback_tags
        rollback_tags(snap_id)
        _update_fp_status(fp_result_id, "failed", error=f"Write failed: {e}")


def _run_pass2():
    """Run AudD fallback for unmatched and low-confidence tracks."""
    fp_status["phase"] = "pass2"

    if not audd_check_budget():
        logger.info("AudD budget exceeded, skipping Pass 2")
        return

    with get_db() as db:
        rows = db.execute("""
            SELECT fr.id, fr.track_id, t.file_path
            FROM fingerprint_results fr
            JOIN tracks t ON t.id = fr.track_id
            WHERE fr.status IN ('unmatched', 'pass2_pending')
            ORDER BY fr.created_at
        """).fetchall()

    if not rows:
        return

    logger.info(f"Pass 2 (AudD): {len(rows)} tracks")

    for row in rows:
        if _fp_stop.is_set():
            break
        if not audd_check_budget():
            logger.info("AudD budget hit during Pass 2, stopping")
            break

        fp_result_id = row["id"]
        track_id = row["track_id"]
        file_path = row["file_path"]

        fp_status["current_file"] = file_path
        _update_fp_status(fp_result_id, "pass2_processing")

        audd_result = audd_identify(file_path)
        if not audd_result:
            _update_fp_status(fp_result_id, "unmatched")
            continue

        # Store AudD result
        audd_score = audd_result.get("audd_score") or 0.8  # Default if not provided
        genre_tags = []  # AudD doesn't return MB-style genre tags
        matched_genre = "Other"

        with get_db() as db:
            db.execute("""
                UPDATE fingerprint_results SET
                    audd_score=?, audd_data=?, match_source='audd',
                    composite_confidence=?,
                    matched_artist=?, matched_title=?, matched_album=?,
                    matched_year=?, matched_isrc=?, matched_label=?,
                    matched_genre=?,
                    matched_cover_art_url=?, matched_spotify_id=?,
                    matched_dsp_ids=?,
                    status='flagged',
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=?
            """, (
                audd_score,
                json.dumps(audd_result),
                audd_score * 0.7 + 0.2,  # Adjusted confidence
                audd_result.get("artist"),
                audd_result.get("title"),
                audd_result.get("album"),
                audd_result.get("year"),
                audd_result.get("isrc"),
                audd_result.get("label"),
                matched_genre,
                audd_result.get("cover_art_url"),
                audd_result.get("spotify_id"),
                json.dumps(audd_result.get("dsp_ids")),
                fp_result_id,
            ))

        fp_status["matched"] += 1
        fp_status["flagged"] += 1
        fp_status["unmatched"] = max(0, fp_status["unmatched"] - 1)

        # Rate limit for AudD
        time.sleep(0.5)


def _plex_refresh_modified():
    """Batch Plex force-refresh for albums with written tags."""
    fp_status["phase"] = "refreshing"

    plex_url = os.environ.get("PLEX_URL", "http://10.0.0.13:32400")
    plex_token = os.environ.get("PLEX_TOKEN", "")
    if not plex_token:
        logger.warning("PLEX_TOKEN not set, skipping refresh")
        return

    # Get unique album directories that were modified
    with get_db() as db:
        rows = db.execute("""
            SELECT DISTINCT t.file_path
            FROM fingerprint_results fr
            JOIN tracks t ON t.id = fr.track_id
            WHERE fr.status = 'tag_written'
        """).fetchall()

    if not rows:
        return

    # Get unique album rating keys from Plex
    # For now, trigger a full music library scan
    try:
        import urllib.request
        url = f"{plex_url}/library/sections/5/refresh?X-Plex-Token={plex_token}"
        req = urllib.request.Request(url, method="GET")
        urllib.request.urlopen(req, timeout=10)
        logger.info("Triggered Plex music library scan")

        # Update status of written tracks
        with get_db() as db:
            db.execute(
                "UPDATE fingerprint_results SET status='complete', "
                "updated_at=CURRENT_TIMESTAMP "
                "WHERE status='tag_written'"
            )
    except Exception as e:
        logger.warning(f"Plex refresh failed: {e}")


# -----------------------------------------------------------------------
# Helper functions
# -----------------------------------------------------------------------

def _create_fp_result(track_id: int) -> int | None:
    """Create an initial fingerprint_results row."""
    try:
        with get_db() as db:
            cursor = db.execute(
                "INSERT INTO fingerprint_results (track_id, status) VALUES (?, 'pending')",
                (track_id,),
            )
            return cursor.lastrowid
    except Exception as e:
        logger.error(f"Failed to create fp_result for track {track_id}: {e}")
        return None


def _update_fp_status(fp_result_id: int, status: str, error: str | None = None):
    """Update the status of a fingerprint result."""
    try:
        with get_db() as db:
            if error:
                db.execute(
                    "UPDATE fingerprint_results SET status=?, error_message=?, "
                    "processed_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (status, error, fp_result_id),
                )
            else:
                db.execute(
                    "UPDATE fingerprint_results SET status=?, "
                    "processed_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (status, fp_result_id),
                )
    except Exception as e:
        logger.warning(f"Failed to update fp_result {fp_result_id}: {e}")


def _get_setting(key: str, default=None):
    """Get a setting value, with type conversion."""
    try:
        with get_db() as db:
            row = db.execute(
                "SELECT value FROM settings WHERE key=?", (key,)
            ).fetchone()
            if row and row["value"]:
                try:
                    return float(row["value"])
                except ValueError:
                    return row["value"]
    except Exception:
        pass
    return default
