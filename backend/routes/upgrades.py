import asyncio
import logging
import os
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from database import get_db
from file_manager import compute_sha256, import_flac
from scanner import read_track_metadata
from ws_manager import manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/upgrades", tags=["upgrades"])


class ScanScope(BaseModel):
    format_filter: Literal["all_lossy", "mp3", "aac", "m4a", "ogg", "wma", "opus", "cd_flac"] = "all_lossy"
    unscanned_only: bool = True
    batch_size: int = Field(default=50, ge=0)
    artist_filter: str | None = None

LOSSY_FORMATS = {"mp3", "aac", "m4a", "ogg", "wma", "opus"}
_counter_lock = threading.Lock()

# Global upgrade status
upgrade_search_status = {
    "running": False,
    "phase": "idle",
    "searched": 0,
    "found": 0,
    "downloading": 0,
    "completed": 0,
    "failed": 0,
    # Per-item download detail (populated during download phase)
    "current_track": None,        # "Artist - Title"
    "current_artist": None,
    "current_title": None,
    "current_album": None,
    "current_step": None,         # "slskd" | "transferring" | "importing"
    "current_bytes": 0,           # bytes transferred from slskd
    "current_total_bytes": 0,     # total file size in bytes
    "download_index": 0,          # 1-based current item number
    "download_total": 0,          # total items in this download run
}

_search_lock = threading.Lock()
_download_lock = threading.Lock()
_event_loop: asyncio.AbstractEventLoop | None = None


def set_event_loop(loop: asyncio.AbstractEventLoop):
    global _event_loop
    _event_loop = loop


def _broadcast_sync(msg_type: str, data: dict):
    loop = _event_loop
    if loop is None or not loop.is_running():
        return
    try:
        asyncio.run_coroutine_threadsafe(manager.broadcast(msg_type, data), loop)
    except Exception as e:
        logger.debug(f"Broadcast error: {e}")


def _get_setting(key: str, default: str) -> str:
    try:
        with get_db() as db:
            row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default
    except Exception:
        return default


