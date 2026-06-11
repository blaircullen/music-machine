"""
audio_probe — lightweight ffprobe wrapper for decoded duration.
"""

import json
import logging
import subprocess

logger = logging.getLogger(__name__)


def decoded_duration_ms(file_path: str) -> int | None:
    """
    Return the decoded duration of *file_path* in milliseconds, or None on
    any failure (missing ffprobe binary, non-zero exit, unreadable output).

    Uses ffprobe's JSON output; ``format.duration`` is a float in seconds.
    """
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v", "quiet",
                "-print_format", "json",
                "-show_format",
                file_path,
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout)
        duration_seconds = float(data["format"]["duration"])
        return int(duration_seconds * 1000)
    except Exception as e:
        logger.debug(f"decoded_duration_ms failed for {file_path}: {e}")
        return None
