"""
Sonic Station Engine

Replaces the Last.fm-based stations_service.py with a fully local similarity engine.

Pipeline:
  1. Seed station with 3–5 specific tracks
  2. Compute centroid of seed feature vectors
  3. Blend with learned preference_vector (EMA from listening history)
  4. SQL pre-filter (blacklist, recency exclusion)
  5. Cosine similarity against numpy in-memory matrix
  6. Weighted random sample → 35–40 tracks
  7. Sync to Plex playlist

Feature matrix is loaded lazily on first station refresh and invalidated
when sonic-analyzer sets settings.sonic_cache_dirty = 'true'.
"""

import json
import logging
import random
import struct
import threading
from datetime import datetime, timedelta
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

_SAMPLE_SIZE = 38
_RECENCY_DAYS = 7
_EMA_ALPHA = {
    "up": 0.4,
    "down": 0.3,
    "played": 0.15,
    "skipped": 0.05,
}
_BLACKLIST_DAYS = 30
_DIVERSE_SEED_THRESHOLD = 0.4   # pairwise cosine below this → k-means split

# ---------------------------------------------------------------------------
# Feature cache (in-memory numpy matrix)
# ---------------------------------------------------------------------------

_cache_lock = threading.RLock()
_feature_matrix: Optional[np.ndarray] = None  # shape (N, FEATURE_DIM), L2-normalized
_track_ids: Optional[list[int]] = None          # parallel list of track IDs
_cache_dirty = True                              # force reload on first access


def _load_feature_cache():
    """Load all feature vectors from DB into memory. Thread-safe via _cache_lock."""
    global _feature_matrix, _track_ids, _cache_dirty

    from database import get_db
    with get_db() as db:
        rows = db.execute(
            "SELECT track_id, feature_vector FROM track_features "
            "WHERE feature_vector IS NOT NULL ORDER BY track_id"
        ).fetchall()
        # Clear dirty flag
        db.execute(
            "INSERT OR REPLACE INTO settings (key, value) "
            "VALUES ('sonic_cache_dirty', 'false')"
        )

    if not rows:
        _feature_matrix = np.zeros((0, 1), dtype=np.float32)
        _track_ids = []
        _cache_dirty = False
        return

    tids = []
    vectors = []
    for row in rows:
        vec = np.frombuffer(row["feature_vector"], dtype=np.float32).copy()
        tids.append(row["track_id"])
        vectors.append(vec)

    matrix = np.array(vectors, dtype=np.float32)
    # L2-normalize each row
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms = np.where(norms == 0, 1.0, norms)
    matrix = matrix / norms

    _feature_matrix = matrix
    _track_ids = tids
    _cache_dirty = False
    logger.info(f"Feature cache loaded: {len(tids)} tracks, dim={matrix.shape[1]}")


def _ensure_cache():
    """Return (matrix, track_ids), refreshing if dirty."""
    global _cache_dirty
    with _cache_lock:
        if _cache_dirty or _feature_matrix is None:
            # Check DB dirty flag
            try:
                from database import get_db
                with get_db() as db:
                    row = db.execute(
                        "SELECT value FROM settings WHERE key = 'sonic_cache_dirty'"
                    ).fetchone()
                if row and row["value"] == "true":
                    _cache_dirty = True
            except Exception:
                pass

            if _cache_dirty or _feature_matrix is None:
                _load_feature_cache()

        return _feature_matrix, _track_ids


def invalidate_cache():
    """Called when new tracks are analyzed. Sets cache dirty."""
    global _cache_dirty
    with _cache_lock:
        _cache_dirty = True


# ---------------------------------------------------------------------------
# Vector helpers
# ---------------------------------------------------------------------------

def _unpack_vector(blob: bytes) -> Optional[np.ndarray]:
    if not blob:
        return None
    n = len(blob) // 4
    vec = np.frombuffer(blob, dtype=np.float32).copy()
    norm = np.linalg.norm(vec)
    if norm > 0:
        vec = vec / norm
    return vec