def _run_upgrade_search_worker(
    format_filter: str = "all_lossy",
    unscanned_only: bool = True,
    batch_size: int = 50,
    artist_filter: str | None = None,
):
    """
    Background thread: search slskd for FLAC upgrades of pending lossy and CD-quality FLAC tracks.
    Uses album-level parallel searching to reduce search time and API calls.
    """
    if not _search_lock.acquire(blocking=False):
        logger.warning("Upgrade search already running")
        return

    job_id = None
    try:
        with get_db() as db:
            cursor = db.execute(
                "INSERT INTO jobs (job_type, status) VALUES ('upgrade_search', 'running')"
            )
            job_id = cursor.lastrowid
    except Exception as e:
        logger.error(f"Failed to create upgrade_search job: {e}")

    timeout_s = int(_get_setting("slskd_search_timeout_s", "15"))
    concurrency = int(_get_setting("upgrade_concurrency", "8"))

    upgrade_search_status.update({
        "running": True,
        "phase": "searching",
        "searched": 0,
        "found": 0,
        "downloading": 0,
        "completed": 0,
        "failed": 0,
    })
    _broadcast_sync("job_update", dict(upgrade_search_status))

    try:
        # Build format filter
        if format_filter == "cd_flac":
            fmt_clause = "t.format = 'flac' AND (t.bit_depth IS NULL OR t.bit_depth <= 16)"
            params = []
        elif format_filter == "all_lossy":
            lossy_placeholders = ",".join("?" * len(LOSSY_FORMATS))
            fmt_clause = f"t.format IN ({lossy_placeholders})"
            params = list(LOSSY_FORMATS)
        else:
            # Specific lossy format (mp3, aac, etc.)
            fmt_clause = "t.format = ?"
            params = [format_filter]

        unscanned_clause = ""
        if unscanned_only:
            unscanned_clause = """
                AND t.id NOT IN (
                    SELECT track_id FROM upgrade_queue
                    WHERE status NOT IN ('failed', 'skipped')
                )
            """

        artist_clause = ""
        if artist_filter:
            artist_clause = "AND LOWER(t.artist) LIKE ?"
            params.append(f"%{artist_filter.lower()}%")

        query = f"""
            SELECT t.* FROM tracks t
            WHERE ({fmt_clause})
            AND t.status = 'active'
            {unscanned_clause}
            {artist_clause}
            ORDER BY t.artist, t.album, t.track_number
        """

        with get_db() as db:
            candidates = db.execute(query, params).fetchall()

        total = len(candidates)
        logger.info(
            f"Upgrade search: {total} tracks to search "
            f"(lossy + CD-quality FLAC), concurrency={concurrency}"
        )

        # Pre-create queue entries for all candidates
        track_to_queue: dict[int, int] = {}  # track_id → queue_id
        for track in candidates:
            track_dict = dict(track)
            track_id = track_dict["id"]
            artist = track_dict.get("artist") or ""
            album = track_dict.get("album") or ""
            title = track_dict.get("title") or ""
            with get_db() as db:
                existing = db.execute(
                    "SELECT id FROM upgrade_queue WHERE track_id = ?", (track_id,)
                ).fetchone()
                if existing:
                    queue_id = existing["id"]
                    db.execute(
                        "UPDATE upgrade_queue SET status = 'searching', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (queue_id,),
                    )
                else:
                    cursor = db.execute(
                        "INSERT INTO upgrade_queue (track_id, search_query, status) VALUES (?, ?, 'searching')",
                        (track_id, f"{artist} {album or title}"),
                    )
                    queue_id = cursor.lastrowid
                track_to_queue[track_id] = queue_id

        # Group candidates by (artist, album); tracks with no album use individual search
        grouped: dict[tuple, list[dict]] = defaultdict(list)
        individual: list[dict] = []
        for track in candidates:
            track_dict = dict(track)
            artist = (track_dict.get("artist") or "").strip()
            album = (track_dict.get("album") or "").strip()
            if album:
                grouped[(artist, album)].append(track_dict)
            else:
                individual.append(track_dict)

        logger.info(
            f"Upgrade search: {len(grouped)} album groups, "
            f"{len(individual)} individual (no-album) tracks"
        )

        # Apply batch_size limit
        if batch_size > 0:
            group_keys = list(grouped.keys())[:batch_size]
            grouped = {k: grouped[k] for k in group_keys}
            remaining = max(0, batch_size - len(grouped))
            individual = individual[:remaining]

        from upgrade_service import search_album, search_for_flac

        def _search_group(group_key: tuple, tracks_in_group: list[dict]) -> dict[int, dict | None]:
            """Worker: run one album-level search, returns track_id → result|None."""
            artist, album = group_key
            try:
                return asyncio.run(search_album(artist, album, tracks_in_group, timeout_s))
            except Exception as e:
                logger.error(f"Album search error for '{artist} - {album}': {e}")
                return {}

        def _search_individual(track_dict: dict) -> tuple[int, dict | None]:
            """Worker: run one individual track search, returns (track_id, result|None)."""
            track_id = track_dict["id"]
            artist = track_dict.get("artist") or ""
            album = track_dict.get("album") or ""
            title = track_dict.get("title") or ""
            is_flac = (track_dict.get("format") or "").lower() == "flac"
            try:
                result = asyncio.run(
                    search_for_flac(
                        artist=artist,
                        album=album,
                        title=title,
                        timeout_s=timeout_s,
                        hi_res_only=is_flac,
                    )
                )
                return track_id, result
            except Exception as e:
                logger.error(f"Individual search error for track {track_id}: {e}")
                return track_id, None

        def _upsert_result(track_id: int, result: dict | None):
            """Write search result to upgrade_queue and update status counters."""
            queue_id = track_to_queue.get(track_id)
            if queue_id is None:
                return
            if result:
                with _counter_lock:
                    upgrade_search_status["found"] += 1
                with get_db() as db:
                    db.execute(
                        """UPDATE upgrade_queue
                           SET status = 'found',
                               match_quality = ?,
                               slskd_search_id = ?,
                               slskd_username = ?,
                               slskd_filename = ?,
                               slskd_file_size = ?,
                               updated_at = CURRENT_TIMESTAMP
                           WHERE id = ?""",
                        (
                            result["match_quality"],
                            result["slskd_search_id"],
                            result["username"],
                            result["filename"],
                            result["file_size"],
                            queue_id,
                        ),
                    )
            else:
                with get_db() as db:
                    db.execute(
                        "UPDATE upgrade_queue SET status = 'skipped', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (queue_id,),
                    )

        # Submit album groups in parallel
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            # Album group futures
            album_futures = {
                pool.submit(_search_group, key, tracks): tracks
                for key, tracks in grouped.items()
            }
            # Individual track futures
            individual_futures = {
                pool.submit(_search_individual, track): track
                for track in individual
            }
            all_futures = {**album_futures, **individual_futures}

            for future in as_completed(all_futures):
                try:
                    if future in album_futures:
                        tracks_in_group = album_futures[future]
                        group_results = future.result()  # dict[track_id → result|None]
                        for track_dict in tracks_in_group:
                            tid = track_dict["id"]
                            _upsert_result(tid, group_results.get(tid))
                            with _counter_lock:
                                upgrade_search_status["searched"] += 1
                    else:
                        track_dict = individual_futures[future]
                        tid, result = future.result()
                        _upsert_result(tid, result)
                        with _counter_lock:
                            upgrade_search_status["searched"] += 1
                except Exception as e:
                    logger.error(f"Future result error: {e}")
                    with _counter_lock:
                        upgrade_search_status["failed"] += 1
                finally:
                    _broadcast_sync("job_update", dict(upgrade_search_status))

        if job_id:
            with get_db() as db:
                db.execute(
                    "UPDATE jobs SET status = 'completed', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (job_id,),
                )

        upgrade_search_status["phase"] = "complete"
        _broadcast_sync("job_update", dict(upgrade_search_status))

    except Exception as e:
        logger.error(f"Upgrade search worker failed: {e}", exc_info=True)
        upgrade_search_status["phase"] = "failed"
        if job_id:
            try:
                with get_db() as db:
                    db.execute(
                        "UPDATE jobs SET status = 'failed', error_msg = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (str(e), job_id),
                    )
            except Exception:
                pass
    finally:
        upgrade_search_status["running"] = False
        _search_lock.release()
        _broadcast_sync("job_update", dict(upgrade_search_status))


