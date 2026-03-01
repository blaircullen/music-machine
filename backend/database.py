import sqlite3
from pathlib import Path
from contextlib import contextmanager
import os

DB_PATH = Path(os.environ.get("DB_PATH", "/data/plex-dedup.db"))


def init_db():
    with get_db() as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
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
        """)

        # Insert default settings if not present
        defaults = [
            ("auto_resolve_threshold", "0.0"),
            ("upgrade_scan_limit", "0"),
            ("upgrade_concurrency", "2"),
            ("upgrade_include_flac_hires", "true"),
        ]
        for key, value in defaults:
            db.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                (key, value)
            )

        # Migrate upgrade_queue from slskd columns to MusicGrabber columns
        _migrate_upgrade_queue(db)


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
