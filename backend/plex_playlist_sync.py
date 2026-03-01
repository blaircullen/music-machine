"""
Plex Playlist Sync — syncs MusicGrabber M3U playlists to Plex.

Reads M3U files from the Singles directory, resolves tracks via Plex search
by artist+title, and creates/updates Plex playlists using ratingKeys so they
survive file moves from Library Reorg.
"""

import logging
import os
import re
import time
from pathlib import Path
from typing import Optional
import requests

logger = logging.getLogger(__name__)

PLEX_URL = os.environ.get("PLEX_URL", "http://10.0.0.7:32400")
PLEX_TOKEN = os.environ.get("PLEX_TOKEN", "fzVAhz-21g7CfJvA7jK8")
MUSIC_PATH = os.environ.get("MUSIC_PATH", "/music")
SINGLES_DIR = os.path.join(MUSIC_PATH, "Singles")
MUSIC_SECTION_ID = os.environ.get("PLEX_MUSIC_SECTION", "2")

_machine_id: Optional[str] = None


def _plex_get(path: str, params: Optional[dict] = None) -> requests.Response:
    """Make a GET request to Plex API."""
    url = f"{PLEX_URL}{path}"
    p = {"X-Plex-Token": PLEX_TOKEN}
    if params:
        p.update(params)
    headers = {"Accept": "application/json"}
    resp = requests.get(url, params=p, headers=headers, timeout=15)
    resp.raise_for_status()
    return resp


def _plex_put(path: str, params: Optional[dict] = None) -> requests.Response:
    url = f"{PLEX_URL}{path}"
    p = {"X-Plex-Token": PLEX_TOKEN}
    if params:
        p.update(params)
    headers = {"Accept": "application/json"}
    resp = requests.put(url, params=p, headers=headers, timeout=15)
    resp.raise_for_status()
    return resp


def _plex_delete(path: str, params: Optional[dict] = None) -> requests.Response:
    url = f"{PLEX_URL}{path}"
    p = {"X-Plex-Token": PLEX_TOKEN}
    if params:
        p.update(params)
    resp = requests.delete(url, params=p, timeout=15)
    resp.raise_for_status()
    return resp


def _plex_post(path: str, params: Optional[dict] = None) -> requests.Response:
    url = f"{PLEX_URL}{path}"
    p = {"X-Plex-Token": PLEX_TOKEN}
    if params:
        p.update(params)
    headers = {"Accept": "application/json"}
    resp = requests.post(url, params=p, headers=headers, timeout=15)
    resp.raise_for_status()
    return resp


def get_machine_id() -> str:
    """Get the Plex server machine identifier (cached)."""
    global _machine_id
    if _machine_id:
        return _machine_id
    resp = _plex_get("/")
    data = resp.json()
    _machine_id = data["MediaContainer"]["machineIdentifier"]
    return _machine_id


def _normalize(s: str) -> str:
    """Normalize string for fuzzy matching: lowercase, strip punctuation."""
    s = s.lower().strip()
    # Decode common unicode escapes from MusicGrabber (u0026 = &)
    s = re.sub(r'u0026', '&', s)
    s = re.sub(r"['\u2019\u2018\u2032]", "", s)  # Remove apostrophes/primes
    # Transliterate common accented chars (both strings must be 25 chars)
    s = s.translate(str.maketrans(
        "àáâãäåèéêëìíîïòóôõöùúûüýñ",
        "aaaaaaeeeeiiiiooooouuuuyn",
    ))
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _sanitize_search(s: str) -> str:
    """Sanitize a string for Plex API search: fix quotes."""
    s = s.replace("\u2019", "'").replace("\u2018", "'")
    s = s.replace("\u201c", '"').replace("\u201d", '"')
    return s


def _search_variants(title: str) -> list[str]:
    """Generate search query variants for Plex API (handles apostrophe mismatches)."""
    sanitized = _sanitize_search(title)
    variants = [sanitized]
    # Plex stores curly apostrophes in metadata but API search is picky.
    # Try truncating at apostrophe (partial match often works better).
    if "'" in sanitized:
        # "Don't Stop Believin'" → "Don"  is too short, so find longest word-boundary
        # before the first apostrophe that's at least 3 chars
        truncated = sanitized.split("'")[0].strip()
        if len(truncated) >= 3:
            variants.append(truncated)
        # Also try removing apostrophes entirely
        no_apos = sanitized.replace("'", "")
        if no_apos != sanitized:
            variants.append(no_apos)
    return variants