def _pack_vector(vec: np.ndarray) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec.astype(np.float32))


def _cosine_similarity(matrix: np.ndarray, target: np.ndarray) -> np.ndarray:
    """matrix rows must already be L2-normalized. target will be normalized here."""
    norm = np.linalg.norm(target)
    if norm > 0:
        target = target / norm
    return matrix @ target


# ---------------------------------------------------------------------------
# Seed centroid + diverse-seed handling
# ---------------------------------------------------------------------------

def _compute_target_vector(seed_track_ids: list[int]) -> Optional[np.ndarray]:
    """
    Compute seed centroid from track feature vectors.
    If seeds are sonically very diverse (pairwise cosine < threshold),
    returns None to signal caller to use k-means split.
    """
    from database import get_db
    placeholders = ",".join("?" * len(seed_track_ids))
    with get_db() as db:
        rows = db.execute(
            f"SELECT track_id, feature_vector FROM track_features "
            f"WHERE track_id IN ({placeholders}) AND feature_vector IS NOT NULL",
            seed_track_ids,
        ).fetchall()

    if not rows:
        return None

    vecs = []
    for row in rows:
        v = _unpack_vector(row["feature_vector"])
        if v is not None:
            vecs.append(v)

    if not vecs:
        return None

    return np.mean(np.array(vecs, dtype=np.float32), axis=0)


def _seeds_are_diverse(seed_track_ids: list[int]) -> bool:
    """Return True if seed tracks are sonically very different from each other."""
    if len(seed_track_ids) < 2:
        return False
    from database import get_db
    placeholders = ",".join("?" * len(seed_track_ids))
    with get_db() as db:
        rows = db.execute(
            f"SELECT feature_vector FROM track_features "
            f"WHERE track_id IN ({placeholders}) AND feature_vector IS NOT NULL",
            seed_track_ids,
        ).fetchall()

    vecs = [_unpack_vector(r["feature_vector"]) for r in rows if r["feature_vector"]]
    vecs = [v for v in vecs if v is not None]
    if len(vecs) < 2:
        return False

    # Check minimum pairwise cosine similarity
    matrix = np.array(vecs, dtype=np.float32)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms = np.where(norms == 0, 1.0, norms)
    matrix = matrix / norms
    sim_matrix = matrix @ matrix.T
    np.fill_diagonal(sim_matrix, 1.0)
    min_sim = sim_matrix.min()
    return float(min_sim) < _DIVERSE_SEED_THRESHOLD


# ---------------------------------------------------------------------------
# Candidate selection
# ---------------------------------------------------------------------------

def _get_excluded_track_ids(station_id: int) -> set[int]:
    """Return track_ids to exclude: active blacklist + recent history."""
    from database import get_db
    now = datetime.now().isoformat()
    cutoff = (datetime.now() - timedelta(days=_RECENCY_DAYS)).isoformat()
    with get_db() as db:
        blacklisted = {
            r["track_id"] for r in db.execute(
                "SELECT track_id FROM station_blacklist "
                "WHERE station_id = ? AND (expires_at IS NULL OR expires_at > ?)",
                (station_id, now),
            ).fetchall()
        }
        recent = {
            r["track_id"] for r in db.execute(
                "SELECT track_id FROM station_track_history "
                "WHERE station_id = ? AND generated_at >= ?",
                (station_id, cutoff),
            ).fetchall()
        }
    return blacklisted | recent


