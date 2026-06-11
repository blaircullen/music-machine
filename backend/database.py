import sqlite3
from pathlib import Path
from contextlib import contextmanager
import os

DB_PATH = Path(os.environ.get("DB_PATH", "/data/music-machine.db"))


def init_db():
    with get_db() as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")

        # Migrations that DROP tables must run before executescript so CREATE IF NOT EXISTS
        # sees no existing table and creates the new schema.
        # executescript() issues an implicit COMMIT, which applies the DROPs first.
        _migrate_stations_to_sonic(db)

        db.executescript("""
            CREATE TABLE IF NOT EXISTS tracks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_path TEXT UNIQUE NOT NULL,
                file_size INTEGER,
                format TEXT,
                bitrate INTEGER,
                bit_depth INTEGER,
                sample_rate INTEGER,
                duration REAL,
                artist TEXT,
                album_artist TEXT,
                album TEXT,
                title TEXT,
                track_number INTEGER,
                disc_number INTEGER,
                fingerprint TEXT,
                sha256 TEXT,
                scanned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                status TEXT DEFAULT 'active'
            );

            CREATE TABLE IF NOT EXISTS dupe_groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                match_type TEXT,
                confidence REAL,
                resolved INTEGER DEFAULT 0,
                kept_track_id INTEGER REFERENCES tracks(id)
            );

            CREATE TABLE IF NOT EXISTS dupe_group_members (
                group_id INTEGER REFERENCES dupe_groups(id),
                track_id INTEGER REFERENCES tracks(id),
                PRIMARY KEY (group_id, track_id)
            );

            CREATE TABLE IF NOT EXISTS upgrade_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                track_id INTEGER REFERENCES tracks(id),
                search_query TEXT,
                status TEXT DEFAULT 'pending',
                match_quality TEXT,
                mg_track_id TEXT,
                mg_job_id TEXT,
                mg_quality TEXT,
                mg_source_url TEXT,
                staging_path TEXT,
                sha256_original TEXT,
                sha256_new TEXT,
                error_msg TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS file_transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                track_id INTEGER,
                action TEXT,
                source_path TEXT,
                dest_path TEXT,
                state TEXT DEFAULT 'committed',
                sha256_before TEXT,
                sha256_after TEXT,
                performed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_type TEXT,
                status TEXT DEFAULT 'running',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                error_msg TEXT,
                details TEXT
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_tracks_artist_title ON tracks(artist, title);
            CREATE INDEX IF NOT EXISTS idx_tracks_status ON tracks(status);
            CREATE INDEX IF NOT EXISTS idx_tracks_format ON tracks(format);
            CREATE INDEX IF NOT EXISTS idx_upgrade_queue_status ON upgrade_queue(status);
            CREATE INDEX IF NOT EXISTS idx_upgrade_queue_track_id ON upgrade_queue(track_id);
            CREATE INDEX IF NOT EXISTS idx_file_transactions_track_id ON file_transactions(track_id);
            CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);

            CREATE TABLE IF NOT EXISTS tag_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                track_id INTEGER REFERENCES tracks(id),
                file_path TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                acoustid_score REAL,
                mb_recording_id TEXT,
                mb_release_id TEXT,
                matched_artist TEXT,
                matched_title TEXT,
                matched_album TEXT,
                cover_art_url TEXT,
                error_msg TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_tag_jobs_status ON tag_jobs(status);
            CREATE INDEX IF NOT EXISTS idx_tag_jobs_file_path ON tag_jobs(file_path);

            CREATE TABLE IF NOT EXISTS stations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                seed_track_ids TEXT NOT NULL DEFAULT '[]',
                plex_playlist_name TEXT NOT NULL,
                track_count INTEGER NOT NULL DEFAULT 0,
                last_refreshed TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS station_track_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                station_id INTEGER NOT NULL REFERENCES stations(id) ON DELETE CASCADE,
                track_id INTEGER NOT NULL,
                generated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS track_features (
                track_id         INTEGER PRIMARY KEY REFERENCES tracks(id),
                bpm              REAL,
                key              TEXT,
                energy           REAL,
                danceability     REAL,
                valence          REAL,
                acousticness     REAL,
                instrumentalness REAL,
                voice_gender     TEXT,
                mood_happy       REAL,
                mood_sad         REAL,
                mood_aggressive  REAL,
                mood_relaxed     REAL,
                genre_electronic REAL,
                genre_rock       REAL,
                genre_pop        REAL,
                genre_hiphop     REAL,
                genre_jazz       REAL,
                feature_vector   BLOB,
                analyzed_at      TEXT
            );

            CREATE TABLE IF NOT EXISTS track_authenticity (
                track_id INTEGER PRIMARY KEY REFERENCES tracks(id),
                verdict TEXT,
                confidence REAL,
                cutoff_hz REAL,
                nyquist_hz REAL,
                shelf_db REAL,
                sharpness REAL,
                source_guess TEXT,
                sample_rate INTEGER,
                spectrogram_path TEXT,
                method_version INTEGER,
                analyzed_at TEXT
            );

            CREATE TABLE IF NOT EXISTS analysis_queue (
                track_id  INTEGER PRIMARY KEY,
                queued_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS authenticity_queue (
                track_id INTEGER PRIMARY KEY,
                queued_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS station_preferences (
                station_id        INTEGER PRIMARY KEY REFERENCES stations(id) ON DELETE CASCADE,
                preference_vector BLOB,
                updated_at        TEXT
            );

            CREATE TABLE IF NOT EXISTS station_blacklist (
                station_id INTEGER NOT NULL,
                track_id   INTEGER NOT NULL,
                expires_at TEXT,
                PRIMARY KEY (station_id, track_id)
            );

            CREATE TABLE IF NOT EXISTS station_feedback (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                station_id INTEGER NOT NULL,
                track_id   INTEGER NOT NULL,
                signal     TEXT NOT NULL,
                source     TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_station_track_history_station_id
                ON station_track_history(station_id);
            CREATE INDEX IF NOT EXISTS idx_station_track_history_generated_at
                ON station_track_history(generated_at);
            CREATE INDEX IF NOT EXISTS idx_station_track_history_station_generated
                ON station_track_history(station_id, generated_at);
            CREATE INDEX IF NOT EXISTS idx_track_features_analyzed_at
                ON track_features(analyzed_at);
            CREATE INDEX IF NOT EXISTS idx_track_authenticity_verdict
                ON track_authenticity(verdict);
            CREATE INDEX IF NOT EXISTS idx_analysis_queue_queued_at
                ON analysis_queue(queued_at);
            CREATE INDEX IF NOT EXISTS idx_station_feedback_station_id
                ON station_feedback(station_id);

            -- Fingerprint verification engine tables
            CREATE TABLE IF NOT EXISTS fingerprint_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                track_id INTEGER NOT NULL REFERENCES tracks(id),
                chromaprint TEXT,
                acoustid_score REAL,
                acoustid_recording_id TEXT,
                acoustid_release_id TEXT,
                audd_score REAL,
                audd_data JSON,
                composite_confidence REAL,
                match_source TEXT,
                matched_artist TEXT,
                matched_title TEXT,
                matched_album TEXT,
                matched_album_artist TEXT,
                matched_year INTEGER,
                matched_track_number INTEGER,
                matched_disc_number INTEGER,
                matched_genre TEXT,
                matched_genre_raw TEXT,
                matched_isrc TEXT,
                matched_label TEXT,
                matched_composer TEXT,
                matched_cover_art_url TEXT,
                matched_spotify_id TEXT,
                matched_dsp_ids JSON,
                status TEXT NOT NULL DEFAULT 'pending',
                error_message TEXT,
                processed_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(track_id)
            );

            CREATE TABLE IF NOT EXISTS tag_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                track_id INTEGER NOT NULL REFERENCES tracks(id),
                fingerprint_result_id INTEGER REFERENCES fingerprint_results(id),
                original_artist TEXT,
                original_title TEXT,
                original_album TEXT,
                original_album_artist TEXT,
                original_year INTEGER,
                original_track_number INTEGER,
                original_disc_number INTEGER,
                original_genre TEXT,
                original_isrc TEXT,
                original_label TEXT,
                original_composer TEXT,
                original_cover_art_hash TEXT,
                original_cover_art BLOB,
                snapshot_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS audd_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                requests INTEGER DEFAULT 0,
                cost_cents REAL DEFAULT 0,
                UNIQUE(date)
            );

            CREATE TABLE IF NOT EXISTS genre_map (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                raw_genre TEXT NOT NULL UNIQUE,
                normalized_genre TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_fp_status ON fingerprint_results(status);
            CREATE INDEX IF NOT EXISTS idx_fp_confidence ON fingerprint_results(composite_confidence);
            CREATE INDEX IF NOT EXISTS idx_fp_track ON fingerprint_results(track_id);
        """)

        # Insert default settings if not present
        defaults = [
            ("auto_resolve_threshold", "0.0"),
            ("upgrade_scan_limit", "0"),
            ("upgrade_concurrency", "2"),
            ("upgrade_include_flac_hires", "true"),
            ("lastfm_api_key", ""),
            ("sonic_concurrency", "2"),
            ("auto_recue_new_imports", "false"),
            ("auto_recue_daily_cap", "50"),
            ("lidarr_recue_enabled", "true"),
            ("lidarr_url", "http://10.0.0.13:8787"),
            ("lidarr_api_key", "2cecee10715a4c1dbe8daa16226f7ed7"),
            ("lidarr_quality_profile_id", "2"),
            ("lossless_concurrency", "1"),
            ("audd_api_key", "0b109e9c1fef8b670abdd86dd24d3c7d"),
            ("audd_monthly_budget", "20"),
            ("fp_auto_threshold", "0.95"),
            ("fp_review_threshold", "0.50"),
            ("fp_concurrency", "12"),
            ("identity_act_enabled", "false"),
        ]
        for key, value in defaults:
            db.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                (key, value)
            )

        # Migrate upgrade_queue from slskd columns to MusicGrabber columns
        _migrate_upgrade_queue(db)
        _migrate_authenticity_queue(db)
        _migrate_track_authenticity(db)
        _migrate_recue_log(db)
        _migrate_freeze_upgrade_queue(db)

        # Seed genre normalization map
        try:
            from genre_normalizer import seed_genre_map
            seed_genre_map()
        except Exception:
            pass


