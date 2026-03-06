import os
import subprocess
from pathlib import Path
from typing import Generator, Iterator

from mutagen import File as MutagenFile
from mutagen.flac import FLAC
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4
from mutagen.oggvorbis import OggVorbis
from mutagen.oggopus import OggOpus
from mutagen.asf import ASF
from mutagen.wave import WAVE

import logging

logger = logging.getLogger(__name__)

AUDIO_EXTENSIONS = {".mp3", ".flac", ".m4a", ".ogg", ".wav", ".alac", ".wma", ".opus", ".aac"}
LOSSLESS_FORMATS = {"flac", "wav", "alac"}


def _first(val) -> str:
    if val is None:
        return ""
    if isinstance(val, list):
        return str(val[0]) if val else ""
    return str(val)


def _parse_int(val) -> int:
    if val is None:
        return 0
    try:
        return int(str(val).split("/")[0])
    except (ValueError, IndexError, AttributeError):
        return 0


def read_track_metadata(path: str) -> dict:
    """Read audio metadata from a file using mutagen. Returns dict matching tracks table schema."""
    file_path = Path(path)
    try:
        stat = file_path.stat()
    except OSError:
        return _empty_meta(str(file_path))

    ext = file_path.suffix.lower()
    fmt = ext.lstrip(".")

    meta = {
        "file_path": str(file_path),
        "file_size": stat.st_size,
        "format": fmt,
        "bitrate": None,
        "bit_depth": None,
        "sample_rate": None,
        "duration": None,
        "artist": "",
        "album_artist": "",
        "album": "",
        "title": file_path.stem,
        "track_number": None,
        "disc_number": None,
        "fingerprint": None,
        "sha256": None,
    }

    try:
        audio = MutagenFile(str(file_path))
    except Exception:
        return meta

    if audio is None:
        return meta

    # Common audio info fields
    if hasattr(audio.info, "length"):
        meta["duration"] = float(audio.info.length)
    if hasattr(audio.info, "sample_rate"):
        meta["sample_rate"] = int(audio.info.sample_rate)
    if hasattr(audio.info, "bitrate"):
        meta["bitrate"] = int(audio.info.bitrate / 1000)

    if isinstance(audio, FLAC):
        meta["format"] = "flac"
        meta["bit_depth"] = audio.info.bits_per_sample
        if audio.tags:
            meta["artist"] = _first(audio.get("artist"))
            meta["album_artist"] = _first(audio.get("albumartist") or audio.get("artist"))
            meta["album"] = _first(audio.get("album"))
            meta["title"] = _first(audio.get("title")) or file_path.stem
            meta["track_number"] = _parse_int(_first(audio.get("tracknumber")))
            meta["disc_number"] = _parse_int(_first(audio.get("discnumber")))

    elif isinstance(audio, MP3):
        meta["format"] = "mp3"
        # mp3 bitrate from mutagen is already in bps for MP3Info
        if hasattr(audio.info, "bitrate"):
            meta["bitrate"] = int(audio.info.bitrate / 1000)
        if audio.tags:
            tpe1 = audio.tags.get("TPE1")
            tpe2 = audio.tags.get("TPE2")
            meta["artist"] = str(tpe1) if tpe1 else ""
            meta["album_artist"] = str(tpe2) if tpe2 else meta["artist"]
            talb = audio.tags.get("TALB")
            meta["album"] = str(talb) if talb else ""
            tit2 = audio.tags.get("TIT2")
            meta["title"] = str(tit2) if tit2 else file_path.stem
            trck = audio.tags.get("TRCK")
            meta["track_number"] = _parse_int(str(trck)) if trck else None
            tpos = audio.tags.get("TPOS")
            meta["disc_number"] = _parse_int(str(tpos)) if tpos else None

    elif isinstance(audio, MP4):
        # Handles m4a, alac, aac
        if ext in (".alac",) or (hasattr(audio.info, "codec") and "alac" in str(getattr(audio.info, "codec", "")).lower()):
            meta["format"] = "alac"
        elif ext == ".aac":
            meta["format"] = "aac"
        else:
            meta["format"] = "m4a"
        if hasattr(audio.info, "bits_per_sample") and audio.info.bits_per_sample:
            meta["bit_depth"] = audio.info.bits_per_sample
        if audio.tags:
            art = audio.tags.get("\xa9ART")
            meta["artist"] = _first(art) if art else ""
            aart = audio.tags.get("aART")
            meta["album_artist"] = _first(aart) if aart else meta["artist"]
            alb = audio.tags.get("\xa9alb")
            meta["album"] = _first(alb) if alb else ""
            nam = audio.tags.get("\xa9nam")
            meta["title"] = _first(nam) if nam else file_path.stem
            trkn = audio.tags.get("trkn")
            if trkn:
                meta["track_number"] = trkn[0][0]
            disk = audio.tags.get("disk")
            if disk:
                meta["disc_number"] = disk[0][0]

    elif isinstance(audio, OggVorbis):
        meta["format"] = "ogg"
        if audio.tags:
            meta["artist"] = _first(audio.get("artist"))
            meta["album_artist"] = _first(audio.get("albumartist") or audio.get("artist"))
            meta["album"] = _first(audio.get("album"))
            meta["title"] = _first(audio.get("title")) or file_path.stem
            meta["track_number"] = _parse_int(_first(audio.get("tracknumber")))
            meta["disc_number"] = _parse_int(_first(audio.get("discnumber")))

    elif isinstance(audio, OggOpus):
        meta["format"] = "opus"
        if audio.tags:
            meta["artist"] = _first(audio.get("artist"))
            meta["album_artist"] = _first(audio.get("albumartist") or audio.get("artist"))
            meta["album"] = _first(audio.get("album"))
            meta["title"] = _first(audio.get("title")) or file_path.stem
            meta["track_number"] = _parse_int(_first(audio.get("tracknumber")))
            meta["disc_number"] = _parse_int(_first(audio.get("discnumber")))

    elif isinstance(audio, ASF):
        # WMA files
        meta["format"] = "wma"
        if audio.tags:
            artist_tag = audio.tags.get("Author") or audio.tags.get("WM/AlbumArtist")
            meta["artist"] = _first(artist_tag)
            album_artist_tag = audio.tags.get("WM/AlbumArtist")
            meta["album_artist"] = _first(album_artist_tag) or meta["artist"]
            album_tag = audio.tags.get("WM/AlbumTitle")
            meta["album"] = _first(album_tag)
            title_tag = audio.tags.get("Title")
            meta["title"] = _first(title_tag) or file_path.stem
            track_tag = audio.tags.get("WM/TrackNumber")
            meta["track_number"] = _parse_int(_first(track_tag))

    elif isinstance(audio, WAVE):
        meta["format"] = "wav"
        if hasattr(audio.info, "bits_per_sample"):
            meta["bit_depth"] = audio.info.bits_per_sample
        # WAV tags are usually ID3
        if audio.tags:
            try:
                tpe1 = audio.tags.get("TPE1")
                meta["artist"] = str(tpe1) if tpe1 else ""
                tit2 = audio.tags.get("TIT2")
                meta["title"] = str(tit2) if tit2 else file_path.stem
                talb = audio.tags.get("TALB")
                meta["album"] = str(talb) if talb else ""
            except Exception:
                pass

    else:
        # Generic fallback — try common tag keys
        try:
            tags = audio.tags
            if tags:
                for key in ("artist", "ARTIST"):
                    if key in tags:
                        meta["artist"] = _first(tags[key])
                        break
                for key in ("title", "TITLE"):
                    if key in tags:
                        meta["title"] = _first(tags[key]) or file_path.stem
                        break
                for key in ("album", "ALBUM"):
                    if key in tags:
                        meta["album"] = _first(tags[key])
                        break
        except Exception:
            pass

    return meta