def _run_download_worker():
    """
    Background thread: download approved slskd items, verify, import to library.
    """
    if not _download_lock.acquire(blocking=False):
        logger.warning("Download worker already running")
        return

    job_id = None
    try:
        with get_db() as db:
            cursor = db.execute(
                "INSERT INTO jobs (job_type, status) VALUES ('upgrade_download', 'running')"
            )
            job_id = cursor.lastrowid
    except Exception as e:
        logger.error(f"Failed to create upgrade_download job: {e}")

    staging_root = Path(os.environ.get("STAGING_PATH", "/staging"))
    music_path = os.environ.get("MUSIC_PATH", "/music")
    trash_path = os.environ.get("TRASH_PATH", "/trash")

    upgrade_search_status.update({
        "running": True,
        "phase": "downloading",
        "downloading": 0,
        "completed": 0,
        "failed": 0,
        "current_track": None,
        "current_artist": None,
        "current_title": None,
        "current_album": None,
        "current_step": None,
        "current_bytes": 0,
        "current_total_bytes": 0,
        "download_index": 0,
        "download_total": 0,
    })
    _broadcast_sync("job_update", dict(upgrade_search_status))

    try:
        with get_db() as db:
            approved = db.execute(
                """SELECT uq.*, t.file_path, t.artist, t.album, t.title,
                          t.track_number, t.disc_number,
                          t.format AS original_format,
                          t.bit_depth AS original_bit_depth,
                          t.sample_rate AS original_sample_rate
                   FROM upgrade_queue uq
                   JOIN tracks t ON uq.track_id = t.id
                   WHERE uq.status = 'approved'
                     AND uq.slskd_username IS NOT NULL
                     AND uq.slskd_filename IS NOT NULL"""
            ).fetchall()

        total = len(approved)
        logger.info(f"Download worker: {total} approved items")
        upgrade_search_status["download_total"] = total
        _broadcast_sync("job_update", dict(upgrade_search_status))

        from upgrade_service import download_file, fetch_completed_file, get_download_status, cancel_download

        for idx, item in enumerate(approved, start=1):
            item_dict = dict(item)
            queue_id = item_dict["id"]
            username = item_dict["slskd_username"]
            filename = item_dict["slskd_filename"]
            file_size = item_dict.get("slskd_file_size")
            original_path = item_dict["file_path"]
            artist = item_dict.get("artist") or ""
            title = item_dict.get("title") or ""
            album = item_dict.get("album") or ""

            upgrade_search_status.update({
                "downloading": upgrade_search_status["downloading"] + 1,
                "download_index": idx,
                "current_track": f"{artist} - {title}" if artist or title else filename.split("\\")[-1],
                "current_artist": artist,
                "current_title": title,
                "current_album": album,
                "current_step": "slskd",
                "current_bytes": 0,
                "current_total_bytes": file_size or 0,
            })
            _broadcast_sync("job_update", dict(upgrade_search_status))

            with get_db() as db:
                db.execute(
                    "UPDATE upgrade_queue SET status = 'downloading', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (queue_id,),
                )

            try:
                # Initiate download via slskd
                started = asyncio.run(download_file(username, filename, file_size))
                if not started:
                    raise RuntimeError(f"slskd download initiation failed for {username}/{filename}")

                # Poll until complete or failed
                poll_timeout = 600  # 10 minutes
                poll_interval = 5
                elapsed = 0
                local_filename = None

                while elapsed < poll_timeout:
                    time.sleep(poll_interval)
                    elapsed += poll_interval

                    dl_status = asyncio.run(get_download_status(username, filename))
                    if dl_status is None:
                        continue

                    # Update live bytes progress
                    bytes_done = dl_status.get("bytes_transferred", 0)
                    total_bytes = dl_status.get("size", 0) or file_size or 0
                    upgrade_search_status["current_bytes"] = bytes_done
                    upgrade_search_status["current_total_bytes"] = total_bytes
                    _broadcast_sync("job_update", dict(upgrade_search_status))

                    state = dl_status.get("state", "")
                    if state.startswith("Completed"):
                        if "Succeeded" in state or state == "Completed":
                            local_filename = dl_status.get("local_filename")
                            break
                        else:
                            raise RuntimeError(f"slskd download {state}: {username}/{filename}")
                    elif "Failed" in state or "Cancelled" in state:
                        raise RuntimeError(f"slskd download {state}: {username}/{filename}")

                if not local_filename:
                    raise RuntimeError(f"Download timed out or no local path: {username}/{filename}")

                # SCP completed file from BuyVM to local /staging
                upgrade_search_status["current_step"] = "transferring"
                _broadcast_sync("job_update", dict(upgrade_search_status))
                staging_file = fetch_completed_file(local_filename, str(staging_root))
                staging_path = Path(staging_file)

                if not staging_path.exists():
                    raise FileNotFoundError(f"Staging file not found after SCP: {staging_path}")

                # Compute SHA-256 of the downloaded file
                sha256_new = compute_sha256(str(staging_path))

                # Import: verify + move to library
                upgrade_search_status["current_step"] = "importing"
                _broadcast_sync("job_update", dict(upgrade_search_status))
                new_library_path = import_flac(str(staging_path), original_path, music_path)

                # Read metadata from the newly placed FLAC
                new_meta = read_track_metadata(new_library_path)

                # Quality gate: if original was FLAC, verify the new file is actually better
                if item_dict.get("original_format", "").lower() == "flac":
                    orig_depth = item_dict.get("original_bit_depth") or 0
                    orig_rate = item_dict.get("original_sample_rate") or 0
                    new_depth = new_meta.get("bit_depth") or 0
                    new_rate = new_meta.get("sample_rate") or 0
                    if new_depth <= orig_depth and new_rate <= orig_rate:
                        Path(new_library_path).unlink(missing_ok=True)
                        raise ValueError(
                            f"Downloaded FLAC not higher resolution than original "
                            f"(orig {orig_depth}bit/{orig_rate}Hz, new {new_depth}bit/{new_rate}Hz)"
                        )

                # Database updates
                from file_manager import trash_file as do_trash_file

                # Trash the original lossy file (if still exists)
                if Path(original_path).exists():
                    trash_dest = do_trash_file(original_path, trash_path, music_path)
                    with get_db() as db:
                        db.execute(
                            """INSERT INTO file_transactions
                               (track_id, action, source_path, dest_path, state, sha256_after)
                               VALUES (?, 'upgrade', ?, ?, 'committed', ?)""",
                            (item_dict["track_id"], original_path, trash_dest, sha256_new),
                        )

                with get_db() as db:
                    # Mark original as upgraded
                    db.execute(
                        "UPDATE tracks SET status = 'upgraded' WHERE id = ?",
                        (item_dict["track_id"],),
                    )

                    # Insert new FLAC track
                    db.execute(
                        """INSERT INTO tracks
                           (file_path, file_size, format, bitrate, bit_depth, sample_rate,
                            duration, artist, album_artist, album, title, track_number,
                            disc_number, status, scanned_at)
                           VALUES (?, ?, 'flac', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', CURRENT_TIMESTAMP)""",
                        (
                            new_library_path,
                            new_meta.get("file_size"),
                            new_meta.get("bitrate"),
                            new_meta.get("bit_depth"),
                            new_meta.get("sample_rate"),
                            new_meta.get("duration"),
                            new_meta.get("artist") or item_dict.get("artist", ""),
                            new_meta.get("album_artist", ""),
                            new_meta.get("album") or item_dict.get("album", ""),
                            new_meta.get("title") or item_dict.get("title", ""),
                            new_meta.get("track_number") or item_dict.get("track_number"),
                            new_meta.get("disc_number") or item_dict.get("disc_number"),
                        ),
                    )

                    # Mark queue item complete
                    db.execute(
                        """UPDATE upgrade_queue
                           SET status = 'completed',
                               staging_path = ?,
                               sha256_new = ?,
                               updated_at = CURRENT_TIMESTAMP
                           WHERE id = ?""",
                        (new_library_path, sha256_new, queue_id),
                    )

                upgrade_search_status["completed"] += 1
                logger.info(
                    f"Upgraded: {item_dict.get('artist')} - {item_dict.get('title')} -> {new_library_path}"
                )

            except Exception as e:
                logger.error(
                    f"Download failed for queue item {queue_id} "
                    f"({item_dict.get('artist')} - {item_dict.get('title')}): {e}"
                )
                upgrade_search_status["failed"] += 1
                with get_db() as db:
                    db.execute(
                        """UPDATE upgrade_queue
                           SET status = 'failed',
                               error_msg = ?,
                               updated_at = CURRENT_TIMESTAMP
                           WHERE id = ?""",
                        (str(e), queue_id),
                    )

        if job_id:
            with get_db() as db:
                db.execute(
                    "UPDATE jobs SET status = 'completed', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (job_id,),
                )

        upgrade_search_status["phase"] = "complete"
        _broadcast_sync("job_update", dict(upgrade_search_status))
        _broadcast_sync("stats_update", {"event": "download_complete"})

    except Exception as e:
        logger.error(f"Download worker failed: {e}", exc_info=True)
        upgrade_search_status["phase"] = "failed"
        if job_id:
            try:
                with get_db() as db:
                    db.execute(
                        "UPDATE jobs SET status = 'failed', error_msg = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (str(e), job_id),
                    )
            except Exception:
                pass
    finally:
        upgrade_search_status["running"] = False
        _download_lock.release()
        _broadcast_sync("job_update", dict(upgrade_search_status))