def _similarity_search(
    matrix: np.ndarray,
    track_ids: list[int],
    target: np.ndarray,
    excluded: set[int],
    n: int,
) -> list[int]:
    """
    Cosine similarity search. Returns track IDs of top-weighted random sample.
    Uses similarity scores as weights for weighted sampling (not pure top-N).
    """
    if matrix.shape[0] == 0:
        return []

    sims = _cosine_similarity(matrix, target)  # shape (N,)

    # Build mask for excluded tracks
    tid_array = np.array(track_ids, dtype=np.int64)
    excluded_arr = np.array(list(excluded), dtype=np.int64)
    if len(excluded_arr) > 0:
        mask = np.isin(tid_array, excluded_arr, invert=True)
    else:
        mask = np.ones(len(track_ids), dtype=bool)

    candidate_sims = sims[mask]
    candidate_ids = tid_array[mask]

    if len(candidate_ids) == 0:
        return []

    # Clip negatives to 0 (very dissimilar tracks get zero weight)
    weights = np.clip(candidate_sims, 0, None)
    total = weights.sum()
    if total <= 0:
        weights = np.ones(len(candidate_ids))
        total = float(len(candidate_ids))

    probs = weights / total

    # Weighted sample without replacement
    k = min(n, len(candidate_ids))
    chosen_indices = np.random.choice(len(candidate_ids), size=k, replace=False, p=probs)
    selected = candidate_ids[chosen_indices].tolist()
    random.shuffle(selected)
    return selected


# ---------------------------------------------------------------------------
# Preference vector (EMA learning)
# ---------------------------------------------------------------------------

def _get_preference_vector(station_id: int) -> Optional[np.ndarray]:
    from database import get_db
    with get_db() as db:
        row = db.execute(
            "SELECT preference_vector FROM station_preferences WHERE station_id = ?",
            (station_id,),
        ).fetchone()
    return _unpack_vector(row["preference_vector"]) if row and row["preference_vector"] else None


def _save_preference_vector(station_id: int, vec: np.ndarray):
    from database import get_db
    with get_db() as db:
        db.execute(
            "INSERT OR REPLACE INTO station_preferences (station_id, preference_vector, updated_at) "
            "VALUES (?, ?, ?)",
            (station_id, _pack_vector(vec), datetime.now().isoformat()),
        )


def apply_feedback(station_id: int, track_id: int, signal: str, source: str = "player"):
    """
    Update preference vector via EMA and manage blacklist on thumbs-down.
    signal: "up" | "down" | "played" | "skipped"
    """
    from database import get_db
    alpha = _EMA_ALPHA.get(signal, 0.1)

    with get_db() as db:
        # Record feedback
        db.execute(
            "INSERT INTO station_feedback (station_id, track_id, signal, source, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (station_id, track_id, signal, source, datetime.now().isoformat()),
        )

        # Thumbs down → add to blacklist for 30 days
        if signal == "down":
            expires = (datetime.now() + timedelta(days=_BLACKLIST_DAYS)).isoformat()
            db.execute(
                "INSERT OR REPLACE INTO station_blacklist (station_id, track_id, expires_at) "
                "VALUES (?, ?, ?)",
                (station_id, track_id, expires),
            )

    # Update preference vector via EMA
    with get_db() as db:
        tf_row = db.execute(
            "SELECT feature_vector FROM track_features WHERE track_id = ?", (track_id,)
        ).fetchone()

    if not tf_row or not tf_row["feature_vector"]:
        return

    track_vec = _unpack_vector(tf_row["feature_vector"])
    if track_vec is None:
        return

    pref_vec = _get_preference_vector(station_id)
    if pref_vec is None:
        return  # no preference yet; will be set on next refresh

    if signal == "down":
        # Push away: move preference vector away from track
        new_pref = (1.0 - alpha) * pref_vec - alpha * track_vec
    else:
        # Pull toward: blend preference toward track
        new_pref = alpha * track_vec + (1.0 - alpha) * pref_vec

    _save_preference_vector(station_id, new_pref)


# ---------------------------------------------------------------------------
# Station refresh
# ---------------------------------------------------------------------------

_refresh_status: dict = {}
_status_lock = threading.Lock()


