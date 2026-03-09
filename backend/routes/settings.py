import os
import logging

from fastapi import APIRouter

from database import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settings", tags=["settings"])

# Keys that can be read/written via the API
ALLOWED_KEYS = {
    "auto_resolve_threshold",
    "upgrade_scan_limit",
    "upgrade_concurrency",
    "lastfm_api_key",
}

# Read-only environment-derived values surfaced in GET response
ENV_KEYS = {
    "music_path": "MUSIC_PATH",
    "trash_path": "TRASH_PATH",
    "staging_path": "STAGING_PATH",
    "musicgrabber_url": "MUSICGRABBER_URL",
}

DEFAULTS = {
    "auto_resolve_threshold": "0.0",
    "upgrade_scan_limit": "0",
    "upgrade_concurrency": "8",
    "lastfm_api_key": "",
}


@router.get("")
@router.get("/")
def get_settings():
    """Return all settings, merging DB values over defaults."""
    settings = dict(DEFAULTS)

    # Add read-only env values
    for key, env_var in ENV_KEYS.items():
        settings[key] = os.environ.get(env_var, "")

    # Override with DB values
    with get_db() as db:
        rows = db.execute("SELECT key, value FROM settings").fetchall()
        for row in rows:
            if row["key"] in ALLOWED_KEYS:
                settings[row["key"]] = row["value"]

    return settings


@router.put("")
@router.put("/")
def update_settings(data: dict):
    """Update writable settings. Ignores unknown or read-only keys."""
    with get_db() as db:
        for key, value in data.items():
            if key not in ALLOWED_KEYS:
                continue
            db.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (key, str(value)),
            )

    return get_settings()