@router.get("")
@router.get("/")
def list_upgrades(status: str | None = None):
    """Return upgrade queue items with track metadata. Optionally filter by status."""
    with get_db() as db:
        if status:
            rows = db.execute(
                """SELECT uq.id, uq.track_id, uq.status, uq.match_quality,
                          uq.staging_path, uq.created_at, uq.updated_at, uq.error_msg,
                          t.artist, t.album, t.title, t.format, t.bitrate
                   FROM upgrade_queue uq
                   JOIN tracks t ON uq.track_id = t.id
                   WHERE uq.status = ?
                   ORDER BY uq.created_at DESC""",
                (status,),
            ).fetchall()
        else:
            rows = db.execute(
                """SELECT uq.id, uq.track_id, uq.status, uq.match_quality,
                          uq.staging_path, uq.created_at, uq.updated_at, uq.error_msg,
                          t.artist, t.album, t.title, t.format, t.bitrate
                   FROM upgrade_queue uq
                   JOIN tracks t ON uq.track_id = t.id
                   ORDER BY uq.created_at DESC"""
            ).fetchall()
    return [dict(r) for r in rows]


@router.post("/search")
def start_upgrade_search(scope: ScanScope | None = None):
    """Start a scoped background slskd search for FLAC upgrades."""
    if upgrade_search_status["running"]:
        return {"ok": False, "error": "Upgrade search already in progress"}

    s = scope or ScanScope()
    t = threading.Thread(
        target=_run_upgrade_search_worker,
        kwargs={
            "format_filter": s.format_filter,
            "unscanned_only": s.unscanned_only,
            "batch_size": s.batch_size,
            "artist_filter": s.artist_filter,
        },
        daemon=True,
    )
    t.start()
    return {"ok": True}


