"""
Cover art fetching — tries Cover Art Archive first (free, high quality),
falls back to Spotify album art URL from AudD response.
"""

import logging
import urllib.request

logger = logging.getLogger(__name__)

MAX_ART_SIZE = 500 * 1024  # 500KB cap to avoid file bloat


def fetch_cover_art(
    release_group_id: str | None = None,
    spotify_url: str | None = None,
) -> tuple[bytes, str] | None:
    """
    Fetch cover art. Try Cover Art Archive first, then Spotify URL.
    Returns (jpeg_bytes, mime_type) or None.

    - CAA: 500px front cover (JPEG)
    - Spotify: 640x640 album art
    """
    # Try Cover Art Archive first
    if release_group_id:
        result = _fetch_caa(release_group_id)
        if result:
            return result

    # Fall back to Spotify URL
    if spotify_url:
        result = _fetch_url(spotify_url)
        if result:
            return result

    return None


def _fetch_caa(release_group_id: str) -> tuple[bytes, str] | None:
    """Fetch from Cover Art Archive."""
    urls = [
        f"https://coverartarchive.org/release-group/{release_group_id}/front-500",
        f"https://coverartarchive.org/release-group/{release_group_id}/front",
    ]

    for url in urls:
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "MusicMachine/2.0"},
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                if resp.status == 200:
                    data = resp.read()
                    if len(data) > MAX_ART_SIZE:
                        logger.debug(f"CAA art too large ({len(data)} bytes), skipping")
                        continue
                    content_type = resp.headers.get("Content-Type", "image/jpeg")
                    mime = content_type.split(";")[0].strip()
                    return data, mime
        except Exception:
            continue

    return None


def _fetch_url(url: str) -> tuple[bytes, str] | None:
    """Fetch cover art from a direct URL (e.g., Spotify)."""
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "MusicMachine/2.0"},
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            if resp.status == 200:
                data = resp.read()
                if len(data) > MAX_ART_SIZE:
                    logger.debug(f"Art too large ({len(data)} bytes), skipping")
                    return None
                content_type = resp.headers.get("Content-Type", "image/jpeg")
                mime = content_type.split(";")[0].strip()
                return data, mime
    except Exception as e:
        logger.debug(f"Cover art fetch failed from {url}: {e}")

    return None