def refresh_station(station: dict) -> dict:
    """
    Run the full sonic recommendation pipeline for one station.
    Returns {ok, track_count, error?}.
    """
    station_id = station["id"]
    name = station["name"]

    with _status_lock:
        _refresh_status[station_id] = {"running": True, "error": None}

    result = {"ok": False, "track_count": 0, "error": None}
    try:
        seed_track_ids = json.loads(station.get("seed_track_ids") or "[]")
        if not seed_track_ids:
            raise ValueError("Station has no seed tracks")

        matrix, track_ids = _ensure_cache()
        if matrix is None or track_ids is None or matrix.shape[0] == 0:
            raise ValueError(
                "No tracks have been analyzed yet. "
                "Wait for the sonic analyzer to process some tracks."
            )

        # Compute seed centroid
        seed_centroid = _compute_target_vector(seed_track_ids)
        if seed_centroid is None:
            raise ValueError(
                "None of the seed tracks have been analyzed yet. "
                "Wait for the sonic analyzer to process them."
            )

        # Blend with learned preference vector (if it exists)
        pref_vec = _get_preference_vector(station_id)
        if pref_vec is not None and pref_vec.shape == seed_centroid.shape:
            target = 0.4 * seed_centroid + 0.6 * pref_vec
        else:
            target = seed_centroid
            # Initialize preference vector from seed centroid
            _save_preference_vector(station_id, seed_centroid)

        excluded = _get_excluded_track_ids(station_id)

        # Check if seeds are sonically diverse → k-means split approach
        if _seeds_are_diverse(seed_track_ids) and len(seed_track_ids) >= 4:
            selected_ids = _refresh_diverse_seeds(
                seed_track_ids, matrix, track_ids, excluded
            )
        else:
            selected_ids = _similarity_search(
                matrix, track_ids, target, excluded, n=_SAMPLE_SIZE
            )

        if not selected_ids:
            raise ValueError(
                "No similar tracks found. "
                "The library may not have enough analyzed tracks yet."
            )

        # Save history
        _save_history(station_id, selected_ids)

        # Convert local track_ids → Plex ratingKeys and sync playlist
        _sync_plex_playlist(station["plex_playlist_name"], selected_ids)
        _update_station(station_id, len(selected_ids))

        result = {"ok": True, "track_count": len(selected_ids)}
        logger.info(f"[{name}] Refreshed with {len(selected_ids)} tracks")

    except Exception as e:
        logger.error(f"[{name}] Refresh failed: {e}")
        result = {"ok": False, "track_count": 0, "error": str(e)}
        with _status_lock:
            _refresh_status[station_id]["error"] = str(e)
    finally:
        with _status_lock:
            _refresh_status[station_id]["running"] = False

    return result


def _refresh_diverse_seeds(
    seed_track_ids: list[int],
    matrix: np.ndarray,
    track_ids: list[int],
    excluded: set[int],
) -> list[int]:
    """
    K-means split for diverse seeds: partition into 2 clusters,
    build two sub-playlists of ~18-20 tracks, interleave.
    """
    from database import get_db
    placeholders = ",".join("?" * len(seed_track_ids))
    with get_db() as db:
        rows = db.execute(
            f"SELECT track_id, feature_vector FROM track_features "
            f"WHERE track_id IN ({placeholders}) AND feature_vector IS NOT NULL",
            seed_track_ids,
        ).fetchall()

    vecs = [_unpack_vector(r["feature_vector"]) for r in rows]
    vecs = [v for v in vecs if v is not None]
    if len(vecs) < 2:
        # Fall back to centroid
        centroid = np.mean(np.array(vecs, dtype=np.float32), axis=0)
        return _similarity_search(matrix, track_ids, centroid, excluded, n=_SAMPLE_SIZE)

    # Simple k=2 split via single Lloyd's iteration from extreme seeds
    arr = np.array(vecs, dtype=np.float32)
    # Pick two most dissimilar seeds as initial centroids
    sims = arr @ arr.T
    np.fill_diagonal(sims, 1.0)
    min_idx = np.unravel_index(sims.argmin(), sims.shape)
    c1, c2 = arr[min_idx[0]], arr[min_idx[1]]

    # Assign seeds to clusters
    cluster_sims = np.stack([arr @ c1, arr @ c2], axis=1)
    labels = cluster_sims.argmax(axis=1)
    c1 = arr[labels == 0].mean(axis=0) if (labels == 0).any() else c1
    c2 = arr[labels == 1].mean(axis=0) if (labels == 1).any() else c2

    half = _SAMPLE_SIZE // 2
    list1 = _similarity_search(matrix, track_ids, c1, excluded, n=half + 2)
    list2 = _similarity_search(matrix, track_ids, c2, excluded | set(list1), n=half + 2)

    # Interleave
    interleaved = []
    for a, b in zip(list1, list2):
        interleaved.extend([a, b])
    interleaved.extend(list1[len(list2):])
    interleaved.extend(list2[len(list1):])
    return interleaved[:_SAMPLE_SIZE]


