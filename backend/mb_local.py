"""
Local MusicBrainz query layer — queries a local PostgreSQL replica (via mbslave)
instead of the public MusicBrainz API. Falls back to the public API if the
local mirror is unavailable.

Provides the same interface as musicbrainzngs but without rate limiting.
"""

import logging
import os

logger = logging.getLogger(__name__)

# PostgreSQL connection string for the local MusicBrainz mirror
MB_DB_URI = os.environ.get(
    "MB_DB_URI",
    "postgresql://musicbrainz:musicbrainz@musicbrainz-db:5432/musicbrainz",
)

_pool = None


def _get_pool():
    """Lazy-init a connection pool to the local MusicBrainz PostgreSQL."""
    global _pool
    if _pool is not None:
        return _pool
    try:
        import psycopg2
        from psycopg2 import pool as pg_pool

        _pool = pg_pool.ThreadedConnectionPool(1, 4, MB_DB_URI)
        logger.info("Connected to local MusicBrainz mirror")
        return _pool
    except Exception as e:
        logger.warning(f"Local MusicBrainz mirror unavailable: {e}")
        return None


def is_available() -> bool:
    """Check if the local MusicBrainz mirror is available."""
    pool = _get_pool()
    if pool is None:
        return False
    try:
        conn = pool.getconn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT 1 FROM musicbrainz.recording LIMIT 1")
            cur.fetchone()
            return True
        finally:
            pool.putconn(conn)
    except Exception:
        return False


def get_recording_metadata(recording_mbid: str) -> dict | None:
    """
    Fetch full recording metadata from the local MusicBrainz mirror.

    Returns dict with: artist, title, album, album_artist, date, track_number,
    disc_number, total_tracks, release_group_id, release_id, isrc, label,
    composer, genre_tags (list of {tag, count}).

    Returns None if not found or mirror unavailable.
    """
    pool = _get_pool()
    if pool is None:
        return None

    try:
        conn = pool.getconn()
        try:
            return _query_recording(conn, recording_mbid)
        finally:
            pool.putconn(conn)
    except Exception as e:
        logger.warning(f"Local MB query failed for {recording_mbid}: {e}")
        return None


