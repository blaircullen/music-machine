"""
AudD API client — paid fallback for tracks that don't match on AcoustID.
Enterprise endpoint at https://enterprise.audd.io/

Budget tracking via audd_usage table.
"""

import json
import logging
import os
import subprocess
import tempfile
from datetime import date

from database import get_db

logger = logging.getLogger(__name__)

AUDD_API_URL = "https://enterprise.audd.io/"
AUDD_COST_PER_REQUEST_CENTS = 0.2  # $0.002 per request = 0.2 cents


def _get_api_key() -> str | None:
    """Get AudD API key from settings table or environment."""
    key = os.environ.get("AUDD_API_KEY")
    if key:
        return key

    try:
        with get_db() as db:
            row = db.execute(
                "SELECT value FROM settings WHERE key='audd_api_key'"
            ).fetchone()
            if row and row["value"]:
                return row["value"]
    except Exception:
        pass
    return None


def _get_monthly_budget_cents() -> int:
    """Get monthly AudD budget cap in cents from settings. Default $20 = 2000 cents."""
    try:
        with get_db() as db:
            row = db.execute(
                "SELECT value FROM settings WHERE key='audd_monthly_budget'"
            ).fetchone()
            if row and row["value"]:
                return int(float(row["value"]) * 100)
    except Exception:
        pass
    return 2000  # $20 default


def _get_monthly_spend_cents() -> int:
    """Get total AudD spend this month in cents."""
    today = date.today()
    month_prefix = today.strftime("%Y-%m")

    try:
        with get_db() as db:
            row = db.execute(
                "SELECT COALESCE(SUM(cost_cents), 0) as total "
                "FROM audd_usage WHERE date LIKE ?",
                (f"{month_prefix}%",),
            ).fetchone()
            return row["total"] if row else 0
    except Exception:
        return 0


def _record_usage():
    """Record one AudD API request for today."""
    today_str = date.today().isoformat()
    try:
        with get_db() as db:
            db.execute(
                """INSERT INTO audd_usage (date, requests, cost_cents)
                   VALUES (?, 1, ?)
                   ON CONFLICT(date) DO UPDATE SET
                       requests = requests + 1,
                       cost_cents = cost_cents + ?""",
                (today_str, AUDD_COST_PER_REQUEST_CENTS, AUDD_COST_PER_REQUEST_CENTS),
            )
    except Exception as e:
        logger.warning(f"Failed to record AudD usage: {e}")


def check_budget() -> bool:
    """Check if we're within the monthly AudD budget."""
    spent = _get_monthly_spend_cents()
    budget = _get_monthly_budget_cents()
    return spent < budget


def get_usage_stats() -> dict:
    """Get current AudD usage statistics."""
    today = date.today()
    month_prefix = today.strftime("%Y-%m")

    try:
        with get_db() as db:
            month_row = db.execute(
                "SELECT COALESCE(SUM(requests), 0) as requests, "
                "COALESCE(SUM(cost_cents), 0) as cost_cents "
                "FROM audd_usage WHERE date LIKE ?",
                (f"{month_prefix}%",),
            ).fetchone()

            today_row = db.execute(
                "SELECT COALESCE(requests, 0) as requests, "
                "COALESCE(cost_cents, 0) as cost_cents "
                "FROM audd_usage WHERE date = ?",
                (today.isoformat(),),
            ).fetchone()

        budget = _get_monthly_budget_cents()
        return {
            "month_requests": month_row["requests"] if month_row else 0,
            "month_cost_dollars": round((month_row["cost_cents"] if month_row else 0) / 100, 2),
            "today_requests": today_row["requests"] if today_row else 0,
            "budget_dollars": round(budget / 100, 2),
            "budget_remaining_dollars": round(
                max(0, budget - (month_row["cost_cents"] if month_row else 0)) / 100, 2
            ),
            "within_budget": check_budget(),
        }
    except Exception:
        return {
            "month_requests": 0,
            "month_cost_dollars": 0,
            "today_requests": 0,
            "budget_dollars": 20.0,
            "budget_remaining_dollars": 20.0,
            "within_budget": True,
        }