@router.get("/status")
def get_upgrade_status():
    """Return aggregate upgrade status counts."""
    with get_db() as db:
        counts = db.execute(
            """SELECT
               COUNT(*) FILTER (WHERE status = 'searching') as searching,
               COUNT(*) FILTER (WHERE status = 'found') as found,
               COUNT(*) FILTER (WHERE status = 'downloading') as downloading,
               COUNT(*) FILTER (WHERE status = 'completed') as completed,
               COUNT(*) FILTER (WHERE status = 'failed') as failed
               FROM upgrade_queue"""
        ).fetchone()

    c = dict(counts) if counts else {}
    return {
        "running": upgrade_search_status["running"],
        "phase": upgrade_search_status["phase"],
        "searched": upgrade_search_status["searched"],
        "found": c.get("found", 0),
        "downloading": c.get("downloading", 0),
        "completed": c.get("completed", 0),
        "failed": c.get("failed", 0),
        # Per-item detail
        "current_track": upgrade_search_status["current_track"],
        "current_artist": upgrade_search_status["current_artist"],
        "current_title": upgrade_search_status["current_title"],
        "current_album": upgrade_search_status["current_album"],
        "current_step": upgrade_search_status["current_step"],
        "current_bytes": upgrade_search_status["current_bytes"],
        "current_total_bytes": upgrade_search_status["current_total_bytes"],
        "download_index": upgrade_search_status["download_index"],
        "download_total": upgrade_search_status["download_total"],
    }