def _empty_meta(file_path: str) -> dict:
    return {
        "file_path": file_path,
        "file_size": 0,
        "format": Path(file_path).suffix.lstrip("."),
        "bitrate": None,
        "bit_depth": None,
        "sample_rate": None,
        "duration": None,
        "artist": "",
        "album_artist": "",
        "album": "",
        "title": Path(file_path).stem,
        "track_number": None,
        "disc_number": None,
        "fingerprint": None,
        "sha256": None,
    }


def quality_score(track: dict) -> int:
    """Deterministic quality ranking. Higher is better."""
    fmt = (track.get("format") or "").lower()
    score = 0

    # Format base score
    if fmt in ("flac", "wav", "alac"):
        score += 10000
    elif fmt in ("m4a", "aac"):
        score += 150
    elif fmt == "ogg":
        score += 120
    elif fmt == "mp3":
        score += 100
    else:
        score += 50

    # Bit depth bonus (only lossless have this)
    bit_depth = track.get("bit_depth") or 0
    if bit_depth:
        score += bit_depth * 200

    # Sample rate bonus (e.g. 96000 Hz -> +96)
    sample_rate = track.get("sample_rate") or 0
    if sample_rate:
        score += sample_rate // 1000

    # Bitrate (kbps)
    bitrate = track.get("bitrate") or 0
    if bitrate:
        score += bitrate

    return score


def generate_fingerprint(path: str) -> str | None:
    """
    Generate Chromaprint fingerprint by running fpcalc as a subprocess.
    Using subprocess (not pyacoustid ctypes) ensures a crash in the C library
    cannot kill the main Python process.
    """
    try:
        result = subprocess.run(
            ["fpcalc", "-json", str(path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            logger.debug(f"fpcalc exited {result.returncode} for {path}: {result.stderr.strip()}")
            return None
        import json as _json
        data = _json.loads(result.stdout)
        return data.get("fingerprint") or None
    except Exception as e:
        logger.debug(f"Fingerprint failed for {path}: {e}")
        return None


def scan_directory(music_path: str) -> Iterator[dict]:
    """Walk directory tree, yield metadata dicts for audio files."""
    root = Path(music_path)
    for dirpath, dirnames, filenames in os.walk(str(root)):
        # Sort directories for deterministic ordering
        dirnames.sort()
        for filename in sorted(filenames):
            filepath = Path(dirpath) / filename
            if filepath.suffix.lower() in AUDIO_EXTENSIONS:
                try:
                    yield read_track_metadata(str(filepath))
                except Exception as e:
                    logger.warning(f"Error reading {filepath}: {e}")
                    continue