def _clean_title(title: str) -> str:
    """Strip MusicGrabber suffixes and noise from track titles."""
    # Remove parenthetical/bracketed suffixes
    title = re.sub(r"\s*\(from\b[^)]*\)", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\s*\[(?:Video|[^\]]*Remaster[^\]]*|\d{4})\]", "", title, flags=re.IGNORECASE)
    # Remove remaster/version tags in parens: (Remaster), (2013 Remaster), (Remastered 2011),
    # (2005 Remaster), (Original), (Edit), (Mono), (2011), (2012 - Remaster)
    title = re.sub(r"\s*\((?:Remaster(?:ed)?(?:\s+\d{4})?|\d{4}\s*[-–]?\s*Remaster(?:ed)?|Original(?:\s+Album\s+Version)?|Edit|Mono|Demo|\d[\d\s]*(?:Single\s+)?Edit|\d{4})\)", "", title, flags=re.IGNORECASE)
    # Same in brackets: [2003 Remaster]
    title = re.sub(r"\s*\[(?:Remaster(?:ed)?(?:\s+\d{4})?|\d{4}\s*[-–]?\s*Remaster(?:ed)?|\d{4})\]", "", title, flags=re.IGNORECASE)
    # Remove trailing dash suffixes: "- From", "- 2004 Remaster", "- A COLORS SHOW",
    # "- Music From", "- Remastered 2016", "- 2015 Remastered", "- Remaster",
    # "- Original Album Version", "- Mono", "- Pt. 1", "- Edit", "- 2012 - Remaster"
    title = re.sub(
        r"\s+-\s+(?:From|Music From|A COLORS SHOW"
        r"|\d{4}\s+Remaster(?:ed)?"
        r"|Remaster(?:ed)?(?:\s+\d{4})?"
        r"|\d{4}\s*[-–]\s*Remaster(?:ed)?"
        r"|Original(?:\s+Album\s+Version)?"
        r"|Mono|Edit"
        r"|Pt\.\s*\d+)\s*$",
        "", title, flags=re.IGNORECASE,
    )
    # Remove "Video Official", "Official Audio", "Official Video"
    title = re.sub(r"\s*(?:Video\s+)?Official(?:\s+(?:Audio|Video))?\s*$", "", title, flags=re.IGNORECASE)
    # Remove surrounding quotes: 'Title'
    title = re.sub(r"^['\u2018\u2019]+|['\u2018\u2019]+$", "", title)
    return title.strip()


def _strip_collab(title: str) -> str:
    """Remove featuring/with/+ collaborator suffixes from track titles."""
    # feat./ft./featuring in parens or inline
    title = re.split(r"\s*[\(\[]*\s*(?:feat\.?|ft\.?|featuring)\s", title, flags=re.IGNORECASE)[0]
    # (with Artist)
    title = re.sub(r"\s*\(with\b[^)]*\)", "", title, flags=re.IGNORECASE)
    # " + Artist Name" at end of title
    title = re.sub(r"\s*\+\s+[A-Z].*$", "", title)
    return title.strip()


def _extract_artists(artist_str: str) -> list[str]:
    """Split multi-artist string into individual artists for search attempts."""
    artists = []
    # Split on ", " and " & " but not within parentheses
    # First try the full string
    artists.append(artist_str)
    # Then split on common delimiters
    parts = re.split(r"\s*(?:,\s*|\s+&\s+)", artist_str)
    if len(parts) > 1:
        artists.append(parts[0])  # Primary artist
    return artists


def _score_match(
    norm_artist: str, norm_title: str,
    t_artist: str, t_original: str, t_title: str,
    artist_variants: list[str],
) -> int:
    """Score how well a Plex track matches. 0 = no match."""
    # Title must match
    if t_title != norm_title and norm_title not in t_title and t_title not in norm_title:
        return 0

    # Exact title match bonus
    title_bonus = 10 if t_title == norm_title else 0

    # Artist matching
    for i, artist_norm in enumerate(artist_variants):
        priority = 100 - (i * 10)  # Primary artist scores higher
        if t_artist == artist_norm or t_original == artist_norm:
            return priority + title_bonus
        if artist_norm in t_artist or t_artist in artist_norm:
            return priority - 20 + title_bonus
        if t_original and (artist_norm in t_original or t_original in artist_norm):
            return priority - 30 + title_bonus

    # Title matched but no artist match — still useful as last resort
    # (handles "Various Artists" compilations)
    return 0


