"""
Library Reorganizer Worker
Scans the music library and inbox directories, moves files to correct
Artist/Album/Track structure based on embedded tags.

Uses mutagen for tag reading (available in plex-dedup container).
Paths configurable via env vars MUSIC_ROOT and INBOX_DIRS.
"""

import logging
import os
import re
import shutil
from pathlib import Path
from typing import Callable, Optional

from mutagen import File as MutagenFile

logger = logging.getLogger(__name__)

MUSIC_ROOT = os.environ.get("MUSIC_ROOT", "/music/FLAC")
_raw_inbox = os.environ.get("INBOX_DIRS", "/music/MP3s,/music/iTunes,/music/Singles")
INBOX_DIRS = [d.strip() for d in _raw_inbox.split(",") if d.strip()]
INBOX_EXTS = {".flac"}
AUDIO_EXTS = {".flac", ".mp3", ".m4a", ".ogg", ".opus", ".wav", ".wma", ".aac"}
SKIP_DIRS = {"@eaDir", ".Trash", "#recycle"}
VA_NAMES = {"v.a.", "va", "various artists", "various", "compilation"}

UNSAFE_CHARS = re.compile(r'[<>:"/\\|?*]')


def sanitize(name: str) -> str:
    name = UNSAFE_CHARS.sub("", name)
    name = name.strip(". ")
    return name if name else "Unknown"


def get_tags(filepath: str) -> Optional[dict]:
    try:
        audio = MutagenFile(filepath, easy=True)
        if audio is None:
            return None
        tags = {}
        for key in ("albumartist", "artist", "album", "title", "tracknumber"):
            val = audio.get(key)
            if val:
                tags[key] = val[0] if isinstance(val, list) else str(val)
        return tags
    except Exception as e:
        logger.debug(f"Tag read failed: {filepath}: {e}")
        return None


def resolve_artist(tags: dict) -> Optional[str]:
    album_artist = (tags.get("albumartist") or "").strip()
    artist = (tags.get("artist") or "").strip()
    if album_artist.lower() in VA_NAMES:
        return artist if artist else None
    return album_artist if album_artist else (artist if artist else None)


def build_dest_path(tags: dict, src_path: str) -> Optional[str]:
    artist = resolve_artist(tags)
    album = (tags.get("album") or "").strip()
    title = (tags.get("title") or "").strip()
    track = (tags.get("tracknumber") or "").strip()

    if not artist or not album:
        return None

    artist = sanitize(artist)
    album = sanitize(album)
    ext = Path(src_path).suffix

    if title:
        title = sanitize(title)
        if track:
            track_num = track.split("/")[0].zfill(2)
            filename = f"{track_num} - {title}{ext}"
        else:
            filename = f"{title}{ext}"
    else:
        filename = Path(src_path).name

    dest = os.path.join(MUSIC_ROOT, artist, album, filename)
    if os.path.abspath(src_path) == os.path.abspath(dest):
        return None
    return dest


def check_dest_conflict(dest: str) -> str:
    if not os.path.exists(dest):
        return dest
    base, ext = os.path.splitext(dest)
    i = 2
    while os.path.exists(f"{base} ({i}){ext}"):
        i += 1
    return f"{base} ({i}){ext}"


def _clean_empty(path: str):
    if not os.path.isdir(path):
        return
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            if Path(f).suffix.lower() in AUDIO_EXTS:
                return
    try:
        shutil.rmtree(path)
        logger.info(f"Removed empty dir: {path}")
    except Exception as e:
        logger.warning(f"Could not remove {path}: {e}")


