"""
Tag snapshot and rollback — backup existing metadata before any write,
and restore from snapshot when needed.
"""

import hashlib
import logging

from mutagen import File as MutagenFile
from mutagen.flac import FLAC
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4

from database import get_db

logger = logging.getLogger(__name__)


def snapshot_tags(track_id: int, file_path: str, fp_result_id: int) -> int | None:
    """
    Read all current tags via mutagen, store in tag_snapshots.
    Returns snapshot_id or None on failure.
    """
    try:
        audio = MutagenFile(file_path)
        if audio is None:
            logger.warning(f"Cannot open {file_path} for snapshot")
            return None

        tags = _read_all_tags(audio)
        cover_art_bytes, cover_art_hash = _read_cover_art(audio)

        with get_db() as db:
            cursor = db.execute(
                """INSERT INTO tag_snapshots
                   (track_id, fingerprint_result_id,
                    original_artist, original_title, original_album,
                    original_album_artist, original_year, original_track_number,
                    original_disc_number, original_genre, original_isrc,
                    original_label, original_composer,
                    original_cover_art_hash, original_cover_art)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    track_id,
                    fp_result_id,
                    tags.get("artist"),
                    tags.get("title"),
                    tags.get("album"),
                    tags.get("album_artist"),
                    tags.get("year"),
                    tags.get("track_number"),
                    tags.get("disc_number"),
                    tags.get("genre"),
                    tags.get("isrc"),
                    tags.get("label"),
                    tags.get("composer"),
                    cover_art_hash,
                    cover_art_bytes,
                ),
            )
            return cursor.lastrowid

    except Exception as e:
        logger.error(f"Snapshot failed for track {track_id}: {e}")
        return None


def rollback_tags(snapshot_id: int) -> bool:
    """Restore tags from snapshot. Write original values back via mutagen."""
    try:
        with get_db() as db:
            row = db.execute(
                """SELECT ts.*, t.file_path
                   FROM tag_snapshots ts
                   JOIN tracks t ON t.id = ts.track_id
                   WHERE ts.id = ?""",
                (snapshot_id,),
            ).fetchone()

        if not row:
            logger.warning(f"Snapshot {snapshot_id} not found")
            return False

        file_path = row["file_path"]
        audio = MutagenFile(file_path)
        if audio is None:
            logger.error(f"Cannot open {file_path} for rollback")
            return False

        metadata = {
            "artist": row["original_artist"],
            "title": row["original_title"],
            "album": row["original_album"],
            "album_artist": row["original_album_artist"],
            "date": str(row["original_year"]) if row["original_year"] else None,
            "track_number": row["original_track_number"],
            "disc_number": row["original_disc_number"],
            "genre": row["original_genre"],
        }

        from tagger import write_metadata

        write_metadata(file_path, metadata, row["original_cover_art"])
        logger.info(f"Rolled back tags for {file_path}")

        # Update fingerprint_result status
        with get_db() as db:
            fp_id = row["fingerprint_result_id"]
            if fp_id:
                db.execute(
                    "UPDATE fingerprint_results SET status='rolled_back', "
                    "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (fp_id,),
                )

        return True

    except Exception as e:
        logger.error(f"Rollback failed for snapshot {snapshot_id}: {e}")
        return False


def rollback_batch(fp_result_ids: list[int]) -> dict:
    """Bulk rollback. Returns {success, failed, errors}."""
    success = 0
    failed = 0
    errors = []

    for fp_id in fp_result_ids:
        with get_db() as db:
            snap = db.execute(
                "SELECT id FROM tag_snapshots WHERE fingerprint_result_id = ?",
                (fp_id,),
            ).fetchone()

        if not snap:
            failed += 1
            errors.append(f"No snapshot for fingerprint_result {fp_id}")
            continue

        if rollback_tags(snap["id"]):
            success += 1
        else:
            failed += 1
            errors.append(f"Rollback failed for snapshot {snap['id']}")

    return {"success": success, "failed": failed, "errors": errors}


def _read_all_tags(audio) -> dict:
    """Read common tags from any mutagen file object."""
    tags = {}

    if isinstance(audio, FLAC):
        tags["artist"] = _first(audio.get("artist"))
        tags["title"] = _first(audio.get("title"))
        tags["album"] = _first(audio.get("album"))
        tags["album_artist"] = _first(audio.get("albumartist"))
        tags["genre"] = _first(audio.get("genre"))
        tags["isrc"] = _first(audio.get("isrc"))
        tags["composer"] = _first(audio.get("composer"))
        date = _first(audio.get("date"))
        tags["year"] = _parse_year(date)
        tn = _first(audio.get("tracknumber"))
        tags["track_number"] = _parse_track_num(tn)
        dn = _first(audio.get("discnumber"))
        tags["disc_number"] = _parse_track_num(dn)

    elif isinstance(audio, MP3):
        if audio.tags:
            tags["artist"] = _id3_text(audio.tags, "TPE1")
            tags["title"] = _id3_text(audio.tags, "TIT2")
            tags["album"] = _id3_text(audio.tags, "TALB")
            tags["album_artist"] = _id3_text(audio.tags, "TPE2")
            tags["genre"] = _id3_text(audio.tags, "TCON")
            tags["composer"] = _id3_text(audio.tags, "TCOM")
            date = _id3_text(audio.tags, "TDRC")
            tags["year"] = _parse_year(date)
            tn = _id3_text(audio.tags, "TRCK")
            tags["track_number"] = _parse_track_num(tn)
            dn = _id3_text(audio.tags, "TPOS")
            tags["disc_number"] = _parse_track_num(dn)

    elif isinstance(audio, MP4):
        if audio.tags:
            tags["artist"] = _first(audio.tags.get("\xa9ART"))
            tags["title"] = _first(audio.tags.get("\xa9nam"))
            tags["album"] = _first(audio.tags.get("\xa9alb"))
            tags["album_artist"] = _first(audio.tags.get("aART"))
            tags["genre"] = _first(audio.tags.get("\xa9gen"))
            tags["composer"] = _first(audio.tags.get("\xa9wrt"))
            date = _first(audio.tags.get("\xa9day"))
            tags["year"] = _parse_year(date)
            trkn = audio.tags.get("trkn")
            if trkn and isinstance(trkn[0], tuple):
                tags["track_number"] = trkn[0][0]
            disk = audio.tags.get("disk")
            if disk and isinstance(disk[0], tuple):
                tags["disc_number"] = disk[0][0]

    else:
        # Vorbis/Opus
        tags["artist"] = _first(audio.get("artist"))
        tags["title"] = _first(audio.get("title"))
        tags["album"] = _first(audio.get("album"))
        tags["album_artist"] = _first(audio.get("albumartist"))
        tags["genre"] = _first(audio.get("genre"))
        tags["isrc"] = _first(audio.get("isrc"))
        tags["composer"] = _first(audio.get("composer"))
        date = _first(audio.get("date"))
        tags["year"] = _parse_year(date)
        tn = _first(audio.get("tracknumber"))
        tags["track_number"] = _parse_track_num(tn)
        dn = _first(audio.get("discnumber"))
        tags["disc_number"] = _parse_track_num(dn)

    # Remove None/empty values
    tags["label"] = None  # Not stored in standard tags
    return tags


def _read_cover_art(audio) -> tuple[bytes | None, str | None]:
    """Read embedded cover art. Returns (bytes, sha256_hash)."""
    art_bytes = None

    if isinstance(audio, FLAC):
        pics = audio.pictures
        if pics:
            art_bytes = pics[0].data
    elif isinstance(audio, MP3):
        if audio.tags:
            for key in audio.tags:
                if key.startswith("APIC"):
                    art_bytes = audio.tags[key].data
                    break
    elif isinstance(audio, MP4):
        if audio.tags:
            covr = audio.tags.get("covr")
            if covr:
                art_bytes = bytes(covr[0])

    if art_bytes:
        art_hash = hashlib.sha256(art_bytes).hexdigest()
        return art_bytes, art_hash
    return None, None


def _first(val) -> str | None:
    """Get first element from a tag list, or None."""
    if val is None:
        return None
    if isinstance(val, list) and len(val) > 0:
        return str(val[0])
    if isinstance(val, str):
        return val
    return str(val)


def _id3_text(tags, key: str) -> str | None:
    """Get text from an ID3 tag frame."""
    frame = tags.get(key)
    if frame and hasattr(frame, "text") and frame.text:
        return str(frame.text[0])
    return None


def _parse_year(date_str: str | None) -> int | None:
    """Extract year from date string."""
    if not date_str:
        return None
    try:
        return int(str(date_str)[:4])
    except (ValueError, TypeError):
        return None


def _parse_track_num(tn_str: str | None) -> int | None:
    """Parse track number from 'N' or 'N/M' format."""
    if not tn_str:
        return None
    try:
        return int(str(tn_str).split("/")[0])
    except (ValueError, TypeError):
        return None
