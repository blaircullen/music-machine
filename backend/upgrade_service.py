"""
Upgrade service — MusicGrabber integration.

Searches for FLAC upgrades via MusicGrabber's Monochrome/Tidal API
and downloads them directly to the shared music library.
"""

import logging
import os
import re
import time
import unicodedata
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

MUSICGRABBER_URL = os.environ.get("MUSICGRABBER_URL", "http://localhost:38274")

HTTP_TIMEOUT = 30
SEARCH_TIMEOUT = 20
DOWNLOAD_POLL_TIMEOUT = 600  # 10 minutes max wait for a download
DOWNLOAD_POLL_INTERVAL = 3


def _normalize_text(text: str) -> str:
    """Normalize text for comparison (mirrors dedup.py normalize_text)."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = text.lower().strip()
    text = re.sub(r"[^\w\s\-]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _classify_quality(quality_str: str | None, audio_quality_str: str | None = None) -> str:
    """Classify a Monochrome result as hi_res or lossless."""
    q = (quality_str or "").upper()
    aq = (audio_quality_str or "").upper()

    if q == "HI_RES_LOSSLESS" or "HI_RES" in q:
        return "hi_res"
    if "24" in aq and ("BIT" in aq or "KHZ" in aq):
        return "hi_res"
    if any(rate in aq for rate in ["96KHZ", "192KHZ", "88KHZ", "176KHZ"]):
        return "hi_res"
    if q == "LOSSLESS" or "FLAC" in aq:
        return "lossless"
    return "lossless"


def _score_search_result(result: dict, target_artist: str, target_title: str, target_album: str = "") -> int:
    """Score a MusicGrabber search result for match quality. Higher is better."""
    score = result.get("quality_score", 0)

    # Artist match bonus
    result_artist = _normalize_text(result.get("channel") or "")
    norm_artist = _normalize_text(target_artist)
    if norm_artist and result_artist:
        artist_words = set(norm_artist.split())
        result_words = set(result_artist.split())
        if artist_words and result_words:
            overlap = len(artist_words & result_words) / len(artist_words)
            score += int(overlap * 200)
            # Exact match bonus
            if norm_artist == result_artist:
                score += 100

    # Title match bonus
    result_title = _normalize_text(result.get("title") or "")
    norm_title = _normalize_text(target_title)
    if norm_title and result_title:
        title_words = set(norm_title.split())
        result_words = set(result_title.split())
        if title_words:
            overlap = len(title_words & result_words) / len(title_words)
            score += int(overlap * 150)
            if norm_title == result_title:
                score += 100

    # Album match bonus
    result_album = _normalize_text(result.get("album") or "")
    norm_album = _normalize_text(target_album)
    if norm_album and result_album:
        album_words = set(norm_album.split())
        result_words = set(result_album.split())
        if album_words:
            overlap = len(album_words & result_words) / len(album_words)
            score += int(overlap * 100)

    # Quality bonus
    quality = (result.get("quality") or "").upper()
    if quality == "HI_RES_LOSSLESS":
        score += 500
    elif quality == "LOSSLESS":
        score += 300
    elif quality == "HIGH":
        score += 100

    return score


async def search_for_flac(
    artist: str,
    album: str,
    title: str,
    timeout_s: int = 20,
    hi_res_only: bool = False,
) -> dict | None:
    """
    Search MusicGrabber (Monochrome/Tidal) for a FLAC version of the given track.
    Returns the best match dict or None.

    Return shape: {mg_track_id, title, artist, album, quality, match_quality, source_url}
    """
    query = f"{artist} {title}" if not album else f"{artist} {album} {title}"

    import asyncio as _asyncio

    max_retries = 8
    async with httpx.AsyncClient(timeout=SEARCH_TIMEOUT) as client:
        data = None
        for attempt in range(max_retries):
            try:
                resp = await client.post(
                    f"{MUSICGRABBER_URL}/api/search",
                    json={
                        "query": query,
                        "source": "monochrome",
                        "limit": 10,
                    },
                )
                if resp.status_code == 429:
                    wait = 3 ** attempt + 1  # 2, 4, 10, 28, 82... (capped at 30s)
                    wait = min(wait, 30)
                    logger.info(f"429 rate limited for '{query}', retry {attempt+1}/{max_retries} in {wait}s")
                    await _asyncio.sleep(wait)
                    continue
                resp.raise_for_status()
                data = resp.json()
                break
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 429:
                    wait = 3 ** attempt + 1
                    wait = min(wait, 30)
                    logger.info(f"429 rate limited for '{query}', retry {attempt+1}/{max_retries} in {wait}s")
                    await _asyncio.sleep(wait)
                    continue
                logger.warning(f"MusicGrabber search failed for '{query}': {e}")
                return None
            except Exception as e:
                logger.warning(f"MusicGrabber search failed for '{query}': {e}")
                return None
        if data is None:
            logger.warning(f"MusicGrabber search exhausted retries for '{query}'")
            return None

    results = data.get("results") or []
    if not results:
        return None

    # Score and rank results
    scored = []
    for r in results:
        match_score = _score_search_result(r, artist, title, album)
        scored.append((r, match_score))

    scored.sort(key=lambda x: x[1], reverse=True)
    best_result, best_score = scored[0]

    # Minimum score threshold — must match artist reasonably
    if best_score < 200:
        logger.debug(f"Best result for '{query}' scored too low ({best_score}), skipping")
        return None

    quality = _classify_quality(
        best_result.get("quality"),
        best_result.get("audio_quality"),
    )

    if hi_res_only and quality != "hi_res":
        return None

    return {
        "mg_track_id": best_result.get("video_id"),
        "title": best_result.get("title"),
        "artist": best_result.get("channel"),
        "album": best_result.get("album"),
        "quality": best_result.get("quality"),
        "match_quality": quality,
        "source_url": best_result.get("source_url"),
        "match_score": best_score,
    }


async def search_album(
    artist: str,
    album: str,
    tracks: list[dict],
    timeout_s: int = 15,
    inter_search_delay: float = 1.5,
) -> dict[int, dict]:
    """
    Search MusicGrabber for each track in an album group.
    Returns a dict keyed by track_id → best matching result dict.

    tracks: list of {id, title, track_number, format, bit_depth}
    inter_search_delay: seconds to wait between track searches (rate limit protection)
    """
    import asyncio as _asyncio

    results: dict[int, dict] = {}

    for i, track in enumerate(tracks):
        track_id = track["id"]
        title = track.get("title") or ""
        is_flac_source = (track.get("format") or "").lower() == "flac"

        # Throttle between searches to avoid 429s
        if i > 0 and inter_search_delay > 0:
            await _asyncio.sleep(inter_search_delay)

        match = await search_for_flac(
            artist=artist,
            album=album,
            title=title,
            timeout_s=timeout_s,
            hi_res_only=is_flac_source,
        )

        if match:
            results[track_id] = match

    logger.info(
        f"Album search '{artist} - {album}': "
        f"{len(results)}/{len(tracks)} tracks matched"
    )
    return results


def download_track(mg_track_id: str, artist: str = "", title: str = "") -> str:
    """
    Initiate a download via MusicGrabber.
    POST /api/download with the monochrome track ID.
    Returns the MusicGrabber job_id.
    """
    resp = httpx.post(
        f"{MUSICGRABBER_URL}/api/download",
        json={
            "video_id": mg_track_id,
            "source": "monochrome",
            "source_url": f"https://monochrome.tf/track/{mg_track_id}",
            "artist": artist,
            "title": title,
            "convert_to_flac": True,
            "download_type": "single",
        },
        timeout=HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    job_id = data.get("job_id")
    if not job_id:
        raise RuntimeError(f"MusicGrabber returned no job_id for track {mg_track_id}")
    return job_id


def get_download_status(mg_job_id: str) -> dict | None:
    """
    Get download status for a MusicGrabber job.
    Returns {status, artist, title, audio_quality, error} or None.
    """
    try:
        resp = httpx.get(
            f"{MUSICGRABBER_URL}/api/jobs/{mg_job_id}",
            timeout=HTTP_TIMEOUT,
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.debug(f"get_download_status error for job {mg_job_id}: {e}")
        return None


def wait_for_download(mg_job_id: str, timeout: int = DOWNLOAD_POLL_TIMEOUT) -> dict:
    """
    Poll MusicGrabber until the download completes or fails.
    Returns the final job status dict.
    Raises RuntimeError on timeout or failure.
    """
    elapsed = 0
    while elapsed < timeout:
        time.sleep(DOWNLOAD_POLL_INTERVAL)
        elapsed += DOWNLOAD_POLL_INTERVAL

        status = get_download_status(mg_job_id)
        if status is None:
            continue

        job_status = status.get("status", "")
        if job_status in ("completed", "completed_with_errors"):
            return status
        elif job_status == "failed":
            error = status.get("error", "Unknown error")
            raise RuntimeError(f"MusicGrabber download failed: {error}")

    raise RuntimeError(f"MusicGrabber download timed out after {timeout}s (job {mg_job_id})")


async def check_connected() -> bool:
    """Return True if MusicGrabber API is reachable."""
    async with httpx.AsyncClient(timeout=5) as client:
        try:
            resp = await client.get(f"{MUSICGRABBER_URL}/api/version")
            return resp.status_code == 200
        except Exception:
            return False