def _sync_plex_playlist(playlist_name: str, track_ids: list[int]):
    """
    Look up Plex ratingKeys for local track IDs via artist+title search,
    then sync the playlist.
    """
    from database import get_db
    from plex_playlist_sync import search_plex_track, sync_keys_to_playlist

    placeholders = ",".join("?" * len(track_ids))
    with get_db() as db:
        rows = db.execute(
            f"SELECT artist, title FROM tracks WHERE id IN ({placeholders})",
            track_ids,
        ).fetchall()

    rating_keys = []
    for row in rows:
        rk = search_plex_track(row["artist"] or "", row["title"] or "")
        if rk:
            rating_keys.append(rk)

    if rating_keys:
        sync_keys_to_playlist(playlist_name, rating_keys)
    else:
        logger.warning(f"No Plex rating keys found for playlist '{playlist_name}'")


def _save_history(station_id: int, track_ids: list[int]):
    from database import get_db
    cutoff_30 = (datetime.now() - timedelta(days=30)).isoformat()
    now = datetime.now().isoformat()
    with get_db() as db:
        db.execute(
            "DELETE FROM station_track_history WHERE station_id = ? AND generated_at < ?",
            (station_id, cutoff_30),
        )
        for tid in track_ids:
            db.execute(
                "INSERT INTO station_track_history (station_id, track_id, generated_at) "
                "VALUES (?, ?, ?)",
                (station_id, tid, now),
            )


def _update_station(station_id: int, track_count: int):
    from database import get_db
    with get_db() as db:
        db.execute(
            "UPDATE stations SET track_count = ?, last_refreshed = ? WHERE id = ?",
            (track_count, datetime.now().isoformat(), station_id),
        )


def refresh_all_stations():
    """Refresh all stations. Called by the daily scheduler."""
    from database import get_db
    with get_db() as db:
        stations = [dict(r) for r in db.execute("SELECT * FROM stations").fetchall()]

    logger.info(f"Refreshing {len(stations)} stations")
    for station in stations:
        refresh_station(station)


def get_refresh_status(station_id: int) -> dict:
    with _status_lock:
        return dict(_refresh_status.get(station_id, {"running": False, "error": None}))


# ---------------------------------------------------------------------------
# Analysis stats
# ---------------------------------------------------------------------------

def get_analysis_stats() -> dict:
    from database import get_db
    with get_db() as db:
        total = db.execute(
            "SELECT COUNT(*) as n FROM tracks WHERE status = 'active'"
        ).fetchone()["n"]
        analyzed = db.execute(
            "SELECT COUNT(*) as n FROM track_features WHERE feature_vector IS NOT NULL"
        ).fetchone()["n"]
        queued = db.execute(
            "SELECT COUNT(*) as n FROM analysis_queue"
        ).fetchone()["n"]
    return {
        "total_tracks": total,
        "analyzed_count": analyzed,
        "queued_count": queued,
        "coverage_pct": round(analyzed / total * 100, 1) if total > 0 else 0.0,
    }
