#!/usr/bin/env python3
"""Lossless authenticity worker for queued FLAC files."""

from __future__ import annotations

import logging
import os
import sqlite3
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

import lossless_detect
import lidarr_client
import lidarr_recue
import recue_fakes
from database import log_recue


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s lossless-worker: %(message)s",
)
logger = logging.getLogger(__name__)

DB_PATH = os.environ.get("DB_PATH", "/data/music-machine.db")
MUSIC_PATH = Path(os.environ.get("MUSIC_PATH", "/music"))
MUSICGRABBER_URL = os.environ.get("MUSICGRABBER_URL", "http://10.0.0.13:38274")
DATA_PATH = Path(os.environ.get("DATA_PATH", "/data"))
SPECTROGRAM_DIR = DATA_PATH / "spectrograms"
STATE_FILE = DATA_PATH / "recue_state.jsonl"

POLL_INTERVAL = 15
INTER_TRACK_SLEEP = 0.1
TERMINAL_VERDICTS = {"lossless", "suspect", "low_rate", "insufficient_audio"}
RETRY_RECUE_STATUSES = {
    "no_match",
    "verify_failed_all",
    "dl_failed_all",
    "landed_not_found_all",
    "no_usable_candidate",
    "no_metadata",
}
SUCCESS_RECUE_STATUSES = {"placed", "staged", "already_present"}
HOST_MUSIC_PREFIXES = ("/mnt/nas/music", "/mnt/music")

_in_progress: set[int] = set()
_in_progress_lock = threading.Lock()


def configure_recue() -> None:
    recue_fakes.BASE = MUSICGRABBER_URL
    recue_fakes.MUSIC_ROOT = MUSIC_PATH
    recue_fakes.SINGLES_DIR = MUSIC_PATH / "Singles"
    recue_fakes.REJECT_DIR = MUSIC_PATH / ".recue-rejected"
    recue_fakes.STAGING_DIR = MUSIC_PATH / ".recue-staging"
    recue_fakes.STATE_FILE = STATE_FILE


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}


def get_setting(key: str, default: str) -> str:
    try:
        conn = get_db()
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        conn.close()
        return str(row["value"]) if row else default
    except Exception as exc:
        logger.warning("Failed to read setting %s, using default %r: %s", key, default, exc)
        return default


def get_concurrency() -> int:
    try:
        return max(1, min(2, int(get_setting("lossless_concurrency", "1"))))
    except ValueError:
        return 1


def map_music_path(file_path: str) -> Path:
    if file_path.startswith(str(MUSIC_PATH)):
        return Path(file_path)
    for prefix in HOST_MUSIC_PREFIXES:
        if file_path.startswith(prefix):
            rel = file_path[len(prefix) :].lstrip("/")
            return MUSIC_PATH / rel
    return Path(file_path)


def delete_from_queue(track_id: int) -> None:
    conn = get_db()
    try:
        conn.execute("DELETE FROM authenticity_queue WHERE track_id = ?", (track_id,))
        conn.commit()
    finally:
        conn.close()


def update_queue_retry(track_id: int, status: str) -> None:
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT COALESCE(attempts, 0) AS attempts FROM authenticity_queue WHERE track_id = ?",
            (track_id,),
        ).fetchone()
        current_attempts = int(row["attempts"]) if row else 0
        next_attempts = current_attempts + 1
        days = 1 if next_attempts == 1 else 3 if next_attempts == 2 else 7 if next_attempts == 3 else 14
        conn.execute(
            """
            UPDATE authenticity_queue
               SET attempts = ?,
                   last_status = ?,
                   next_check_at = datetime('now', ?)
             WHERE track_id = ?
            """,
            (next_attempts, status, f"+{days} days", track_id),
        )
        conn.commit()
        logger.info(
            "Track %s: queued for re-check in %s day(s), attempts=%s status=%s",
            track_id,
            days,
            next_attempts,
            status,
        )
    finally:
        conn.close()


def defer_queue(track_id: int, status: str) -> None:
    defer_queue_for(track_id, status, "1 day")


def defer_queue_for(track_id: int, status: str, interval: str) -> None:
    conn = get_db()
    try:
        conn.execute(
            """
            UPDATE authenticity_queue
               SET last_status = ?,
                   next_check_at = datetime('now', ?)
             WHERE track_id = ?
            """,
            (status, f"+{interval}", track_id),
        )
        conn.commit()
        logger.info("Track %s: deferred for %s, status=%s", track_id, interval, status)
    finally:
        conn.close()