@router.get("/coverage")
def get_coverage():
    """Return scan coverage counts across the library."""
    with get_db() as db:
        lossy_placeholders = ",".join("?" * len(LOSSY_FORMATS))
        total = db.execute(
            f"""SELECT COUNT(*) FROM tracks
                WHERE (format IN ({lossy_placeholders})
                       OR (format = 'flac' AND (bit_depth IS NULL OR bit_depth <= 16)))
                AND status = 'active'""",
            list(LOSSY_FORMATS),
        ).fetchone()[0]

        scanned = db.execute(
            """SELECT COUNT(DISTINCT track_id) FROM upgrade_queue
               WHERE status NOT IN ('failed', 'skipped')"""
        ).fetchone()[0]

        found = db.execute(
            "SELECT COUNT(*) FROM upgrade_queue WHERE status = 'found'"
        ).fetchone()[0]

        completed = db.execute(
            "SELECT COUNT(*) FROM upgrade_queue WHERE status = 'completed'"
        ).fetchone()[0]

    return {
        "total_candidates": total,
        "scanned": scanned,
        "unscanned": max(0, total - scanned),
        "found": found,
        "completed": completed,
    }


@router.get("/unscanned")
def list_unscanned(limit: int = 500):
    """Return active lossy/CD-FLAC tracks that have never been searched."""
    with get_db() as db:
        lossy_placeholders = ",".join("?" * len(LOSSY_FORMATS))
        rows = db.execute(
            f"""SELECT t.id AS track_id, t.artist, t.album, t.title,
                       t.format, t.bitrate, t.bit_depth, t.sample_rate
                FROM tracks t
                LEFT JOIN upgrade_queue uq ON uq.track_id = t.id
                    AND uq.status NOT IN ('failed', 'skipped')
                WHERE (
                    t.format IN ({lossy_placeholders})
                    OR (t.format = 'flac' AND (t.bit_depth IS NULL OR t.bit_depth <= 16))
                )
                AND t.status = 'active'
                AND uq.track_id IS NULL
                ORDER BY t.artist, t.album, t.track_number
                LIMIT ?""",
            list(LOSSY_FORMATS) + [limit],
        ).fetchall()
    return [dict(r) for r in rows]


