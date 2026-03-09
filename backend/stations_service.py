"""
Pandora-style station recommendation engine.

For each station:
1. Expand seed artists via Last.fm artist.getSimilar
2. Filter by listener count (mainstream relevance gate)
3. Cross-reference with Plex library
4. Apply station filters (BPM range, decade)
5. Apply recency weight (tracks heard recently score 0.3x)
6. Weighted random sample -> 35-40 tracks
7. Sync to named Plex playlist
"""
import json
import logging
import random
import threading
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

_DEFAULT_SAMPLE_SIZE = 38
_RECENCY_DAYS = 7
_RECENCY_WEIGHT_MULTIPLIER = 0.3
_MAX_SIMILAR_PER_SEED = 50
_MAX_ARTISTS_TO_QUERY_PLEX = 80   # cap Plex lookups per refresh

# Per-station refresh status (keyed by station id)
_refresh_status: dict = {}
_status_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _build_candidates(
    similar: list[dict],
    plex_tracks: dict,       # artist_name -> list of track dicts
    station: dict,
) -> list[dict]:
    """
    Merge similar artists with their Plex tracks, apply listener + BPM + decade filters.
    Returns list of {ratingKey, weight} dicts.
    """
    min_listeners = station.get("lastfm_min_listeners") or 500_000
    bpm_min = station.get("bpm_min")
    bpm_max = station.get("bpm_max")
    decade_min = station.get("decade_min")
    decade_max = station.get("decade_max")

    candidates = []
    for item in similar:
        name = item["name"]
        match = item["match"]
        listeners = item["listeners"]

        if listeners < min_listeners:
            continue

        tracks = plex_tracks.get(name, [])
        for track in tracks:
            bpm = track.get("bpm")
            year = track.get("year")

            # BPM filter: fail-open when bpm is None (untagged)
            if bpm is not None:
                if bpm_min is not None and bpm < bpm_min:
                    continue
                if bpm_max is not None and bpm > bpm_max:
                    continue

            # Decade filter: fail-open when year is None
            if year is not None:
                if decade_min is not None and year < decade_min:
                    continue
                if decade_max is not None and year > decade_max:
                    continue

            candidates.append({
                "ratingKey": track["ratingKey"],
                "weight": match,
            })

    return candidates


def _apply_recency_weights(
    candidates: list[dict],
    recent_keys: set,
) -> list[dict]:
    """Multiply weight by 0.3 for tracks appearing in recent_keys."""
    result = []
    for c in candidates:
        w = c["weight"]
        if c["ratingKey"] in recent_keys:
            w *= _RECENCY_WEIGHT_MULTIPLIER
        result.append({**c, "weight": w})
    return result


def _weighted_sample(candidates: list[dict], n: int) -> list[str]:
    """
    Weighted sample without replacement. Returns list of ratingKey strings.
    Falls back to full list if fewer candidates than n.
    """
    if not candidates:
        return []

    # Deduplicate by ratingKey (keep highest weight)
    seen: dict[str, float] = {}
    for c in candidates:
        key = c["ratingKey"]
        if key not in seen or c["weight"] > seen[key]:
            seen[key] = c["weight"]

    unique = list(seen.items())  # [(ratingKey, weight), ...]

    if len(unique) <= n:
        random.shuffle(unique)
        return [k for k, _ in unique]

    # Weighted sampling without replacement
    selected = []
    remaining = list(unique)
    for _ in range(n):
        if not remaining:
            break
        weights = [w for _, w in remaining]
        total = sum(weights)
        if total <= 0:
            idx = random.randrange(len(remaining))
        else:
            r = random.uniform(0, total)
            cumulative = 0.0
            idx = len(remaining) - 1
            for i, w in enumerate(weights):
                cumulative += w
                if cumulative >= r:
                    idx = i
                    break
        selected.append(remaining[idx][0])
        remaining.pop(idx)

    random.shuffle(selected)
    return selected


def _get_recent_keys(station_id: int) -> set:
    """Return set of ratingKeys generated for this station in the last 7 days."""
    from database import get_db
    cutoff = (datetime.now() - timedelta(days=_RECENCY_DAYS)).isoformat()
    with get_db() as db:
        rows = db.execute(
            "SELECT rating_key FROM station_track_history "
            "WHERE station_id = ? AND generated_at >= ?",
            (station_id, cutoff),
        ).fetchall()
    return {row["rating_key"] for row in rows}


def _save_history(station_id: int, rating_keys: list[str]):
    """Record generated tracks and prune history older than 30 days."""
    from database import get_db
    cutoff_30 = (datetime.now() - timedelta(days=30)).isoformat()
    now = datetime.now().isoformat()
    with get_db() as db:
        db.execute(
            "DELETE FROM station_track_history WHERE station_id = ? AND generated_at < ?",
            (station_id, cutoff_30),
        )
        for key in rating_keys:
            db.execute(
                "INSERT INTO station_track_history (station_id, rating_key, generated_at) "
                "VALUES (?, ?, ?)",
                (station_id, key, now),
            )


