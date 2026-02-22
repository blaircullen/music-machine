import httpx
import asyncio
import base64
import json
import logging
import re
from pathlib import Path
from dedup import normalize_text

logger = logging.getLogger(__name__)

# Squid.wtf backend API hosts
SEARCH_TRACKS_HOST = "https://hifi-two.spotisaver.net"
SEARCH_ALBUMS_HOST = "https://triton.squid.wtf"
TRACK_DOWNLOAD_HOST = "https://vogel.qqdl.site"
TRACK_INFO_HOST = "https://wolf.qqdl.site"

# Quality tiers (best to worst)
QUALITY_HI_RES = "HI_RES_LOSSLESS"  # 24-bit
QUALITY_LOSSLESS = "LOSSLESS"        # 16/44.1 CD quality
QUALITY_HIGH = "HIGH"                # 320kbps AAC

DEFAULT_HEADERS = {"User-Agent": "plex-dedup/1.0"}


def _collapse_spaced_name(text: str) -> str:
    """Collapse stylized spaced-out names like 'A R I Z O N A' → 'ARIZONA'."""
    text = re.sub(r"\s+", " ", text).strip()
    # If the text is single chars separated by spaces (e.g. "A R I Z O N A"),
    # collapse them into one word
    text = re.sub(r"(?<!\S)((?:\w )+\w)(?!\S)", lambda m: m.group(1).replace(" ", "") if all(len(p) == 1 for p in m.group(1).split(" ")) else m.group(1), text)
    return text


def build_search_query(track: dict) -> str:
    """Build a search query for squid.wtf from track metadata."""
    artist = _collapse_spaced_name(track["artist"])
    album = _collapse_spaced_name(track["album"])
    return f"{artist} {album}"


def classify_match(track: dict, result: dict) -> str:
    """Classify how well a squid.wtf result matches our track."""
    t_artist = normalize_text(track.get("artist", ""))
    t_album = normalize_text(track.get("album", ""))
    t_title = normalize_text(track.get("title", ""))

    r_artist = normalize_text(result.get("artist", ""))
    r_album = normalize_text(result.get("album", ""))
    r_title = normalize_text(result.get("title", ""))

    if t_artist == r_artist and t_album == r_album and t_title == r_title:
        return "exact"
    if t_artist == r_artist and (t_album in r_album or r_album in t_album):
        return "fuzzy"
    return "none"


def _extract_artist_name(artist_data) -> str:
    """Extract artist name from Tidal API artist field (can be dict or list)."""
    if isinstance(artist_data, dict):
        return artist_data.get("name", "")
    if isinstance(artist_data, list) and artist_data:
        return artist_data[0].get("name", "")
    return str(artist_data) if artist_data else ""


def _parse_album_result(album: dict) -> dict:
    """Normalize a Tidal album API result into our format."""
    return {
        "tidal_id": album.get("id"),
        "title": album.get("title", ""),
        "artist": _extract_artist_name(album.get("artists", album.get("artist"))),
        "num_tracks": album.get("numberOfTracks", 0),
        "release_date": album.get("releaseDate", ""),
        "audio_quality": album.get("audioQuality", ""),
        "cover": album.get("cover", ""),
    }


def _parse_track_result(track: dict) -> dict:
    """Normalize a Tidal track API result into our format."""
    album_data = track.get("album", {})
    return {
        "tidal_id": track.get("id"),
        "title": track.get("title", ""),
        "artist": _extract_artist_name(track.get("artists", track.get("artist"))),
        "album": album_data.get("title", "") if isinstance(album_data, dict) else str(album_data),
        "track_number": track.get("trackNumber", 0),
        "volume_number": track.get("volumeNumber", 1),
        "duration": track.get("duration", 0),
        "audio_quality": track.get("audioQuality", ""),
        "isrc": track.get("isrc", ""),
    }


async def search_albums(query: str, rate_limit: float = 3.0) -> list[dict]:
    """Search for albums on squid.wtf via triton backend."""
    await asyncio.sleep(rate_limit)
    async with httpx.AsyncClient(headers=DEFAULT_HEADERS, timeout=30) as client:
        resp = await client.get(f"{SEARCH_ALBUMS_HOST}/search/", params={"al": query})
        resp.raise_for_status()
        data = resp.json()

    albums_data = data.get("data", {}).get("albums", {}).get("items", [])
    return [_parse_album_result(a) for a in albums_data]


async def search_tracks(query: str, rate_limit: float = 3.0) -> list[dict]:
    """Search for tracks on squid.wtf via spotisaver backend."""
    await asyncio.sleep(rate_limit)
    async with httpx.AsyncClient(headers=DEFAULT_HEADERS, timeout=30) as client:
        resp = await client.get(f"{SEARCH_TRACKS_HOST}/search/", params={"s": query})
        resp.raise_for_status()
        data = resp.json()

    tracks_data = data.get("data", {}).get("items", [])
    return [_parse_track_result(t) for t in tracks_data]


