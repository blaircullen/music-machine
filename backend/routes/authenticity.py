import json
import os
import sqlite3
import subprocess
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

from database import get_db

router = APIRouter(prefix="/api/authenticity", tags=["authenticity"])

SCAN_PATH = Path("/data/authenticity_scan.jsonl")
SPECTROGRAM_DIR = Path("/data/spectrograms")
MUSIC_ROOT = Path(os.environ.get("MUSIC_PATH", "/music")).resolve()

HOST_MUSIC_PREFIX = "/mnt/nas/music"
CONTAINER_MUSIC_PREFIX = "/music"

ORDER_BY = {
    "confidence desc": "a.confidence DESC",
    "confidence asc": "a.confidence ASC",
    "cutoff_hz desc": "a.cutoff_hz DESC",
    "cutoff_hz asc": "a.cutoff_hz ASC",
    "artist asc": "t.artist COLLATE NOCASE ASC",
    "title asc": "t.title COLLATE NOCASE ASC",
}


def _container_path(path):
    if path.startswith(HOST_MUSIC_PREFIX):
        return CONTAINER_MUSIC_PREFIX + path[len(HOST_MUSIC_PREFIX):]
    return path


def _ingest_scan():
    if not SCAN_PATH.exists():
        raise FileNotFoundError(str(SCAN_PATH))

    ingested = 0
    matched = 0
    unmatched = 0

    with get_db() as db:
        rows = db.execute("SELECT id, file_path FROM tracks").fetchall()
        track_ids = {row["file_path"]: row["id"] for row in rows}

        with SCAN_PATH.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue

                ingested += 1
                scan = json.loads(line)
                file_path = _container_path(scan.get("path", ""))
                track_id = track_ids.get(file_path)
                if not track_id:
                    unmatched += 1
                    continue

                db.execute(
                    """
                    INSERT OR REPLACE INTO track_authenticity (
                        track_id, verdict, confidence, cutoff_hz, nyquist_hz,
                        shelf_db, sharpness, source_guess, sample_rate,
                        method_version, analyzed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                    """,
                    (
                        track_id,
                        scan.get("verdict"),
                        scan.get("confidence"),
                        scan.get("cutoff_hz"),
                        scan.get("nyquist_hz"),
                        scan.get("shelf_db"),
                        scan.get("sharpness"),
                        scan.get("source_guess"),
                        scan.get("sample_rate"),
                        scan.get("method_version"),
                    ),
                )
                matched += 1

    return {"ingested": ingested, "matched": matched, "unmatched": unmatched}


def _render_spectrogram(track_id):
    with get_db() as db:
        row = db.execute(
            "SELECT file_path FROM tracks WHERE id = ? AND status = 'active'",
            (track_id,),
        ).fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Track not found")

    file_path = Path(row["file_path"])
    resolved = file_path.resolve()
    if not resolved.is_relative_to(MUSIC_ROOT):
        raise HTTPException(status_code=403, detail="Access denied")
    if not resolved.exists():
        raise HTTPException(status_code=404, detail="Audio file not found on disk")

    SPECTROGRAM_DIR.mkdir(parents=True, exist_ok=True)
    out_path = SPECTROGRAM_DIR / f"{track_id}.png"
    if out_path.exists():
        return out_path

    try:
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-y",
                "-i",
                str(resolved),
                "-lavfi",
                "showspectrumpic=s=900x420:legend=1",
                str(out_path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        detail = e.stderr.strip() or "ffmpeg spectrogram render failed"
        raise RuntimeError(detail) from e

    return out_path


def _recue_summary(db):
    empty = {
        "triggered": 0,
        "fixed": 0,
        "staged": 0,
        "by_source": {"lidarr": 0, "musicgrabber": 0},
    }
    try:
        triggered = db.execute("SELECT COUNT(DISTINCT track_id) FROM recue_log").fetchone()[0]
        fixed = db.execute(
            "SELECT COUNT(DISTINCT track_id) FROM recue_log WHERE status = 'fixed'"
        ).fetchone()[0]
        staged = db.execute("SELECT COUNT(*) FROM recue_log WHERE status = 'staged'").fetchone()[0]
        by_source_rows = db.execute(
            "SELECT source, COUNT(DISTINCT track_id) AS count FROM recue_log GROUP BY source"
        ).fetchall()
    except sqlite3.OperationalError:
        return empty

    by_source = dict(empty["by_source"])
    for row in by_source_rows:
        source = row["source"] or ""
        if source in by_source:
            by_source[source] = row["count"]
    return {
        "triggered": triggered,
        "fixed": fixed,
        "staged": staged,
        "by_source": by_source,
    }


@router.get("/summary")
def summary():
    with get_db() as db:
        rows = db.execute(
            """SELECT verdict, COUNT(*) as count
               FROM track_authenticity
               GROUP BY verdict
               ORDER BY count DESC"""
        ).fetchall()
        analyzed = db.execute("SELECT COUNT(*) FROM track_authenticity").fetchone()[0]
        total_flac = db.execute(
            """SELECT COUNT(*)
               FROM tracks
               WHERE lower(file_path) LIKE '%.flac' AND status = 'active'"""
        ).fetchone()[0]
        recue = _recue_summary(db)

    coverage_pct = round((analyzed / total_flac * 100), 2) if total_flac else 0.0
    return {
        "counts": {row["verdict"] or "unknown": row["count"] for row in rows},
        "analyzed": analyzed,
        "total_flac": total_flac,
        "coverage_pct": coverage_pct,
        "recue": recue,
    }


@router.post("/ingest")
async def ingest():
    try:
        return await run_in_threadpool(_ingest_scan)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Authenticity scan file not found")
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSONL row: {e}")


@router.get("")
@router.get("/")
def list_authenticity(
    verdict: str | None = None,
    q: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    order: str = "confidence desc",
):
    order_by = ORDER_BY.get(order.lower())
    if not order_by:
        raise HTTPException(status_code=400, detail="Invalid order")

    where = []
    params = []
    if verdict:
        where.append("a.verdict = ?")
        params.append(verdict)
    if q:
        where.append(
            """(
                t.artist LIKE ? OR t.title LIKE ? OR t.album LIKE ? OR t.file_path LIKE ?
            )"""
        )
        like = f"%{q}%"
        params.extend([like, like, like, like])

    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    with get_db() as db:
        total = db.execute(
            f"""
            SELECT COUNT(*)
            FROM track_authenticity a
            JOIN tracks t ON t.id = a.track_id
            {where_sql}
            """,
            params,
        ).fetchone()[0]
        rows = db.execute(
            f"""
            SELECT a.track_id, a.verdict, a.confidence, a.cutoff_hz,
                   a.source_guess, a.sample_rate,
                   t.artist, t.title, t.album, t.file_path
            FROM track_authenticity a
            JOIN tracks t ON t.id = a.track_id
            {where_sql}
            ORDER BY {order_by}
            LIMIT ? OFFSET ?
            """,
            params + [limit, offset],
        ).fetchall()

    return {"total": total, "items": [dict(row) for row in rows]}


@router.get("/{track_id}/spectrogram")
async def spectrogram(track_id: int):
    try:
        png = await run_in_threadpool(_render_spectrogram, track_id)
    except HTTPException:
        raise
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

    return FileResponse(str(png), media_type="image/png")