def _update_station(station_id: int, track_count: int):
    from database import get_db
    with get_db() as db:
        db.execute(
            "UPDATE stations SET track_count = ?, last_refreshed = ? WHERE id = ?",
            (track_count, datetime.now().isoformat(), station_id),
        )


def _get_lastfm_key() -> str:
    from database import get_db
    with get_db() as db:
        row = db.execute(
            "SELECT value FROM settings WHERE key = 'lastfm_api_key'"
        ).fetchone()
    return row["value"] if row else ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def refresh_station(station: dict) -> dict:
    """
    Run the full recommendation pipeline for one station.
    Returns {ok, track_count, error?}.
    """
    station_id = station["id"]
    name = station["name"]

    with _status_lock:
        _refresh_status[station_id] = {"running": True, "error": None}

    try:
        api_key = _get_lastfm_key()
        if not api_key:
            raise ValueError("Last.fm API key not configured - add it in Settings")

        seed_artists = json.loads(station["seed_artists"])
        if not seed_artists:
            raise ValueError("Station has no seed artists")

        from lastfm_client import get_similar_artists
        from plex_playlist_sync import get_plex_artist_tracks, sync_keys_to_playlist

        # Step 1: Expand seeds via Last.fm
        logger.info(f"[{name}] Fetching similar artists for {len(seed_artists)} seeds")
        aggregated: dict[str, dict] = {}  # name -> {match, listeners}
        for seed in seed_artists:
            similar = get_similar_artists(seed, api_key=api_key, limit=_MAX_SIMILAR_PER_SEED)
            for item in similar:
                n = item["name"]
                if n in aggregated:
                    aggregated[n]["match"] += item["match"]
                else:
                    aggregated[n] = {"match": item["match"], "listeners": item["listeners"]}

        similar_list = [
            {"name": n, "match": v["match"], "listeners": v["listeners"]}
            for n, v in sorted(aggregated.items(), key=lambda x: -x[1]["match"])
        ]

        logger.info(f"[{name}] {len(similar_list)} unique similar artists found")

        # Step 2: Cross-reference with Plex (cap to top N by match score)
        plex_tracks: dict[str, list] = {}
        candidates_artists = [
            a for a in similar_list
            if a["listeners"] >= (station.get("lastfm_min_listeners") or 500_000)
        ][:_MAX_ARTISTS_TO_QUERY_PLEX]

        logger.info(f"[{name}] Querying Plex for {len(candidates_artists)} artists")
        for item in candidates_artists:
            tracks = get_plex_artist_tracks(item["name"])
            if tracks:
                plex_tracks[item["name"]] = tracks

        logger.info(f"[{name}] {len(plex_tracks)} artists found in library")

        # Step 3: Build + filter candidates
        candidates = _build_candidates(similar_list, plex_tracks, station)
        logger.info(f"[{name}] {len(candidates)} candidates after filters")

        # Step 4: Recency weighting
        recent_keys = _get_recent_keys(station_id)
        candidates = _apply_recency_weights(candidates, recent_keys)

        # Step 5: Sample
        selected_keys = _weighted_sample(candidates, n=_DEFAULT_SAMPLE_SIZE)
        logger.info(f"[{name}] Selected {len(selected_keys)} tracks")

        if not selected_keys:
            raise ValueError(
                "No tracks found. Check seed artists exist in your library and Last.fm key is valid."
            )

        # Step 6: Sync to Plex playlist
        sync_keys_to_playlist(station["plex_playlist_name"], selected_keys)

        # Step 7: Persist history + update station
        _save_history(station_id, selected_keys)
        _update_station(station_id, len(selected_keys))

        with _status_lock:
            _refresh_status[station_id] = {"running": False, "error": None}

        return {"ok": True, "track_count": len(selected_keys)}

    except Exception as e:
        logger.error(f"[{name}] Refresh failed: {e}")
        with _status_lock:
            _refresh_status[station_id] = {"running": False, "error": str(e)}
        return {"ok": False, "track_count": 0, "error": str(e)}


def refresh_all_stations():
    """Refresh all stations. Called by the daily scheduler."""
    from database import get_db
    with get_db() as db:
        stations = db.execute("SELECT * FROM stations").fetchall()

    logger.info(f"Refreshing {len(stations)} stations")
    for station in stations:
        refresh_station(dict(station))


def get_refresh_status(station_id: int) -> dict:
    with _status_lock:
        return dict(_refresh_status.get(station_id, {"running": False, "error": None}))