def set_lidarr_searching(track_id: int, attempt: int) -> None:
    conn = get_db()
    try:
        conn.execute(
            """
            UPDATE authenticity_queue
               SET last_status = ?,
                   next_check_at = datetime('now', '+6 hours')
             WHERE track_id = ?
            """,
            (f"lidarr_searching:{attempt}", track_id),
        )
        conn.commit()
        logger.info("Track %s: Lidarr search attempt %s queued for check in 6 hours", track_id, attempt)
    finally:
        conn.close()


def upsert_authenticity(track_id: int, analysis: dict[str, Any], spectrogram_path: str | None) -> None:
    conn = get_db()
    try:
        conn.execute(
            """
            INSERT OR REPLACE INTO track_authenticity (
                track_id, verdict, confidence, cutoff_hz, nyquist_hz,
                shelf_db, sharpness, source_guess, sample_rate, channels,
                duration, n_windows_used, error, spectrogram_path,
                method_version, analyzed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            """,
            (
                track_id,
                analysis.get("verdict"),
                analysis.get("confidence"),
                analysis.get("cutoff_hz"),
                analysis.get("nyquist_hz"),
                analysis.get("shelf_db"),
                analysis.get("sharpness"),
                analysis.get("source_guess"),
                analysis.get("sample_rate"),
                analysis.get("channels"),
                analysis.get("duration"),
                analysis.get("n_windows_used"),
                analysis.get("error"),
                spectrogram_path,
                analysis.get("method_version"),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def render_spectrogram_best_effort(track_id: int, file_path: Path) -> str | None:
    try:
        SPECTROGRAM_DIR.mkdir(parents=True, exist_ok=True)
        out_path = SPECTROGRAM_DIR / f"{track_id}.png"
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-y",
                "-i",
                str(file_path),
                "-lavfi",
                "showspectrumpic=s=900x420:legend=1",
                str(out_path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return str(out_path)
    except Exception as exc:
        logger.warning("Track %s: failed to render spectrogram: %s", track_id, exc)
        return None


def next_track() -> sqlite3.Row | None:
    with _in_progress_lock:
        in_prog = list(_in_progress)

    conn = get_db()
    try:
        base_sql = (
            "SELECT aq.track_id, aq.last_status, t.file_path, t.artist, t.album, t.title "
            "FROM authenticity_queue aq "
            "JOIN tracks t ON t.id = aq.track_id "
            "WHERE (aq.next_check_at IS NULL OR aq.next_check_at <= datetime('now')) "
        )
        params: list[Any] = []
        if in_prog:
            placeholders = ",".join("?" * len(in_prog))
            base_sql += f"AND aq.track_id NOT IN ({placeholders}) "
            params.extend(in_prog)
        base_sql += "ORDER BY aq.queued_at LIMIT 1"
        return conn.execute(base_sql, params).fetchone()
    finally:
        conn.close()


def get_daily_count_key() -> str:
    return f"recue_count_{datetime.now().strftime('%Y%m%d')}"


def get_recue_count(key: str) -> int:
    try:
        return max(0, int(get_setting(key, "0")))
    except ValueError:
        return 0


def increment_recue_count(key: str) -> int:
    conn = get_db()
    try:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        current = int(row["value"]) if row and str(row["value"]).isdigit() else 0
        next_value = current + 1
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, str(next_value)),
        )
        conn.commit()
        return next_value
    finally:
        conn.close()


def write_recue_log(track_id: int, source: str, status: str, track_meta: dict[str, str]) -> None:
    try:
        log_recue(
            DB_PATH,
            track_id,
            source,
            status,
            track_meta.get("album") or "",
            track_meta.get("title") or "",
        )
    except Exception as exc:
        logger.warning("Track %s: failed to write recue_log status=%s source=%s: %s", track_id, status, source, exc)


def can_attempt_recue(track_id: int, analysis: dict[str, Any]) -> tuple[bool, str]:
    confidence = float(analysis.get("confidence") or 0.0)
    if confidence < 0.90:
        return False, "low_confidence"
    return True, ""


def can_attempt_musicgrabber_recue(track_id: int) -> tuple[bool, str]:
    if not truthy(get_setting("auto_recue_new_imports", "false")):
        return False, "recue_disabled"
    try:
        cap = max(0, int(get_setting("auto_recue_daily_cap", "50") or "50"))
    except ValueError:
        cap = 50
    count_key = get_daily_count_key()
    if get_recue_count(count_key) >= cap:
        logger.info("Track %s: daily recue cap reached (%s/%s)", track_id, get_recue_count(count_key), cap)
        return False, "cap_reached"
    return True, ""


def run_auto_recue(track_id: int, file_path: Path) -> dict[str, str]:
    count_key = get_daily_count_key()
    count = increment_recue_count(count_key)
    logger.info("Track %s: starting auto-recue attempt %s for %s", track_id, count, file_path)

    row = {"which": "borderline", "source": str(file_path), "target": str(file_path)}
    try:
        with httpx.Client(timeout=60.0) as client:
            result = recue_fakes.process_track(client, row, dry_run=False)
    except Exception as exc:
        result = {"target": str(file_path), "status": f"error:{exc}", "quality": "", "query": ""}

    target = result.get("target") or str(file_path)
    status = result.get("status") or "error:missing_status"
    quality = result.get("quality") or ""
    query = result.get("query") or ""
    try:
        recue_fakes.append_state(target, status, quality, query)
    except Exception as exc:
        logger.error("Track %s: failed to append recue state: %s", track_id, exc)

    logger.info(
        "Track %s: auto-recue result status=%s quality=%s query=%r",
        track_id,
        status,
        quality,
        query,
    )
    return {"status": status, "quality": quality, "query": query}


def parse_lidarr_attempt(status: str | None) -> int | None:
    if not status or not status.startswith("lidarr_searching:"):
        return None
    try:
        return int(status.rsplit(":", 1)[1])
    except ValueError:
        return None


def lidarr_album_id(track_meta: dict[str, str]) -> int | None:
    artist = lidarr_client.find_artist(track_meta.get("artist") or "", lidarr_recue.BASE, lidarr_recue.API_KEY)
    if not artist:
        return None
    album = lidarr_client.find_album(int(artist["id"]), track_meta.get("album") or "", lidarr_recue.BASE, lidarr_recue.API_KEY)
    if not album:
        return None
    return int(album["id"])


def run_musicgrabber_fallback(track_id: int, file_path: Path, track_meta: dict[str, str]) -> None:
    allowed, defer_status = can_attempt_musicgrabber_recue(track_id)
    if not allowed:
        defer_queue(track_id, defer_status)
        return
    result = run_auto_recue(track_id, file_path)
    status = result["status"]
    if status in SUCCESS_RECUE_STATUSES:
        log_status = "fixed" if status in {"placed", "already_present"} else "staged"
        write_recue_log(track_id, "musicgrabber", log_status, track_meta)
        delete_from_queue(track_id)
    elif status in RETRY_RECUE_STATUSES or status.startswith("error:"):
        update_queue_retry(track_id, status)
    else:
        update_queue_retry(track_id, status or "unknown_recue_status")


def process_confirmed_transcode(track_id: int, file_path: Path, track_meta: dict[str, str], last_status: str | None) -> None:
    lidarr_recue.configure(get_setting("lidarr_url", "http://10.0.0.13:8787"), get_setting("lidarr_api_key", "2cecee10715a4c1dbe8daa16226f7ed7"))
    auto_recue_enabled = truthy(get_setting("auto_recue_new_imports", "false"))
    lidarr_enabled = truthy(get_setting("lidarr_recue_enabled", "true"))
    dry_run = truthy(get_setting("lidarr_dry_run", "false"))

    lidarr_attempt = parse_lidarr_attempt(last_status)
    if lidarr_attempt is not None:
        if not auto_recue_enabled:
            defer_queue(track_id, last_status or "lidarr_searching:1")
            return
        album_id = lidarr_album_id(track_meta)
        if album_id is None:
            logger.info("Track %s: Lidarr album disappeared while checking result; falling back to MusicGrabber", track_id)
            run_musicgrabber_fallback(track_id, file_path, track_meta)
            return
        result = lidarr_recue.check_lidarr_result(track_meta, album_id)
        if result == "placed_lidarr":
            write_recue_log(track_id, "lidarr", "fixed", track_meta)
            delete_from_queue(track_id)
            return
        if lidarr_attempt < 2:
            set_lidarr_searching(track_id, lidarr_attempt + 1)
            return
        logger.info("Track %s: Lidarr pending after %s checks; falling back to MusicGrabber", track_id, lidarr_attempt)
        run_musicgrabber_fallback(track_id, file_path, track_meta)
        return

    if not auto_recue_enabled:
        defer_queue(track_id, "recue_disabled")
        return

    if lidarr_enabled:
        allowed, defer_status = can_attempt_musicgrabber_recue(track_id)
        if not allowed:
            defer_queue(track_id, defer_status)
            return
        result = lidarr_recue.recue_via_lidarr(track_meta, str(file_path), dry_run=dry_run)
        if result == "lidarr_searching":
            if not dry_run:
                increment_recue_count(get_daily_count_key())
                write_recue_log(track_id, "lidarr", "triggered", track_meta)
            set_lidarr_searching(track_id, 1)
            return
        if result == "lidarr_inflight":
            defer_queue_for(track_id, result, "6 hours")
            return
        if result == "lidarr_no_album":
            logger.info("Track %s: Lidarr had no album; falling back to MusicGrabber", track_id)
            run_musicgrabber_fallback(track_id, file_path, track_meta)
            return
        update_queue_retry(track_id, result)
        return

    run_musicgrabber_fallback(track_id, file_path, track_meta)


def analyze_track(track_id: int, db_file_path: str, track_meta: dict[str, str] | None = None, last_status: str | None = None) -> None:
    track_meta = dict(track_meta or {})
    track_meta.setdefault("file_path", db_file_path)
    file_path = map_music_path(db_file_path)
    if parse_lidarr_attempt(last_status) is not None:
        process_confirmed_transcode(track_id, file_path, track_meta, last_status)
        return
    if not db_file_path.lower().endswith(".flac"):
        logger.info("Track %s: non-FLAC in authenticity queue, removing: %s", track_id, db_file_path)
        delete_from_queue(track_id)
        return
    if not file_path.exists():
        logger.warning("Track %s: file not found, deferring: %s", track_id, file_path)
        defer_queue(track_id, "file_missing")
        return

    analysis = lossless_detect.analyze_flac(file_path)
    verdict = str(analysis.get("verdict") or "unknown")

    spectrogram_path = None
    if verdict != "lossless":
        spectrogram_path = render_spectrogram_best_effort(track_id, file_path)
    upsert_authenticity(track_id, analysis, spectrogram_path)

    logger.info(
        "Track %s: analyzed verdict=%s confidence=%s cutoff=%s",
        track_id,
        verdict,
        analysis.get("confidence"),
        analysis.get("cutoff_hz"),
    )

    if verdict in TERMINAL_VERDICTS:
        delete_from_queue(track_id)
        return

    if verdict != "transcode":
        update_queue_retry(track_id, verdict)
        return

    allowed, defer_status = can_attempt_recue(track_id, analysis)
    if not allowed:
        defer_queue(track_id, defer_status)
        return

    process_confirmed_transcode(track_id, file_path, track_meta, last_status)


def _worker_thread(worker_id: int) -> None:
    logger.info("Worker thread %s starting", worker_id)
    while True:
        try:
            row = next_track()
            if row is None:
                time.sleep(POLL_INTERVAL)
                continue

            track_id = int(row["track_id"])
            with _in_progress_lock:
                if track_id in _in_progress:
                    time.sleep(0.1)
                    continue
                _in_progress.add(track_id)
            try:
                track_meta = {
                    "artist": str(row["artist"] or ""),
                    "album": str(row["album"] or ""),
                    "title": str(row["title"] or ""),
                    "file_path": str(row["file_path"]),
                }
                analyze_track(track_id, str(row["file_path"]), track_meta, str(row["last_status"] or ""))
                time.sleep(INTER_TRACK_SLEEP)
            finally:
                with _in_progress_lock:
                    _in_progress.discard(track_id)
        except Exception as exc:
            logger.error("Worker %s error: %s", worker_id, exc)
            time.sleep(10)


def run_worker() -> None:
    configure_recue()
    concurrency = get_concurrency()
    logger.info(
        "Lossless analyzer starting: DB=%s MUSIC=%s MG=%s concurrency=%s",
        DB_PATH,
        MUSIC_PATH,
        MUSICGRABBER_URL,
        concurrency,
    )
    threads = []
    for i in range(concurrency):
        thread = threading.Thread(
            target=_worker_thread,
            args=(i,),
            daemon=True,
            name=f"lossless-worker-{i}",
        )
        thread.start()
        threads.append(thread)

    for thread in threads:
        thread.join()


if __name__ == "__main__":
    run_worker()
