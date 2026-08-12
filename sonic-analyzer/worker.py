"""
Sonic Analyzer Worker
Polls analysis_queue, runs essentia_streaming_extractor_music on each FLAC,
parses the output JSON, and stores feature scalars + vector in track_features.

Concurrency is controlled by the 'sonic_concurrency' settings key (default 2, max 4).
Each thread independently picks tracks from analysis_queue using an in-memory
in-progress set to prevent double-processing.

Feature vector layout (73 dims, float32):
  [0:13]   MFCC mean (13)
  [13:26]  MFCC var  (13)
  [26:62]  HPCP mean (36)   — chroma, key-position representation
  [62:67]  Spectral: centroid, rolloff, flux, entropy, complexity (5 means)
  [67:70]  Rhythm: bpm_norm, danceability, onset_rate (3)
  [70:72]  Dynamics: dynamic_complexity, average_loudness (2)
  [72:73]  Tonal: key_strength (1)
Total: 73 dims
"""

import json
import logging
import os
import sqlite3
import struct
import subprocess
import tempfile
import threading
import time
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s sonic-worker: %(message)s",
)
logger = logging.getLogger(__name__)

DB_PATH = os.environ.get("DB_PATH", "/data/music-machine.db")
MUSIC_PATH = os.environ.get("MUSIC_PATH", "/music")
POLL_INTERVAL = 15      # seconds to wait when queue is empty
INTER_TRACK_SLEEP = 0.1  # brief pause between tracks per thread

FEATURE_DIM = 73

# In-memory set of track_ids currently being analyzed (prevents double-pick)
_in_progress = set()  # type: ignore[var-annotated]  # older Python compat
_in_progress_lock = threading.Lock()


def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _get(d, *keys, default=0.0):
    """Safe nested dict access. Returns default if any key is missing or value is not numeric."""
    for k in keys:
        if isinstance(d, dict) and k in d:
            d = d[k]
        elif isinstance(d, list):
            try:
                d = d[int(k)]
            except (ValueError, IndexError, TypeError):
                return default
        else:
            return default
    if isinstance(d, (int, float)) and d == d:  # NaN check
        return float(d)
    return default


def _get_list(d, *keys, length=13, default=0.0):
    """Extract a list of floats from nested dict, padded/truncated to length."""
    for k in keys:
        if isinstance(d, dict) and k in d:
            d = d[k]
        else:
            return [default] * length
    if isinstance(d, list):
        result = []
        for v in d[:length]:
            result.append(float(v) if isinstance(v, (int, float)) and v == v else default)
        while len(result) < length:
            result.append(default)
        return result
    return [default] * length


def extract_features(data):  # type: (dict) -> tuple
    """
    Parse Essentia JSON output into scalar features and a fixed-dim float32 vector.
    Returns (scalars_dict, feature_vector_list).
    """
    ll = data.get("lowlevel", {})
    rh = data.get("rhythm", {})
    to = data.get("tonal", {})

    # --- Scalars ---
    bpm = _get(rh, "bpm")
    key_val = to.get("key_edma", {}).get("key", "") or ""
    key_scale = to.get("key_edma", {}).get("scale", "") or ""
    key = f"{key_val} {key_scale}".strip() or None
    energy = _get(ll, "average_loudness")
    danceability = _get(rh, "danceability")
    key_strength = _get(to, "key_edma", "strength")

    # --- Feature vector ---
    vector = []  # type: list

    # MFCC mean + var (26 dims)
    mfcc = ll.get("mfcc", {})
    vector.extend(_get_list(mfcc, "mean", length=13))
    vector.extend(_get_list(mfcc, "var", length=13))

    # HPCP mean (36 dims) — chroma/key representation
    vector.extend(_get_list(to, "hpcp", "mean", length=36))

    # Spectral features — means (5 dims)
    vector.append(_get(ll, "spectral_centroid", "mean"))
    vector.append(_get(ll, "spectral_rolloff", "mean"))
    vector.append(_get(ll, "spectral_flux", "mean"))
    vector.append(_get(ll, "spectral_entropy", "mean"))
    vector.append(_get(ll, "spectral_complexity", "mean"))

    # Rhythm (3 dims)
    bpm_norm = min(bpm / 200.0, 1.0) if bpm > 0 else 0.0
    vector.append(bpm_norm)
    vector.append(danceability)
    vector.append(_get(rh, "onset_rate"))

    # Dynamics (2 dims)
    vector.append(_get(ll, "dynamic_complexity"))
    vector.append(energy)

    # Tonal (1 dim)
    vector.append(key_strength)

    assert len(vector) == FEATURE_DIM, f"Vector dim mismatch: {len(vector)} != {FEATURE_DIM}"

    scalars = {
        "bpm": bpm if bpm > 0 else None,
        "key": key,
        "energy": energy if energy > 0 else None,
        "danceability": danceability if danceability > 0 else None,
    }
    return scalars, vector


