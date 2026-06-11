#!/usr/bin/env python3
"""Bulk Lidarr recue for known fake FLAC transcodes."""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import lidarr_client as lc  # noqa: E402
from database import log_recue  # noqa: E402


BASE = os.environ.get("LIDARR_URL", "http://10.0.0.13:8787")
API_KEY = os.environ.get("LIDARR_API_KEY", "2cecee10715a4c1dbe8daa16226f7ed7")
DB_PATH = Path(os.environ.get("DB_PATH", "/data/music-machine.db"))
if not DB_PATH.exists() and (REPO_ROOT / "data/music-machine.db").exists():
    DB_PATH = REPO_ROOT / "data/music-machine.db"
DATA_DIR = REPO_ROOT / "data"
MANIFEST_PATH = DATA_DIR / "fake-flac-trash-manifest-20260611.json"
STATE_PATH = DATA_DIR / "lidarr_recue_state.jsonl"

COMMANDS_PER_MINUTE = 3


def normalize(value: str) -> str:
    value = str(value or "").lower()
    value = re.sub(r"\b(feat|featuring|ft)\.?\b.*$", "", value)
    value = re.sub(r"^\s*\d{1,3}\s*[-.]\s*", "", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def loose_match(candidate: str, target: str) -> bool:
    cand = normalize(candidate)
    targ = normalize(target)
    if not cand or not targ:
        return False
    if cand == targ or cand in targ or targ in cand:
        return True
    cand_tokens = set(cand.split())
    targ_tokens = set(targ.split())
    return bool(cand_tokens and targ_tokens and len(cand_tokens & targ_tokens) / max(len(cand_tokens), len(targ_tokens)) >= 0.60)


def read_manifest() -> dict[str, dict[str, Any]]:
    if not MANIFEST_PATH.exists():
        return {}
    try:
        with MANIFEST_PATH.open("r", encoding="utf-8") as fh:
            rows = json.load(fh)
    except Exception:
        return {}
    by_orig: dict[str, dict[str, Any]] = {}
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict) and row.get("orig"):
                by_orig[str(row["orig"])] = row
    return by_orig