@router.post("/approve-hi-res")
def approve_hi_res_upgrades():
    """Approve only hi-res quality found items (match_quality = 'hi_res')."""
    with get_db() as db:
        result = db.execute(
            """UPDATE upgrade_queue
               SET status = 'approved', updated_at = CURRENT_TIMESTAMP
               WHERE status = 'found'
                 AND match_quality = 'hi_res'
                 AND slskd_username IS NOT NULL
                 AND slskd_filename IS NOT NULL"""
        )
        count = result.rowcount
    return {"ok": True, "approved": count}


@router.post("/{item_id}/approve")
def approve_upgrade(item_id: int):
    """Mark an upgrade queue item as approved for download."""
    with get_db() as db:
        row = db.execute(
            "SELECT id, status, slskd_username, slskd_filename FROM upgrade_queue WHERE id = ?",
            (item_id,),
        ).fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Upgrade item not found")

    if not row["slskd_username"] or not row["slskd_filename"]:
        raise HTTPException(
            status_code=400,
            detail="Cannot approve: no slskd download info. Run search first.",
        )

    with get_db() as db:
        db.execute(
            "UPDATE upgrade_queue SET status = 'approved', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (item_id,),
        )

    return {"ok": True, "status": "approved"}


@router.post("/approve-all")
def approve_all_upgrades():
    """Approve all upgrade items with status='found'."""
    with get_db() as db:
        result = db.execute(
            """UPDATE upgrade_queue
               SET status = 'approved', updated_at = CURRENT_TIMESTAMP
               WHERE status = 'found'
                 AND slskd_username IS NOT NULL
                 AND slskd_filename IS NOT NULL"""
        )
    return {"ok": True, "approved": result.rowcount}


@router.post("/download")
def start_download():
    """Start downloading all approved upgrade items."""
    if not _download_lock.acquire(blocking=False):
        return {"ok": False, "error": "A download is already running"}
    _download_lock.release()

    with get_db() as db:
        count = db.execute(
            "SELECT COUNT(*) FROM upgrade_queue WHERE status = 'approved'"
        ).fetchone()[0]

    if count == 0:
        return {"ok": False, "error": "No approved items to download"}

    t = threading.Thread(target=_run_download_worker, daemon=True)
    t.start()
    return {"ok": True, "count": count}


@router.post("/{item_id}/skip")
def skip_upgrade(item_id: int):
    """Skip an upgrade queue item."""
    with get_db() as db:
        row = db.execute("SELECT id FROM upgrade_queue WHERE id = ?", (item_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Upgrade item not found")

    with get_db() as db:
        db.execute(
            "UPDATE upgrade_queue SET status = 'skipped', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (item_id,),
        )
    return {"ok": True, "status": "skipped"}