def _migrate_stations_to_sonic(db):
    """
    Drop old Last.fm-based stations schema and recreate with sonic engine schema.
    Detects old schema by presence of 'seed_artists' column on the stations table.
    """
    cursor = db.execute("PRAGMA table_info(stations)")
    cols = {row[1] for row in cursor.fetchall()}
    if "seed_artists" in cols:
        # Old schema present — drop all station-related tables so executescript recreates them
        db.execute("DROP TABLE IF EXISTS station_track_history")
        db.execute("DROP TABLE IF EXISTS stations")


def _migrate_upgrade_queue(db):
    """Add MusicGrabber columns to upgrade_queue if they don't exist (migrate from slskd)."""
    cursor = db.execute("PRAGMA table_info(upgrade_queue)")
    existing_cols = {row[1] for row in cursor.fetchall()}

    new_cols = {
        "mg_track_id": "TEXT",
        "mg_job_id": "TEXT",
        "mg_quality": "TEXT",
        "mg_source_url": "TEXT",
    }
    for col, col_type in new_cols.items():
        if col not in existing_cols:
            db.execute(f"ALTER TABLE upgrade_queue ADD COLUMN {col} {col_type}")


def _migrate_authenticity_queue(db):
    """Add retry/backoff columns to authenticity_queue if they don't exist."""
    cursor = db.execute("PRAGMA table_info(authenticity_queue)")
    existing_cols = {row[1] for row in cursor.fetchall()}

    new_cols = {
        "attempts": "INTEGER DEFAULT 0",
        "next_check_at": "TEXT",
        "last_status": "TEXT",
    }
    for col, col_type in new_cols.items():
        if col not in existing_cols:
            db.execute(f"ALTER TABLE authenticity_queue ADD COLUMN {col} {col_type}")