def load_transcodes() -> list[dict[str, Any]]:
    if not DB_PATH.exists():
        print(f"DB not found: {DB_PATH}", flush=True)
        return []
    manifest = read_manifest()
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT t.id AS track_id, t.artist, t.album, t.title, t.file_path, ta.verdict
              FROM track_authenticity ta
              JOIN tracks t ON t.id = ta.track_id
             WHERE ta.verdict = 'transcode'
             ORDER BY COALESCE(t.album_artist, t.artist), t.album, t.title
            """
        ).fetchall()
    finally:
        conn.close()

    work: list[dict[str, Any]] = []
    for row in rows:
        file_path = str(row["file_path"] or "")
        manifest_row = manifest.get(file_path) or {}
        work.append(
            {
                "track_id": int(row["track_id"]),
                "artist": str(row["artist"] or manifest_row.get("artist") or ""),
                "album": str(row["album"] or manifest_row.get("album") or ""),
                "title": str(row["title"] or manifest_row.get("title") or ""),
                "file_path": file_path,
                "trash_path": str(manifest_row.get("trash") or ""),
            }
        )
    return work


def album_key(row: dict[str, Any]) -> tuple[str, str]:
    return (normalize(str(row.get("artist") or "")), normalize(str(row.get("album") or "")))


def load_completed_albums() -> set[str]:
    completed: set[str] = set()
    if not STATE_PATH.exists():
        return completed
    with STATE_PATH.open("r", encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            status = str(row.get("status") or "")
            key = str(row.get("album_key") or "")
            if key and status in {"lidarr_searching", "lidarr_inflight", "lidarr_no_album"}:
                completed.add(key)
    return completed


def append_state(album_key_value: str, artist: str, album: str, status: str, details: dict[str, Any] | None = None) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    row = {
        "album_key": album_key_value,
        "artist": artist,
        "album": album,
        "status": status,
        "details": details or {},
        "ts": int(time.time()),
    }
    with STATE_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")


def enqueue_lidarr_rechecks(tracks: list[dict[str, Any]]) -> None:
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=30)
    try:
        for track in tracks:
            track_id = int(track["track_id"])
            conn.execute("INSERT OR IGNORE INTO authenticity_queue (track_id) VALUES (?)", (track_id,))
            conn.execute(
                """
                UPDATE authenticity_queue
                   SET last_status = 'lidarr_searching:1',
                       next_check_at = datetime('now', '+6 hours')
                 WHERE track_id = ?
                """,
                (track_id,),
            )
            log_recue(
                conn,
                track_id,
                "lidarr",
                "triggered",
                str(track.get("album") or ""),
                str(track.get("title") or ""),
            )
        conn.commit()
    finally:
        conn.close()


def trackfile_title(trackfile: dict[str, Any]) -> str:
    if trackfile.get("title"):
        return str(trackfile["title"])
    tracks = trackfile.get("tracks")
    if isinstance(tracks, list):
        return " ".join(str(track.get("title") or "") for track in tracks if isinstance(track, dict))
    return Path(str(trackfile.get("path") or "")).stem


def matching_trackfiles(trackfiles: list[dict[str, Any]], fake_tracks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    matches: dict[int, dict[str, Any]] = {}
    for fake in fake_tracks:
        basename = Path(str(fake.get("file_path") or "")).name
        title = str(fake.get("title") or "")
        for trackfile in trackfiles:
            trackfile_id = trackfile.get("id")
            if trackfile_id is None:
                continue
            path = str(trackfile.get("path") or "")
            if (basename and Path(path).name == basename) or loose_match(trackfile_title(trackfile), title) or loose_match(Path(path).stem, title):
                matches[int(trackfile_id)] = trackfile
    return list(matches.values())


def queue_total_records() -> int:
    data = lc._request("GET", "/queue", BASE, API_KEY, params={"pageSize": 1})  # noqa: SLF001 - thin CLI wrapper
    if isinstance(data, dict):
        return int(data.get("totalRecords") or len(data.get("records") or []))
    return len(data or [])


def wait_for_queue_threshold(threshold: int, dry_run: bool) -> None:
    while True:
        total = queue_total_records()
        print(f"queue totalRecords={total} threshold={threshold}", flush=True)
        if total <= threshold:
            return
        print("queue above threshold; sleeping 60s before retry", flush=True)
        if dry_run:
            return
        time.sleep(60)


def process_album(album_key_value: str, tracks: list[dict[str, Any]], args: argparse.Namespace, command_times: list[float]) -> str:
    first = tracks[0]
    artist_name = str(first.get("artist") or "")
    album_title = str(first.get("album") or "")
    print(f"\nAlbum: {artist_name} - {album_title} ({len(tracks)} fake track(s))", flush=True)

    artist = lc.find_artist(artist_name, BASE, API_KEY)
    if not artist:
        print("  no Lidarr artist match", flush=True)
        return "lidarr_no_album"
    album = lc.find_album(int(artist["id"]), album_title, BASE, API_KEY)
    if not album:
        print("  no Lidarr album match", flush=True)
        return "lidarr_no_album"
    album_id = int(album["id"])

    if lc.album_inflight(album_id, BASE, API_KEY):
        print(f"  album {album_id} inflight; skipped", flush=True)
        return "lidarr_inflight"

    trackfiles = lc.album_trackfiles(album_id, BASE, API_KEY)
    matched = matching_trackfiles(trackfiles, tracks)
    print(f"  matched {len(matched)} trackfile(s) for deleteFiles=false", flush=True)
    for trackfile in matched:
        print(f"  delete trackfile {trackfile.get('id')} deleteFiles=false path={trackfile.get('path')}", flush=True)
        lc.delete_trackfile(int(trackfile["id"]), BASE, API_KEY, delete_files=False)

    print(f"  ensure monitored lossless album={album_id} qualityProfileId=2", flush=True)
    lc.ensure_monitored_lossless(artist, album, BASE, API_KEY)

    wait_for_queue_threshold(args.queue_threshold, args.dry_run)
    now = time.time()
    command_times[:] = [stamp for stamp in command_times if now - stamp < 60.0]
    if len(command_times) >= COMMANDS_PER_MINUTE:
        sleep_for = max(0.0, 60.0 - (now - command_times[0]))
        print(f"  rate limit: sleeping {sleep_for:.1f}s before AlbumSearch", flush=True)
        if not args.dry_run:
            time.sleep(sleep_for)
    print(f"  trigger AlbumSearch album={album_id}", flush=True)
    command_id = lc.trigger_album_search(album_id, BASE, API_KEY)
    command_times.append(time.time())
    print(f"  command_id={command_id}", flush=True)
    if not args.dry_run:
        enqueue_lidarr_rechecks(tracks)
        print(f"  queued {len(tracks)} track(s) for Lidarr verification", flush=True)
    return "lidarr_searching"


def main() -> int:
    parser = argparse.ArgumentParser(description="Bulk Lidarr recue for fake FLAC transcodes")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", default=True, help="plan only; default")
    mode.add_argument("--execute", action="store_true", help="perform Lidarr mutations")
    parser.add_argument("--limit", type=int, help="process only N unique albums")
    parser.add_argument("--queue-threshold", type=int, default=900)
    args = parser.parse_args()
    if args.execute:
        args.dry_run = False

    lc.DRY_RUN = args.dry_run
    print(f"mode={'DRY-RUN' if args.dry_run else 'EXECUTE'} base={BASE} db={DB_PATH}", flush=True)

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in load_transcodes():
        key = album_key(row)
        if key[0] and key[1]:
            grouped[key].append(row)

    completed = load_completed_albums()
    items = [(key, tracks) for key, tracks in grouped.items() if "|".join(key) not in completed]
    if args.limit is not None:
        items = items[: args.limit]
    print(f"unique albums to process: {len(items)}", flush=True)

    command_times: list[float] = []
    counts: dict[str, int] = defaultdict(int)
    for key, tracks in items:
        key_value = "|".join(key)
        try:
            status = process_album(key_value, tracks, args, command_times)
        except Exception as exc:
            status = f"error:{exc}"
            print(f"  {status}", flush=True)
        counts[status] += 1
        if not args.dry_run:
            append_state(key_value, str(tracks[0].get("artist") or ""), str(tracks[0].get("album") or ""), status)

    print(f"\nsummary: {dict(sorted(counts.items()))}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