def search_plex_track(artist: str, title: str) -> Optional[str]:
    """
    Search Plex for a track by artist and title.
    Tries multiple cleaned variants of the title.
    Returns the ratingKey if found, None otherwise.
    """
    # Build title variants to search (most specific → least)
    title_variants = []
    title_variants.append(title)

    cleaned = _clean_title(title)
    if cleaned != title:
        title_variants.append(cleaned)

    no_collab = _strip_collab(cleaned)
    if no_collab != cleaned:
        title_variants.append(no_collab)

    # If artist name is repeated in title, strip it: "Dominic Fike Babydoll" → "Babydoll"
    norm_first_artist = _normalize(artist.split(",")[0].split("&")[0].strip())
    stripped_artist_from_title = re.sub(
        re.escape(artist.split(",")[0].split("&")[0].strip()) + r"\s+",
        "", no_collab, count=1, flags=re.IGNORECASE,
    ).strip()
    if stripped_artist_from_title and stripped_artist_from_title != no_collab:
        title_variants.append(stripped_artist_from_title)

    # Dedupe while preserving order
    seen = set()
    unique_variants = []
    for v in title_variants:
        nv = _normalize(v)
        if nv and nv not in seen:
            seen.add(nv)
            unique_variants.append(v)

    # Build artist variants
    artist_list = _extract_artists(artist)
    artist_norms = [_normalize(a) for a in artist_list]

    best_key = None
    best_score = 0

    for search_title in unique_variants:
        norm_title = _normalize(search_title)
        for query in _search_variants(search_title):
            try:
                resp = _plex_get(f"/library/sections/{MUSIC_SECTION_ID}/all", {
                    "type": "10",
                    "title": query,
                })
                data = resp.json()
                tracks = data.get("MediaContainer", {}).get("Metadata", [])
            except Exception as e:
                logger.warning(f"Plex search failed for '{artist} - {query}': {e}")
                continue

            for track in tracks:
                t_title = _normalize(track.get("title", ""))
                t_artist = _normalize(track.get("grandparentTitle", ""))
                t_original = _normalize(track.get("originalTitle", ""))

                score = _score_match(
                    artist_norms[0], norm_title,
                    t_artist, t_original, t_title,
                    artist_norms,
                )
                if score > best_score:
                    best_score = score
                    best_key = track["ratingKey"]

            if best_score >= 70:
                return best_key

    if best_key and best_score > 0:
        return best_key

    # Last resort: title-only search with cleaned title, accept any artist
    # Catches compilations filed under "Various Artists"
    for search_title in unique_variants:
        norm_title = _normalize(search_title)
        for query in _search_variants(search_title):
            try:
                resp = _plex_get(f"/library/sections/{MUSIC_SECTION_ID}/all", {
                    "type": "10",
                    "title": query,
                })
                data = resp.json()
                tracks = data.get("MediaContainer", {}).get("Metadata", [])
            except Exception:
                continue
            for track in tracks:
                t_title = _normalize(track.get("title", ""))
                if t_title == norm_title:
                    logger.info(
                        f"Fuzzy match (title-only): '{artist} - {title}' → "
                        f"'{track.get('grandparentTitle', '?')} - {track.get('title', '?')}' "
                        f"[{track['ratingKey']}]"
                    )
                    return track["ratingKey"]

    return None


