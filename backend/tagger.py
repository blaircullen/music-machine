"""
MetaTagger — Acoustic fingerprint → MusicBrainz → CoverArt → mutagen write.

Pipeline: fpcalc fingerprint → AcoustID lookup → MusicBrainz recording →
          CoverArt Archive image → write tags + art back to file.
"""

import hashlib
import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Iterator

import musicbrainzngs
from mutagen import File as MutagenFile
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, TALB, TDRC, TIT2, TPE1, TRCK, TXXX
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover

from scanner import AUDIO_EXTENSIONS

logger = logging.getLogger(__name__)

ACOUSTID_API_KEY = os.environ.get("ACOUSTID_API_KEY", "Yx40zTgSFD")
ACOUSTID_MIN_SCORE = 0.5

# Set a descriptive user-agent per MusicBrainz API requirements
musicbrainzngs.set_useragent("MusicMachine-MetaTagger", "1.0", "https://github.com/blaircullen/music-machine")


# ---------------------------------------------------------------------------
# AcoustID lookup
# ---------------------------------------------------------------------------


def lookup_acoustid(fingerprint: str, duration: float) -> list[dict]:
    """
    Query AcoustID API for matching MusicBrainz recording IDs.
    Returns list of {recording_id, score} sorted by score descending.
    """
    import urllib.request
    import urllib.parse

    post_data = urllib.parse.urlencode({
        "client": ACOUSTID_API_KEY,
        "fingerprint": fingerprint,
        "duration": int(duration),
        "meta": "recordings",
        "format": "json",
    }).encode("utf-8")
    url = "https://api.acoustid.org/v2/lookup"

    try:
        req = urllib.request.Request(url, data=post_data, headers={"User-Agent": "MusicMachine-MetaTagger/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        logger.warning(f"AcoustID lookup failed: {e}")
        return []

    results = []
    for result in data.get("results", []):
        score = result.get("score", 0)
        if score < ACOUSTID_MIN_SCORE:
            continue
        for recording in result.get("recordings", []):
            rec_id = recording.get("id")
            if rec_id:
                results.append({"recording_id": rec_id, "score": score})

    # Deduplicate by recording_id, keep highest score
    seen = {}
    for r in results:
        rid = r["recording_id"]
        if rid not in seen or r["score"] > seen[rid]["score"]:
            seen[rid] = r
    return sorted(seen.values(), key=lambda x: x["score"], reverse=True)


# ---------------------------------------------------------------------------
# MusicBrainz metadata lookup
# ---------------------------------------------------------------------------


def lookup_musicbrainz(recording_id: str) -> dict | None:
    """
    Fetch recording metadata from MusicBrainz.
    Returns dict with artist, title, album, date, track_number, total_tracks,
    release_group_id, release_id, or None on failure.
    """
    try:
        result = musicbrainzngs.get_recording_by_id(
            recording_id,
            includes=["releases", "artist-credits"],
        )
    except Exception as e:
        logger.warning(f"MusicBrainz lookup failed for {recording_id}: {e}")
        return None

    recording = result.get("recording", {})

    # Artist
    artist_credits = recording.get("artist-credit", [])
    artist = ""
    for credit in artist_credits:
        if isinstance(credit, dict):
            artist += credit.get("artist", {}).get("name", "")
            artist += credit.get("joinphrase", "")

    title = recording.get("title", "")

    # Pick best release: prefer "Album" type, then earliest official release
    releases = recording.get("release-list", [])
    best_release = _pick_best_release(releases)

    if not best_release:
        return {
            "artist": artist,
            "title": title,
            "album": "",
            "date": "",
            "track_number": None,
            "total_tracks": None,
            "release_group_id": None,
            "release_id": None,
        }

    album = best_release.get("title", "")
    date = best_release.get("date", "")
    release_id = best_release.get("id", "")

    # Release group — not embedded in recording query results,
    # so fetch from the release itself
    release_group_id = None
    rel_group = best_release.get("release-group", {})
    release_group_id = rel_group.get("id")
    if not release_group_id and release_id:
        try:
            rel_result = musicbrainzngs.get_release_by_id(
                release_id, includes=["release-groups"]
            )
            rel_group = rel_result.get("release", {}).get("release-group", {})
            release_group_id = rel_group.get("id")
        except Exception as e:
            logger.debug(f"Release group lookup failed for {release_id}: {e}")

    # Track position
    track_number = None
    total_tracks = None
    medium_list = best_release.get("medium-list", [])
    for medium in medium_list:
        track_list = medium.get("track-list", [])
        for track in track_list:
            if track.get("recording", {}).get("id") == recording_id:
                try:
                    track_number = int(track.get("number", 0))
                except (ValueError, TypeError):
                    pass
                try:
                    total_tracks = int(medium.get("track-count", 0))
                except (ValueError, TypeError):
                    pass
                break

    return {
        "artist": artist,
        "title": title,
        "album": album,
        "date": date[:4] if date else "",  # Year only
        "track_number": track_number,
        "total_tracks": total_tracks,
        "release_group_id": release_group_id,
        "release_id": release_id,
    }


def _pick_best_release(releases: list[dict]) -> dict | None:
    """Pick best release: prefer Album type over Compilation/Single, then earliest."""
    if not releases:
        return None

    # Categorize
    albums = []
    others = []
    for rel in releases:
        rg = rel.get("release-group", {})
        rg_type = (rg.get("primary-type") or rg.get("type") or "").lower()
        status = (rel.get("status") or "").lower()
        if rg_type == "album" and status in ("official", ""):
            albums.append(rel)
        else:
            others.append(rel)

    candidates = albums if albums else others

    # Sort by date (earliest first), missing dates go last
    def date_key(r):
        d = r.get("date", "") or "9999"
        return d

    candidates.sort(key=date_key)
    return candidates[0]


# ---------------------------------------------------------------------------
# Cover Art Archive
# ---------------------------------------------------------------------------


def fetch_cover_art(release_group_id: str) -> tuple[bytes, str] | None:
    """Fetch front cover art from Cover Art Archive. Returns (image_bytes, mime_type) or None."""
    import urllib.request

    # Try 500px thumbnail first, then full image
    urls = [
        f"https://coverartarchive.org/release-group/{release_group_id}/front-500",
        f"https://coverartarchive.org/release-group/{release_group_id}/front",
    ]

    for url in urls:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "MusicMachine-MetaTagger/1.0"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                if resp.status == 200:
                    content_type = resp.headers.get("Content-Type", "image/jpeg")
                    # Normalize to just the MIME type (strip params like charset)
                    mime = content_type.split(";")[0].strip()
                    return resp.read(), mime
        except Exception:
            continue

    return None


# ---------------------------------------------------------------------------
# Tag writing
# ---------------------------------------------------------------------------


def _compute_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def write_metadata(
    file_path: str,
    metadata: dict,
    cover_art_bytes: bytes | None = None,
    recording_id: str | None = None,
    cover_art_mime: str = "image/jpeg",
) -> tuple[str, str]:
    """
    Write metadata tags to an audio file using mutagen.
    Returns (sha256_before, sha256_after).
    """
    sha256_before = _compute_sha256(file_path)

    audio = MutagenFile(file_path)
    if audio is None:
        raise ValueError(f"Cannot open {file_path} with mutagen")

    if isinstance(audio, FLAC):
        _write_flac(audio, metadata, cover_art_bytes, recording_id, cover_art_mime)
    elif isinstance(audio, MP3):
        _write_mp3(audio, file_path, metadata, cover_art_bytes, recording_id, cover_art_mime)
    elif isinstance(audio, MP4):
        _write_mp4(audio, metadata, cover_art_bytes, recording_id, cover_art_mime)
    else:
        # Vorbis/Opus — use same keys as FLAC
        _write_vorbis(audio, metadata, recording_id)

    audio.save()
    sha256_after = _compute_sha256(file_path)
    return sha256_before, sha256_after


def _write_flac(audio: FLAC, meta: dict, art: bytes | None, rec_id: str | None,
                art_mime: str = "image/jpeg"):
    if meta.get("artist"):
        audio["artist"] = [meta["artist"]]
    if meta.get("title"):
        audio["title"] = [meta["title"]]
    if meta.get("album"):
        audio["album"] = [meta["album"]]
    if meta.get("date"):
        audio["date"] = [meta["date"]]
    if meta.get("track_number"):
        tn = str(meta["track_number"])
        if meta.get("total_tracks"):
            tn += f"/{meta['total_tracks']}"
        audio["tracknumber"] = [tn]
    if rec_id:
        audio["musicbrainz_recordingid"] = [rec_id]

    if art:
        pic = Picture()
        pic.type = 3  # Front cover
        pic.mime = art_mime
        pic.data = art
        audio.clear_pictures()
        audio.add_picture(pic)


def _write_mp3(audio: MP3, file_path: str, meta: dict, art: bytes | None, rec_id: str | None,
               art_mime: str = "image/jpeg"):
    if audio.tags is None:
        audio.add_tags()
    tags = audio.tags

    if meta.get("artist"):
        tags["TPE1"] = TPE1(encoding=3, text=[meta["artist"]])
    if meta.get("title"):
        tags["TIT2"] = TIT2(encoding=3, text=[meta["title"]])
    if meta.get("album"):
        tags["TALB"] = TALB(encoding=3, text=[meta["album"]])
    if meta.get("date"):
        tags["TDRC"] = TDRC(encoding=3, text=[meta["date"]])
    if meta.get("track_number"):
        tn = str(meta["track_number"])
        if meta.get("total_tracks"):
            tn += f"/{meta['total_tracks']}"
        tags["TRCK"] = TRCK(encoding=3, text=[tn])
    if rec_id:
        tags["TXXX:MusicBrainz Recording Id"] = TXXX(
            encoding=3, desc="MusicBrainz Recording Id", text=[rec_id]
        )

    if art:
        tags["APIC:Front Cover"] = APIC(
            encoding=3, mime=art_mime, type=3, desc="Front Cover", data=art
        )


def _write_mp4(audio: MP4, meta: dict, art: bytes | None, rec_id: str | None,
               art_mime: str = "image/jpeg"):
    if audio.tags is None:
        audio.add_tags()

    if meta.get("artist"):
        audio.tags["\xa9ART"] = [meta["artist"]]
    if meta.get("title"):
        audio.tags["\xa9nam"] = [meta["title"]]
    if meta.get("album"):
        audio.tags["\xa9alb"] = [meta["album"]]
    if meta.get("date"):
        audio.tags["\xa9day"] = [meta["date"]]
    if meta.get("track_number"):
        total = meta.get("total_tracks") or 0
        audio.tags["trkn"] = [(meta["track_number"], total)]
    if rec_id:
        audio.tags["----:com.apple.iTunes:MusicBrainz Recording Id"] = [
            rec_id.encode("utf-8")
        ]

    if art:
        img_fmt = MP4Cover.FORMAT_PNG if "png" in art_mime else MP4Cover.FORMAT_JPEG
        audio.tags["covr"] = [MP4Cover(art, imageformat=img_fmt)]


def _write_vorbis(audio, meta: dict, rec_id: str | None):
    """Write tags for OggVorbis/OggOpus using Vorbis comment keys."""
    if meta.get("artist"):
        audio["artist"] = [meta["artist"]]
    if meta.get("title"):
        audio["title"] = [meta["title"]]
    if meta.get("album"):
        audio["album"] = [meta["album"]]
    if meta.get("date"):
        audio["date"] = [meta["date"]]
    if meta.get("track_number"):
        tn = str(meta["track_number"])
        if meta.get("total_tracks"):
            tn += f"/{meta['total_tracks']}"
        audio["tracknumber"] = [tn]
    if rec_id:
        audio["musicbrainz_recordingid"] = [rec_id]


# ---------------------------------------------------------------------------
# Check if a file already has a MusicBrainz Recording ID tag
# ---------------------------------------------------------------------------


def has_mb_recording_id(file_path: str) -> bool:
    """Check if a file already has a MusicBrainz Recording ID tag."""
    try:
        audio = MutagenFile(file_path)
        if audio is None:
            return False

        if isinstance(audio, FLAC):
            return bool(audio.get("musicbrainz_recordingid"))
        elif isinstance(audio, MP3):
            if audio.tags:
                return "TXXX:MusicBrainz Recording Id" in audio.tags
        elif isinstance(audio, MP4):
            if audio.tags:
                return "----:com.apple.iTunes:MusicBrainz Recording Id" in audio.tags
        else:
            # Vorbis/Opus
            return bool(audio.get("musicbrainz_recordingid"))
    except Exception:
        pass
    return False


# ---------------------------------------------------------------------------
# Generate fingerprint (reuse fpcalc subprocess pattern from scanner.py)
# ---------------------------------------------------------------------------


def generate_fingerprint_with_duration(path: str) -> tuple[str | None, float | None]:
    """Generate chromaprint fingerprint and duration via fpcalc. Returns (fingerprint, duration)."""
    try:
        result = subprocess.run(
            ["fpcalc", "-json", str(path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return None, None
        data = json.loads(result.stdout)
        return data.get("fingerprint"), data.get("duration")
    except Exception as e:
        logger.debug(f"Fingerprint failed for {path}: {e}")
        return None, None


# ---------------------------------------------------------------------------
# Directory-level tagging pipeline
# ---------------------------------------------------------------------------


def tag_file(file_path: str, force: bool = False, dry_run: bool = False,
             locked_release: dict | None = None) -> dict:
    """
    Tag a single file through the full pipeline.
    Returns a result dict with status and metadata.
    """
    result = {
        "file_path": file_path,
        "status": "pending",
        "acoustid_score": None,
        "mb_recording_id": None,
        "matched_artist": None,
        "matched_title": None,
        "matched_album": None,
        "cover_art_url": None,
        "error_msg": None,
    }

    # Skip if already tagged
    if not force and has_mb_recording_id(file_path):
        result["status"] = "skipped"
        return result

    # Step 1: Fingerprint
    fingerprint, duration = generate_fingerprint_with_duration(file_path)
    if not fingerprint or not duration:
        result["status"] = "failed"
        result["error_msg"] = "Could not generate fingerprint"
        return result

    # Step 2: AcoustID lookup
    matches = lookup_acoustid(fingerprint, duration)
    if not matches:
        result["status"] = "failed"
        result["error_msg"] = "No AcoustID match"
        return result

    result["acoustid_score"] = matches[0]["score"]
    recording_id = matches[0]["recording_id"]
    result["mb_recording_id"] = recording_id

    # Step 3: MusicBrainz metadata
    metadata = lookup_musicbrainz(recording_id)
    if not metadata:
        result["status"] = "failed"
        result["error_msg"] = "MusicBrainz lookup failed"
        return result

    result["matched_artist"] = metadata.get("artist", "")
    result["matched_title"] = metadata.get("title", "")
    result["matched_album"] = metadata.get("album", "")
    result["mb_release_id"] = metadata.get("release_id", "")
    result["release_group_id"] = metadata.get("release_group_id")

    # If directory has a locked release, use it for album consistency
    if locked_release:
        metadata["album"] = locked_release.get("album", metadata.get("album", ""))
        metadata["release_group_id"] = locked_release.get("release_group_id", metadata.get("release_group_id"))
        metadata["release_id"] = locked_release.get("release_id", metadata.get("release_id"))

    # Step 4: Cover art
    cover_art = None
    cover_art_mime = "image/jpeg"
    release_group_id = metadata.get("release_group_id")
    if release_group_id:
        result["cover_art_url"] = f"https://coverartarchive.org/release-group/{release_group_id}/front-500"
        art_result = fetch_cover_art(release_group_id)
        if art_result:
            cover_art, cover_art_mime = art_result

    # Step 5: Write tags
    if dry_run:
        result["status"] = "matched"
        return result

    try:
        sha_before, sha_after = write_metadata(
            file_path, metadata, cover_art, recording_id, cover_art_mime
        )
        result["status"] = "tagged"
        result["sha256_before"] = sha_before
        result["sha256_after"] = sha_after
    except Exception as e:
        result["status"] = "failed"
        result["error_msg"] = f"Write failed: {e}"

    return result


def tag_directory(
    path: str,
    force: bool = False,
    dry_run: bool = False,
) -> Iterator[dict]:
    """
    Walk directory, tag each audio file.
    Yields progress dicts: {type: 'count'|'progress'|'result', ...}

    Directory-level intelligence: if multiple files in same dir match the
    same release, lock to that release for remaining tracks.
    """
    root = Path(path)
    if not root.exists():
        yield {"type": "error", "error": f"Path does not exist: {path}"}
        return

    # Count files first
    audio_files = []
    for dirpath, _, filenames in os.walk(str(root)):
        for fn in sorted(filenames):
            fp = Path(dirpath) / fn
            if fp.suffix.lower() in AUDIO_EXTENSIONS:
                audio_files.append(str(fp))

    yield {"type": "count", "total": len(audio_files)}

    # Group files by directory for album-level locking
    dir_files: dict[str, list[str]] = {}
    for fp in audio_files:
        d = str(Path(fp).parent)
        dir_files.setdefault(d, []).append(fp)

    processed = 0
    for directory, files in dir_files.items():
        locked_release = None
        release_votes: dict[str, int] = {}  # release_id → count

        for file_path in files:
            yield {
                "type": "progress",
                "processed": processed,
                "total": len(audio_files),
                "current_file": file_path,
            }

            result = tag_file(file_path, force=force, dry_run=dry_run,
                              locked_release=locked_release)
            processed += 1

            # Track release votes for directory-level locking
            release_id = result.get("mb_release_id")
            if release_id and result["status"] in ("tagged", "matched"):
                release_votes[release_id] = release_votes.get(release_id, 0) + 1

                # Lock after 2+ files match the same release
                if not locked_release and release_votes[release_id] >= 2:
                    locked_release = {
                        "album": result.get("matched_album", ""),
                        "release_group_id": result.get("release_group_id"),
                        "release_id": release_id,
                    }

            yield {"type": "result", "result": result}

            # Rate limit: 1 req/sec between files (MusicBrainz courtesy)
            # Skip sleep for files that didn't hit external APIs
            if result["status"] not in ("skipped",):
                time.sleep(1.0)