def identify_track(file_path: str) -> dict | None:
    """
    Send audio sample to AudD enterprise API.
    Returns metadata dict or None.

    Extracts a 15-second sample from the middle of the track to maximize
    match accuracy (intros/outros are less distinctive).
    """
    api_key = _get_api_key()
    if not api_key:
        logger.warning("AudD API key not configured")
        return None

    if not check_budget():
        logger.info("AudD monthly budget exceeded, skipping")
        return None

    # Extract a 15-second sample from the middle of the track
    sample_path = _extract_sample(file_path)
    if not sample_path:
        return None

    try:
        import urllib.request
        import urllib.parse

        with open(sample_path, "rb") as f:
            audio_data = f.read()

        # Build multipart form data
        boundary = "----AudDBoundary"
        body = []
        body.append(f"--{boundary}".encode())
        body.append(b'Content-Disposition: form-data; name="api_token"')
        body.append(b"")
        body.append(api_key.encode())
        body.append(f"--{boundary}".encode())
        body.append(b'Content-Disposition: form-data; name="return"')
        body.append(b"")
        body.append(b"spotify,deezer,apple_music")
        body.append(f"--{boundary}".encode())
        body.append(b'Content-Disposition: form-data; name="file"; filename="sample.mp3"')
        body.append(b"Content-Type: audio/mpeg")
        body.append(b"")
        body.append(audio_data)
        body.append(f"--{boundary}--".encode())

        body_bytes = b"\r\n".join(body)

        req = urllib.request.Request(
            AUDD_API_URL,
            data=body_bytes,
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "User-Agent": "MusicMachine/2.0",
            },
        )

        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())

        _record_usage()

        if data.get("status") != "success" or not data.get("result"):
            return None

        result = data["result"]
        return _parse_audd_result(result)

    except Exception as e:
        logger.warning(f"AudD identify failed for {file_path}: {e}")
        _record_usage()  # Still counts against budget
        return None

    finally:
        # Clean up temp sample
        try:
            if sample_path:
                os.unlink(sample_path)
        except OSError:
            pass


def _extract_sample(file_path: str, duration: int = 15) -> str | None:
    """Extract a short audio sample from the middle of the track using ffmpeg."""
    try:
        # Get total duration via ffprobe
        probe = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", file_path],
            capture_output=True, text=True, timeout=10,
        )
        total_duration = float(probe.stdout.strip())
        start = max(0, (total_duration / 2) - (duration / 2))

        # Extract sample as MP3
        sample_fd, sample_path = tempfile.mkstemp(suffix=".mp3")
        os.close(sample_fd)

        subprocess.run(
            ["ffmpeg", "-y", "-ss", str(int(start)), "-i", file_path,
             "-t", str(duration), "-acodec", "libmp3lame", "-ab", "128k",
             "-ac", "1", "-ar", "16000", sample_path],
            capture_output=True, timeout=30,
        )

        if os.path.getsize(sample_path) > 0:
            return sample_path
        else:
            os.unlink(sample_path)
            return None

    except Exception as e:
        logger.debug(f"Sample extraction failed for {file_path}: {e}")
        return None


def _parse_audd_result(result: dict) -> dict:
    """Parse AudD API result into our metadata schema."""
    metadata = {
        "artist": result.get("artist", ""),
        "title": result.get("title", ""),
        "album": result.get("album", ""),
        "year": None,
        "isrc": None,
        "label": None,
        "spotify_id": None,
        "cover_art_url": None,
        "dsp_ids": {},
        "audd_score": None,
        "song_link": result.get("song_link"),
    }

    # Parse release date
    release_date = result.get("release_date", "")
    if release_date:
        try:
            metadata["year"] = int(release_date[:4])
        except (ValueError, IndexError):
            pass

    # Extract Spotify data
    spotify = result.get("spotify")
    if spotify:
        metadata["spotify_id"] = spotify.get("id")
        metadata["dsp_ids"]["spotify"] = spotify.get("id")

        # ISRC from Spotify
        ext_ids = spotify.get("external_ids", {})
        if ext_ids.get("isrc"):
            metadata["isrc"] = ext_ids["isrc"]

        # Cover art from Spotify album
        album_data = spotify.get("album", {})
        images = album_data.get("images", [])
        if images:
            # Prefer 640x640
            for img in images:
                if img.get("height") == 640:
                    metadata["cover_art_url"] = img["url"]
                    break
            if not metadata["cover_art_url"]:
                metadata["cover_art_url"] = images[0].get("url")

        # Album name from Spotify (often more accurate)
        if album_data.get("name"):
            metadata["album"] = album_data["name"]

    # Extract Apple Music data
    apple = result.get("apple_music")
    if apple:
        metadata["dsp_ids"]["apple_music"] = apple.get("url")

    # Extract Deezer data
    deezer = result.get("deezer")
    if deezer:
        metadata["dsp_ids"]["deezer"] = str(deezer.get("id", ""))

    # ISRC fallback from top-level
    if not metadata["isrc"] and result.get("isrc"):
        metadata["isrc"] = result["isrc"]

    # Label from top-level
    if result.get("label"):
        metadata["label"] = result["label"]

    # Score: AudD returns timecode + score for enterprise
    if result.get("score"):
        try:
            metadata["audd_score"] = float(result["score"]) / 100.0
        except (ValueError, TypeError):
            pass

    return metadata