def _migrate_track_authenticity(db):
    """Add detector detail columns to track_authenticity if they don't exist."""
    cursor = db.execute("PRAGMA table_info(track_authenticity)")
    existing_cols = {row[1] for row in cursor.fetchall()}

    new_cols = {
        "channels": "INTEGER",
        "duration": "REAL",
        "n_windows_used": "INTEGER",
        "error": "TEXT",
    }
    for col, col_type in new_cols.items():
        if col not in existing_cols:
            db.execute(f"ALTER TABLE track_authenticity ADD COLUMN {col} {col_type}")


def _migrate_recue_log(db):
    """Create additive recue outcome log for auto-recue metrics."""
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS recue_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            track_id INTEGER,
            source TEXT,
            status TEXT,
            album TEXT,
            title TEXT,
            recued_at TEXT DEFAULT (datetime('now'))
        )
        """
    )
    db.execute("CREATE INDEX IF NOT EXISTS idx_recue_log_status ON recue_log(status)")


def _migrate_freeze_upgrade_queue(db):
    """
    Park all non-terminal upgrade_queue rows to 'frozen' status.

    Runs at startup inside init_db() before any worker threads start.
    Idempotent: guarded by 'freeze_migration_version' = '1' in settings.
    Non-terminal statuses: pending, searching, found, approved, downloading.
    Terminal statuses untouched: completed, failed, skipped, frozen.
    """
    row = db.execute(
        "SELECT value FROM settings WHERE key = 'freeze_migration_version'"
    ).fetchone()
    if row and row[0] == "1":
        return  # Already applied

    db.execute(
        """UPDATE upgrade_queue
           SET status = 'frozen', updated_at = CURRENT_TIMESTAMP
           WHERE status NOT IN ('completed', 'failed', 'skipped', 'frozen')"""
    )
    db.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES ('freeze_migration_version', '1')"
    )


def identity_act_enabled() -> bool:
    """Return True only when the identity_act_enabled setting is explicitly 'true'.

    Reads fresh from DB on every call (no module-level cache).
    Fails closed: any error or unexpected value returns False.
    """
    try:
        with get_db() as db:
            row = db.execute(
                "SELECT value FROM settings WHERE key = 'identity_act_enabled'"
            ).fetchone()
        return row is not None and row[0] == "true"
    except Exception:
        return False


def log_recue(
    conn_or_path: sqlite3.Connection | str | Path,
    track_id: int,
    source: str,
    status: str,
    album: str | None,
    title: str | None,
) -> None:
    """Append one recue metric row using an existing connection or DB path."""
    should_close = not isinstance(conn_or_path, sqlite3.Connection)
    conn = (
        sqlite3.connect(str(conn_or_path), check_same_thread=False, timeout=30)
        if should_close
        else conn_or_path
    )
    try:
        _migrate_recue_log(conn)
        conn.execute(
            """
            INSERT INTO recue_log (track_id, source, status, album, title)
            VALUES (?, ?, ?, ?, ?)
            """,
            (track_id, source, status, album or "", title or ""),
        )
        if should_close:
            conn.commit()
    finally:
        if should_close:
            conn.close()


@contextmanager
def get_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