def _process_file(src: str, stats: dict, emptied_dirs: set, dry_run: bool):
    tags = get_tags(src)
    if not tags:
        stats["skipped"] += 1
        logger.info(f"Skip (no tags): {src}")
        return

    dest = build_dest_path(tags, src)
    if dest is None:
        artist = resolve_artist(tags)
        album = (tags.get("album") or "").strip()
        if not artist or not album:
            missing = "artist" if not artist else "album"
            stats["skipped"] += 1
            logger.info(f"Skip (no {missing}): {src}")
        else:
            stats["already_ok"] += 1
        return

    dest = check_dest_conflict(dest)
    if dry_run:
        stats["moved"] += 1
        return

    try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.move(src, dest)
        stats["moved"] += 1
        rel_src = os.path.relpath(src, MUSIC_ROOT) if src.startswith(MUSIC_ROOT) else src
        rel_dest = os.path.relpath(dest, MUSIC_ROOT)
        logger.info(f"Moved: {rel_src} -> {rel_dest}")
        emptied_dirs.add(os.path.dirname(src))
        # Update DB so scan doesn't create a stale 'deleted' record + new duplicate
        try:
            from database import get_db
            with get_db() as db:
                # Remove any stale (trashed/deleted) record at the new path to avoid
                # UNIQUE constraint violation when we update the active record's path
                db.execute(
                    "DELETE FROM tracks WHERE file_path = ? AND status != 'active'",
                    (dest,),
                )
                db.execute(
                    "UPDATE tracks SET file_path = ?, scanned_at = CURRENT_TIMESTAMP"
                    " WHERE file_path = ? AND status = 'active'",
                    (dest, src),
                )
        except Exception as db_err:
            logger.warning(f"DB path update failed after move ({src} -> {dest}): {db_err}")
    except Exception as e:
        stats["errors"] += 1
        logger.error(f"Move failed {src}: {e}")


def run_reorg(update_fn: Optional[Callable[[dict], None]] = None, dry_run: bool = False) -> dict:
    """
    Run the library reorganization.
    update_fn: called with progress dict {phase, total, progress, current_file}.
    Returns final stats dict.
    """
    stats = {
        "total": 0, "moved": 0, "skipped": 0,
        "errors": 0, "already_ok": 0,
        "inbox_moved": 0, "inbox_skipped": 0,
    }
    emptied_dirs: set = set()

    def _upd(phase: str, current_file: str = ""):
        if update_fn:
            update_fn({
                "phase": phase,
                "total": stats["total"],
                "progress": stats["already_ok"] + stats["moved"] + stats["skipped"],
                "current_file": current_file,
                "moved": stats["moved"],
                "skipped": stats["skipped"],
                "errors": stats["errors"],
                "already_ok": stats["already_ok"],
                "inbox_moved": stats["inbox_moved"],
            })

    # Main library scan
    _upd("scanning")
    for root, dirs, files in os.walk(MUSIC_ROOT, topdown=True):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for fname in sorted(files):
            if Path(fname).suffix.lower() not in AUDIO_EXTS or fname.startswith("."):
                continue
            src = os.path.join(root, fname)
            stats["total"] += 1
            _upd("scanning", src)
            _process_file(src, stats, emptied_dirs, dry_run)

    # Inbox scan — pull stray FLACs into library
    _upd("inbox")
    for inbox in INBOX_DIRS:
        if not os.path.isdir(inbox):
            continue
        for root, dirs, files in os.walk(inbox, topdown=True):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
            for fname in sorted(files):
                if Path(fname).suffix.lower() not in INBOX_EXTS or fname.startswith("."):
                    continue
                src = os.path.join(root, fname)
                stats["total"] += 1
                moved_before = stats["moved"]
                skipped_before = stats["skipped"]
                _upd("inbox", src)
                _process_file(src, stats, emptied_dirs, dry_run)
                if stats["moved"] > moved_before:
                    stats["inbox_moved"] += 1
                if stats["skipped"] > skipped_before:
                    stats["inbox_skipped"] += 1

    # Clean empty directories
    _upd("cleaning")
    if not dry_run:
        for entry in sorted(os.listdir(MUSIC_ROOT)):
            full = os.path.join(MUSIC_ROOT, entry)
            if os.path.isdir(full) and entry not in SKIP_DIRS:
                _clean_empty(full)
        for d in sorted(emptied_dirs, key=len, reverse=True):
            if os.path.isdir(d) and d != MUSIC_ROOT:
                _clean_empty(d)

    _upd("complete")
    logger.info(
        f"Reorg complete: scanned={stats['total']} moved={stats['moved']} "
        f"ok={stats['already_ok']} skipped={stats['skipped']} errors={stats['errors']} "
        f"inbox_moved={stats['inbox_moved']} inbox_skipped={stats['inbox_skipped']}"
    )
    return stats