def _query_recording(conn, recording_mbid: str) -> dict | None:
    """Execute the actual queries against the local MB database."""
    cur = conn.cursor()

    # 1. Get recording basic info
    cur.execute("""
        SELECT r.id, r.name AS title, r.length
        FROM musicbrainz.recording r
        WHERE r.gid = %s::uuid
    """, (recording_mbid,))
    rec_row = cur.fetchone()
    if not rec_row:
        return None

    recording_id = rec_row[0]
    title = rec_row[1]

    # 2. Get artist credits
    cur.execute("""
        SELECT a.name, acn.join_phrase
        FROM musicbrainz.artist_credit_name acn
        JOIN musicbrainz.artist a ON a.id = acn.artist
        JOIN musicbrainz.recording r ON r.artist_credit = acn.artist_credit
        WHERE r.id = %s
        ORDER BY acn.position
    """, (recording_id,))
    artist_parts = cur.fetchall()
    artist = "".join(name + (join or "") for name, join in artist_parts)

    # 3. Get best release (prefer Album type, official, earliest date)
    cur.execute("""
        SELECT
            rel.gid AS release_mbid,
            rel.name AS release_name,
            rg.gid AS release_group_mbid,
            rgpt.name AS primary_type,
            rs.name AS release_status,
            make_date(
                COALESCE(re.date_year, 9999),
                COALESCE(re.date_month, 1),
                COALESCE(re.date_day, 1)
            ) AS release_date,
            re.date_year,
            t.position AS track_number,
            m.position AS disc_number,
            m.track_count AS total_tracks,
            aa.name AS album_artist_name
        FROM musicbrainz.track t
        JOIN musicbrainz.medium m ON m.id = t.medium
        JOIN musicbrainz.release rel ON rel.id = m.release
        JOIN musicbrainz.release_group rg ON rg.id = rel.release_group
        LEFT JOIN musicbrainz.release_group_primary_type rgpt ON rgpt.id = rg.type
        LEFT JOIN musicbrainz.release_country re ON re.release = rel.id
        LEFT JOIN musicbrainz.release_status rs ON rs.id = rel.status
        LEFT JOIN musicbrainz.artist_credit_name acn_rel
            ON acn_rel.artist_credit = rel.artist_credit AND acn_rel.position = 0
        LEFT JOIN musicbrainz.artist aa ON aa.id = acn_rel.artist
        WHERE t.recording = %s
        ORDER BY
            CASE WHEN rgpt.name = 'Album' THEN 0
                 WHEN rgpt.name = 'EP' THEN 1
                 WHEN rgpt.name = 'Single' THEN 2
                 ELSE 3 END,
            CASE WHEN rs.name = 'Official' THEN 0 ELSE 1 END,
            COALESCE(re.date_year, 9999),
            COALESCE(re.date_month, 12),
            COALESCE(re.date_day, 28)
        LIMIT 1
    """, (recording_id,))
    rel_row = cur.fetchone()

    album = ""
    album_artist = ""
    date = ""
    track_number = None
    disc_number = None
    total_tracks = None
    release_group_id = None
    release_id = None

    if rel_row:
        release_id = str(rel_row[0])
        album = rel_row[1] or ""
        release_group_id = str(rel_row[2]) if rel_row[2] else None
        date = str(rel_row[6]) if rel_row[6] and rel_row[6] != 9999 else ""
        track_number = rel_row[7]
        disc_number = rel_row[8]
        total_tracks = rel_row[9]
        album_artist = rel_row[10] or artist

    # 4. Get ISRCs
    cur.execute("""
        SELECT isrc FROM musicbrainz.isrc
        WHERE recording = %s
        LIMIT 1
    """, (recording_id,))
    isrc_row = cur.fetchone()
    isrc = isrc_row[0] if isrc_row else None

    # 5. Get label (from best release)
    label = None
    if rel_row and release_id:
        cur.execute("""
            SELECT l.name
            FROM musicbrainz.release_label rl
            JOIN musicbrainz.label l ON l.id = rl.label
            JOIN musicbrainz.release rel ON rel.id = rl.release
            WHERE rel.gid = %s::uuid
            LIMIT 1
        """, (release_id,))
        label_row = cur.fetchone()
        label = label_row[0] if label_row else None

    # 6. Get composer (from recording work relationships)
    composer = None
    cur.execute("""
        SELECT a.name
        FROM musicbrainz.l_recording_work lrw
        JOIN musicbrainz.link l ON l.id = lrw.link
        JOIN musicbrainz.link_type lt ON lt.id = l.link_type
        JOIN musicbrainz.l_artist_work law ON law.entity1 = lrw.entity1
        JOIN musicbrainz.link l2 ON l2.id = law.link
        JOIN musicbrainz.link_type lt2 ON lt2.id = l2.link_type
        JOIN musicbrainz.artist a ON a.id = law.entity0
        WHERE lrw.entity0 = %s
          AND lt.name = 'performance'
          AND lt2.name = 'composer'
        LIMIT 1
    """, (recording_id,))
    composer_row = cur.fetchone()
    if composer_row:
        composer = composer_row[0]

    # 7. Get genre tags (folksonomy tags with vote counts)
    cur.execute("""
        SELECT t.name, rt.count
        FROM musicbrainz.recording_tag rt
        JOIN musicbrainz.tag t ON t.id = rt.tag
        WHERE rt.recording = %s AND rt.count > 0
        ORDER BY rt.count DESC
        LIMIT 20
    """, (recording_id,))
    genre_tags = [{"tag": row[0], "count": row[1]} for row in cur.fetchall()]

    return {
        "artist": artist,
        "title": title,
        "album": album,
        "album_artist": album_artist,
        "date": date,
        "track_number": track_number,
        "disc_number": disc_number,
        "total_tracks": total_tracks,
        "release_group_id": release_group_id,
        "release_id": release_id,
        "isrc": isrc,
        "label": label,
        "composer": composer,
        "genre_tags": genre_tags,
    }


def search_by_acoustid(recording_mbids: list[str]) -> list[dict]:
    """
    Batch lookup multiple recording MBIDs from the local mirror.
    Returns list of metadata dicts (same shape as get_recording_metadata).
    Skips any that fail or aren't found.
    """
    results = []
    for mbid in recording_mbids:
        meta = get_recording_metadata(mbid)
        if meta:
            results.append(meta)
    return results