def analyze_track(track_id: int, file_path: str) -> bool:
    """
    Run essentia_streaming_extractor_music on file_path, parse output,
    and write results to track_features.
    Returns True on success.
    """
    path = Path(file_path)
    if not path.exists():
        logger.warning(f"Track {track_id}: file not found, removing from queue: {file_path}")
        try:
            conn = get_db()
            conn.execute("DELETE FROM analysis_queue WHERE track_id = ?", (track_id,))
            conn.commit()
            conn.close()
        except Exception:
            pass
        return False

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
        out_path = tf.name

    try:
        # Run at full CPU priority for fastest initial bulk analysis
        cmd = [
            "essentia_streaming_extractor_music",
            str(path),
            out_path,
        ]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            stderr_snippet = result.stderr[:200]
            logger.error(
                f"Track {track_id}: extractor failed (rc={result.returncode}), "
                f"removing from queue: {stderr_snippet}"
            )
            try:
                conn = get_db()
                conn.execute("DELETE FROM analysis_queue WHERE track_id = ?", (track_id,))
                conn.commit()
                conn.close()
            except Exception:
                pass
            return False

        with open(out_path) as f:
            data = json.load(f)

        scalars, vector = extract_features(data)
        blob = struct.pack(f"{FEATURE_DIM}f", *vector)

        conn = get_db()
        try:
            conn.execute(
                """
                INSERT OR REPLACE INTO track_features
                    (track_id, bpm, key, energy, danceability,
                     feature_vector, analyzed_at)
                VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
                """,
                (
                    track_id,
                    scalars["bpm"],
                    scalars["key"],
                    scalars["energy"],
                    scalars["danceability"],
                    blob,
                ),
            )
            conn.execute(
                "DELETE FROM analysis_queue WHERE track_id = ?", (track_id,)
            )
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES ('sonic_cache_dirty', 'true')"
            )
            conn.commit()
            logger.info(
                f"Track {track_id}: analyzed — "
                f"bpm={scalars['bpm']}, key={scalars['key']}"
            )
            return True
        finally:
            conn.close()

    except subprocess.TimeoutExpired:
        logger.error(f"Track {track_id}: extractor timed out after 120s, removing from queue")
        try:
            conn = get_db()
            conn.execute("DELETE FROM analysis_queue WHERE track_id = ?", (track_id,))
            conn.commit()
            conn.close()
        except Exception:
            pass
        return False
    except Exception as e:
        logger.error(f"Track {track_id}: analysis failed: {e}")
        return False
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass


def get_concurrency() -> int:
    try:
        conn = get_db()
        row = conn.execute(
            "SELECT value FROM settings WHERE key = 'sonic_concurrency'"
        ).fetchone()
        conn.close()
        return max(1, min(32, int(row["value"]))) if row else 4
    except Exception:
        return 4


def _worker_thread(worker_id: int):
    """Single worker thread: continuously picks and analyzes tracks from analysis_queue."""
    logger.info(f"Worker thread {worker_id} starting")
    while True:
        try:
            with _in_progress_lock:
                in_prog = list(_in_progress)

            conn = get_db()
            if in_prog:
                placeholders = ",".join("?" * len(in_prog))
                row = conn.execute(
                    f"SELECT aq.track_id, t.file_path "
                    f"FROM analysis_queue aq "
                    f"JOIN tracks t ON t.id = aq.track_id "
                    f"WHERE aq.track_id NOT IN ({placeholders}) "
                    f"ORDER BY aq.queued_at LIMIT 1",
                    in_prog,
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT aq.track_id, t.file_path "
                    "FROM analysis_queue aq "
                    "JOIN tracks t ON t.id = aq.track_id "
                    "ORDER BY aq.queued_at LIMIT 1"
                ).fetchone()
            conn.close()

            if row is None:
                time.sleep(POLL_INTERVAL)
                continue

            track_id = row["track_id"]
            file_path = row["file_path"]

            with _in_progress_lock:
                if track_id in _in_progress:
                    time.sleep(0.1)
                    continue
                _in_progress.add(track_id)

            try:
                analyze_track(track_id, file_path)
                time.sleep(INTER_TRACK_SLEEP)
            finally:
                with _in_progress_lock:
                    _in_progress.discard(track_id)

        except Exception as e:
            logger.error(f"Worker {worker_id} error: {e}")
            time.sleep(10)


def run_worker():
    concurrency = get_concurrency()
    logger.info(
        f"Sonic analyzer starting: DB={DB_PATH}, MUSIC={MUSIC_PATH}, "
        f"concurrency={concurrency} (full CPU priority)"
    )

    threads = []
    for i in range(concurrency):
        t = threading.Thread(
            target=_worker_thread,
            args=(i,),
            daemon=True,
            name=f"sonic-worker-{i}",
        )
        t.start()
        threads.append(t)

    # Block until all threads die (they don't — this runs forever)
    for t in threads:
        t.join()


if __name__ == "__main__":
    run_worker()