async def get_album_tracks(album_id: int, rate_limit: float = 2.0) -> list[dict]:
    """Get all tracks for a specific album."""
    await asyncio.sleep(rate_limit)
    async with httpx.AsyncClient(headers=DEFAULT_HEADERS, timeout=30) as client:
        resp = await client.get(f"{SEARCH_ALBUMS_HOST}/album/", params={"id": album_id})
        resp.raise_for_status()
        data = resp.json()

    # Album endpoint returns tracks in the response
    tracks = data.get("tracks", data.get("items", []))
    if not tracks:
        # Try nested structure
        tracks = data.get("data", {}).get("items", [])
    return [_parse_track_result(t) for t in tracks]


async def get_track_info(track_id: int, rate_limit: float = 1.0) -> dict:
    """Get detailed info for a specific track."""
    await asyncio.sleep(rate_limit)
    async with httpx.AsyncClient(headers=DEFAULT_HEADERS, timeout=30) as client:
        resp = await client.get(f"{TRACK_INFO_HOST}/info/", params={"id": track_id})
        resp.raise_for_status()
        return _parse_track_result(resp.json())


async def get_download_url(track_id: int, quality: str = QUALITY_HI_RES, rate_limit: float = 2.0) -> dict:
    """Get the FLAC download URL for a track.

    Returns dict with: url, bit_depth, sample_rate, audio_quality, mime_type
    Falls back to LOSSLESS if HI_RES_LOSSLESS unavailable.
    """
    await asyncio.sleep(rate_limit)
    async with httpx.AsyncClient(headers=DEFAULT_HEADERS, timeout=30) as client:
        resp = await client.get(
            f"{TRACK_DOWNLOAD_HOST}/track/",
            params={"id": track_id, "quality": quality},
        )

        # Fall back to CD quality if hi-res fails
        if resp.status_code != 200 and quality == QUALITY_HI_RES:
            logger.info(f"Hi-res unavailable for track {track_id}, falling back to lossless")
            await asyncio.sleep(rate_limit)
            resp = await client.get(
                f"{TRACK_DOWNLOAD_HOST}/track/",
                params={"id": track_id, "quality": QUALITY_LOSSLESS},
            )

        resp.raise_for_status()
        data = resp.json()

    # Decode the base64 manifest to extract the download URL
    manifest_b64 = data.get("manifest", "")
    manifest = json.loads(base64.b64decode(manifest_b64))

    urls = manifest.get("urls", [])
    if not urls:
        raise ValueError(f"No download URL found for track {track_id}")

    return {
        "url": urls[0],
        "bit_depth": data.get("bitDepth", 16),
        "sample_rate": data.get("sampleRate", 44100),
        "audio_quality": data.get("audioQuality", "LOSSLESS"),
        "mime_type": manifest.get("mimeType", "audio/flac"),
    }


async def download_flac(url: str, dest: Path, rate_limit: float = 2.0) -> Path:
    """Download a FLAC file from the Tidal CDN to the staging directory."""
    await asyncio.sleep(rate_limit)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    async with httpx.AsyncClient(timeout=300) as client:
        async with client.stream("GET", url) as resp:
            resp.raise_for_status()
            with open(dest, "wb") as f:
                async for chunk in resp.aiter_bytes(8192):
                    f.write(chunk)
    return dest


async def find_album_match(artist: str, album: str, rate_limit: float = 3.0) -> dict | None:
    """Search for an album and return the best match, or None.

    Returns the parsed album dict with tidal_id if found.
    """
    query = f"{_collapse_spaced_name(artist)} {_collapse_spaced_name(album)}"
    results = await search_albums(query, rate_limit)

    n_artist = normalize_text(artist)
    n_album = normalize_text(album)

    # Try exact match first
    for r in results:
        if normalize_text(r["artist"]) == n_artist and normalize_text(r["title"]) == n_album:
            return r

    # Try fuzzy (artist matches, album is substring)
    for r in results:
        r_artist = normalize_text(r["artist"])
        r_title = normalize_text(r["title"])
        if r_artist == n_artist and (n_album in r_title or r_title in n_album):
            return r

    return None


async def find_and_match_track(
    artist: str, album: str, title: str, track_number: int = 0, rate_limit: float = 3.0
) -> dict | None:
    """Find a specific track on Tidal by searching the album, then matching the track.

    Returns dict with tidal track info + match_type, or None.
    """
    album_match = await find_album_match(artist, album, rate_limit)
    if not album_match:
        return None

    tracks = await get_album_tracks(album_match["tidal_id"], rate_limit)

    n_title = normalize_text(title)

    # Try track number match first (most reliable)
    if track_number > 0:
        for t in tracks:
            if t["track_number"] == track_number and normalize_text(t["title"]) == n_title:
                return {**t, "match_type": "exact", "album_tidal_id": album_match["tidal_id"]}

    # Try exact title match
    for t in tracks:
        if normalize_text(t["title"]) == n_title:
            return {**t, "match_type": "exact", "album_tidal_id": album_match["tidal_id"]}

    # Try fuzzy title match (substring)
    for t in tracks:
        t_title = normalize_text(t["title"])
        if n_title in t_title or t_title in n_title:
            return {**t, "match_type": "fuzzy", "album_tidal_id": album_match["tidal_id"]}

    return None