def parse_m3u(m3u_path: str) -> list[dict]:
    """
    Parse an M3U file and extract artist/title from paths.
    Format: Artist Name/Track Title.flac
    Returns list of {artist, title, path}.
    """
    entries = []
    try:
        with open(m3u_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                # Format: Artist/Title.ext
                parts = line.split("/", 1)
                if len(parts) == 2:
                    artist = parts[0].strip()
                    title = Path(parts[1]).stem.strip()
                    # Clean up title: remove " + Featured Artist" suffix
                    entries.append({
                        "artist": artist,
                        "title": title,
                        "path": line,
                    })
    except Exception as e:
        logger.error(f"Failed to parse M3U {m3u_path}: {e}")
    return entries


def _build_uri(machine_id: str, rating_keys: list[str]) -> str:
    """Build the Plex playlist item URI from rating keys."""
    keys_str = ",".join(rating_keys)
    return f"server://{machine_id}/com.plexapp.plugins.library/library/metadata/{keys_str}"


def _get_playlist(name: str) -> Optional[dict]:
    """Find existing playlist by name. Returns playlist dict or None."""
    try:
        resp = _plex_get("/playlists")
        data = resp.json()
        playlists = data.get("MediaContainer", {}).get("Metadata", [])
        for pl in playlists:
            if pl.get("title") == name:
                return pl
    except Exception as e:
        logger.warning(f"Failed to list playlists: {e}")
    return None


def _playlist_id(playlist: dict) -> str:
    """Extract the numeric playlist ID from a playlist dict."""
    # key is like "/playlists/79377/items", ratingKey is "79377"
    return playlist["ratingKey"]


def _get_playlist_track_keys(playlist_id: str) -> list[str]:
    """Get the ratingKeys of all tracks currently in a playlist."""
    keys = []
    try:
        resp = _plex_get(f"/playlists/{playlist_id}/items")
        data = resp.json()
        items = data.get("MediaContainer", {}).get("Metadata", [])
        for item in items:
            keys.append(item["ratingKey"])
    except Exception as e:
        logger.warning(f"Failed to get playlist items: {e}")
    return keys


def sync_m3u_to_plex(m3u_path: str, playlist_name: str) -> dict:
    """
    Sync a single M3U file to a Plex playlist.
    Returns stats: {total, matched, unmatched, added, removed, unmatched_tracks}.
    """
    entries = parse_m3u(m3u_path)
    if not entries:
        return {"total": 0, "matched": 0, "unmatched": 0, "added": 0, "removed": 0,
                "unmatched_tracks": []}

    machine_id = get_machine_id()

    # Resolve M3U entries to Plex ratingKeys
    desired_keys = []
    unmatched_tracks = []
    for entry in entries:
        key = search_plex_track(entry["artist"], entry["title"])
        if key:
            if key not in desired_keys:  # Deduplicate
                desired_keys.append(key)
        else:
            unmatched_tracks.append(f"{entry['artist']} - {entry['title']}")
            logger.info(f"No Plex match: {entry['artist']} - {entry['title']}")

    stats = {
        "total": len(entries),
        "matched": len(desired_keys),
        "unmatched": len(unmatched_tracks),
        "added": 0,
        "removed": 0,
        "unmatched_tracks": unmatched_tracks,
    }

    if not desired_keys:
        logger.warning(f"No tracks matched for playlist '{playlist_name}', skipping")
        return stats

    # Get or create playlist
    existing = _get_playlist(playlist_name)
    if existing:
        pl_id = _playlist_id(existing)
        current_keys = _get_playlist_track_keys(pl_id)

        # Diff: find keys to add and remove
        current_set = set(current_keys)
        desired_set = set(desired_keys)
        to_add = [k for k in desired_keys if k not in current_set]
        to_remove = [k for k in current_keys if k not in desired_set]

        if to_remove:
            uri = _build_uri(machine_id, to_remove)
            _plex_delete(f"/playlists/{pl_id}/items", {"uri": uri})
            stats["removed"] = len(to_remove)
            logger.info(f"Removed {len(to_remove)} tracks from '{playlist_name}'")

        if to_add:
            uri = _build_uri(machine_id, to_add)
            _plex_put(f"/playlists/{pl_id}/items", {"uri": uri})
            stats["added"] = len(to_add)
            logger.info(f"Added {len(to_add)} tracks to '{playlist_name}'")

    else:
        # Create new playlist with all matched tracks
        uri = _build_uri(machine_id, desired_keys)
        _plex_post("/playlists", {
            "type": "audio",
            "title": playlist_name,
            "smart": "0",
            "uri": uri,
        })
        stats["added"] = len(desired_keys)
        logger.info(f"Created playlist '{playlist_name}' with {len(desired_keys)} tracks")

    return stats


def sync_all_m3u_playlists() -> list[dict]:
    """
    Scan the Singles directory for M3U files and sync each to a Plex playlist.
    Returns list of sync results per playlist.
    """
    results = []
    singles = Path(SINGLES_DIR)
    if not singles.exists():
        logger.warning(f"Singles directory not found: {SINGLES_DIR}")
        return results

    m3u_files = sorted(singles.glob("*.m3u"))
    if not m3u_files:
        logger.info("No M3U files found in Singles directory")
        return results

    logger.info(f"Found {len(m3u_files)} M3U files to sync")
    for m3u_file in m3u_files:
        playlist_name = m3u_file.stem
        logger.info(f"Syncing playlist: {playlist_name}")
        try:
            result = sync_m3u_to_plex(str(m3u_file), playlist_name)
            result["playlist"] = playlist_name
            results.append(result)
        except Exception as e:
            logger.error(f"Failed to sync playlist '{playlist_name}': {e}")
            results.append({
                "playlist": playlist_name,
                "error": str(e),
            })

    return results


def trigger_plex_scan():
    """Trigger a Plex library scan for the music section."""
    try:
        _plex_get(f"/library/sections/{MUSIC_SECTION_ID}/refresh")
        logger.info("Triggered Plex library scan")
    except Exception as e:
        logger.error(f"Failed to trigger Plex scan: {e}")
        raise


def wait_for_plex_scan(timeout: int = 120, poll_interval: int = 5):
    """Wait for Plex library scan to complete by polling section status."""
    start = time.time()
    while time.time() - start < timeout:
        try:
            resp = _plex_get(f"/library/sections/{MUSIC_SECTION_ID}")
            data = resp.json()
            dirs = data.get("MediaContainer", {}).get("Directory", [])
            scanning = any(d.get("refreshing") for d in dirs)
            if not scanning:
                logger.info(f"Plex scan complete after {int(time.time() - start)}s")
                return
        except Exception:
            pass
        time.sleep(poll_interval)
    logger.warning(f"Plex scan did not complete within {timeout}s, proceeding anyway")
