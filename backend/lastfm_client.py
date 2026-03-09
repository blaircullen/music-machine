"""
Last.fm REST API client for artist similarity lookups.

Uses the free Last.fm API (no OAuth required) to find similar artists
and fetch listener counts for popularity filtering.
"""
import logging
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

LASTFM_BASE = "http://ws.audioscrobbler.com/2.0/"
_REQUEST_DELAY = 0.25  # seconds between calls — Last.fm allows ~5 req/s free tier


def _get(method: str, api_key: str, params: dict, retries: int = 2) -> Optional[dict]:
    """Make a single Last.fm API GET call. Returns parsed JSON or None on error."""
    p = {"method": method, "api_key": api_key, "format": "json"}
    p.update(params)
    for attempt in range(retries + 1):
        try:
            resp = requests.get(LASTFM_BASE, params=p, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            if "error" in data:
                logger.warning(f"Last.fm API error {data['error']}: {data.get('message')}")
                return None
            return data
        except Exception as e:
            if attempt < retries:
                time.sleep(1.0 * (attempt + 1))
                continue
            logger.warning(f"Last.fm request failed ({method}): {e}")
            return None
    return None


def get_similar_artists(
    artist: str,
    api_key: str,
    limit: int = 50,
) -> list[dict]:
    """
    Get artists similar to `artist` via Last.fm artist.getSimilar.

    Returns list of dicts: {name, match, listeners}
    - name: artist name string
    - match: similarity score 0.0–1.0
    - listeners: Last.fm listener count (0 if unavailable)

    Caller is responsible for filtering by minimum listener count.
    """
    data = _get("artist.getsimilar", api_key, {"artist": artist, "limit": limit})
    if not data:
        return []

    raw_artists = data.get("similarartists", {}).get("artist", [])
    if not raw_artists:
        return []

    results = []
    for item in raw_artists:
        name = item.get("name", "").strip()
        if not name:
            continue
        try:
            match = float(item.get("match", 0))
        except (ValueError, TypeError):
            match = 0.0

        time.sleep(_REQUEST_DELAY)
        listeners = _fetch_listeners(name, api_key)

        results.append({"name": name, "match": match, "listeners": listeners})

    return results


def _fetch_listeners(artist: str, api_key: str) -> int:
    """Return Last.fm listener count for artist, or 0 on error."""
    data = _get("artist.getinfo", api_key, {"artist": artist})
    if not data:
        return 0
    try:
        return int(data["artist"]["stats"]["listeners"])
    except (KeyError, TypeError, ValueError):
        return 0
