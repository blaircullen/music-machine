#!/usr/bin/env python3
"""Re-fetch verified-lossless replacements for fake FLAC transcodes."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import string
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

import httpx
from mutagen import File as MutagenFile


REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import lossless_detect  # noqa: E402


BASE = "http://10.0.0.13:38274"
MUSIC_ROOT = Path("/mnt/nas/music")
SINGLES_DIR = MUSIC_ROOT / "Singles"
REJECT_DIR = MUSIC_ROOT / ".recue-rejected"
STAGING_DIR = MUSIC_ROOT / ".recue-staging"

CONFIRMED_MANIFEST = Path("/home/olares/projects/music-machine/data/fake-flac-trash-manifest-20260611.json")
BORDERLINE_SCAN = Path("/home/olares/projects/music-machine/data/authenticity_scan.jsonl")
STATE_FILE = Path("/home/olares/projects/music-machine/data/recue_state.jsonl")

LOSSLESS_QUALITIES = {"HI_RES_LOSSLESS", "LOSSLESS"}
QUALITY_RANK = {"HI_RES_LOSSLESS": 2, "LOSSLESS": 1}
MAX_CANDIDATES = 3
TERMINAL_EXACT = {
    "already_present",
    "placed",
    "staged",
    "no_match",
    "no_metadata",
    "verify_failed_all",
    "dl_failed_all",
    "landed_not_found_all",
    "no_usable_candidate",
}
SEARCH_ATTEMPTS = 8
JOB_TIMEOUT_SECONDS = 300
JOB_POLL_SECONDS = 3
LANDED_WINDOW_SECONDS = 240
TRACK_SLEEP_SECONDS = 1.5


def normalize_text(value: str) -> str:
    value = value.lower()
    table = str.maketrans("", "", string.punctuation)
    return re.sub(r"\s+", "", value.translate(table))


def tokenize(value: str) -> set[str]:
    return {token for token in re.split(r"[\W_]+", value.lower()) if token}


def loose_title_match(candidate: str, target: str) -> bool:
    candidate_norm = normalize_text(candidate)
    target_norm = normalize_text(target)
    if not candidate_norm or not target_norm:
        return False
    if candidate_norm in target_norm or target_norm in candidate_norm:
        return True

    candidate_tokens = tokenize(candidate)
    target_tokens = tokenize(target)
    if not candidate_tokens or not target_tokens:
        return False
    overlap = len(candidate_tokens & target_tokens)
    return overlap / max(1, min(len(candidate_tokens), len(target_tokens))) >= 0.60


def first_tag(tags: Any, keys: tuple[str, ...]) -> str:
    if not tags:
        return ""
    for key in keys:
        values = tags.get(key) if hasattr(tags, "get") else None
        if values:
            if isinstance(values, (list, tuple)):
                return str(values[0]).strip()
            return str(values).strip()
    return ""


def fallback_title(path: Path) -> str:
    name = path.stem.strip()
    name = re.sub(r"^\s*\d{1,3}\s*[-.]\s*", "", name)
    return name.strip()


def fallback_artist(path: Path) -> str:
    parent = path.parent.name.strip()
    grandparent = path.parent.parent.name.strip() if path.parent.parent != path.parent else ""
    for value in (parent, grandparent):
        if value and value not in {".", str(MUSIC_ROOT.name)}:
            return value
    return ""


def ffprobe_tags(path: Path) -> dict[str, str]:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", str(path)],
            check=False,
            capture_output=True,
            text=True,
        )
        data = json.loads(result.stdout or "{}")
        merged: dict[str, str] = {}
        for stream in data.get("streams") or []:
            for key, value in (stream.get("tags") or {}).items():
                merged[str(key).lower()] = str(value).strip()
        for key, value in ((data.get("format") or {}).get("tags") or {}).items():
            merged[str(key).lower()] = str(value).strip()
        return merged
    except Exception:
        return {}


def read_metadata(path: Path) -> dict[str, str]:
    tags = None
    try:
        audio = MutagenFile(path, easy=True)
        tags = audio.tags if audio else None
    except Exception:
        tags = None

    try:
        artist = first_tag(tags, ("artist", "albumartist", "album artist", "performer"))
        title = first_tag(tags, ("title",))
        album = first_tag(tags, ("album",))
    except Exception:
        artist = ""
        title = ""
        album = ""

    if not artist or not title:
        probe_tags = ffprobe_tags(path)
        if not title:
            title = first_tag(probe_tags, ("title",))
        if not artist:
            artist = first_tag(probe_tags, ("artist", "album_artist", "performer"))
        if not album:
            album = first_tag(probe_tags, ("album",))

    if not title:
        title = fallback_title(path)
    if not artist:
        artist = fallback_artist(path)

    return {"artist": artist, "title": title, "album": album}


def load_confirmed() -> list[dict[str, Any]]:
    with CONFIRMED_MANIFEST.open("r", encoding="utf-8") as fh:
        rows = json.load(fh)
    return [
        {
            "which": "confirmed",
            "source": str(row.get("trash") or ""),
            "target": str(row.get("orig") or ""),
        }
        for row in rows
        if row.get("trash") and row.get("orig")
    ]


def load_borderline() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with BORDERLINE_SCAN.open("r", encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("verdict") == "transcode" and float(row.get("confidence") or 0.0) < 0.90 and row.get("path"):
                rows.append(
                    {
                        "which": "borderline",
                        "source": str(row["path"]),
                        "target": str(row["path"]),
                    }
                )
    return rows


def load_work(which: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if which in {"confirmed", "both"}:
        rows.extend(load_confirmed())
    if which in {"borderline", "both"}:
        rows.extend(load_borderline())
    return rows


def is_terminal(status: str) -> bool:
    return status in TERMINAL_EXACT or status.startswith("verify_failed")


def load_completed_targets() -> set[str]:
    completed: set[str] = set()
    if not STATE_FILE.exists():
        return completed
    with STATE_FILE.open("r", encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            target = str(row.get("target") or "")
            status = str(row.get("status") or "")
            if target and is_terminal(status):
                completed.add(target)
    return completed


def append_state(target: str, status: str, quality: str = "", query: str = "") -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    row = {"target": target, "status": status, "quality": quality, "query": query, "ts_omitted": True}
    with STATE_FILE.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")


def search_musicgrabber(client: httpx.Client, query: str) -> list[dict[str, Any]]:
    payload = {"query": query, "source": "monochrome", "limit": 10}
    for attempt in range(SEARCH_ATTEMPTS):
        response = client.post(f"{BASE}/api/search", json=payload)
        if response.status_code == 429:
            time.sleep(min(3**attempt + 1, 30))
            continue
        response.raise_for_status()
        data = response.json()
        return list(data.get("results") or [])
    response.raise_for_status()
    return []


def parse_track_id(source_url: str) -> str:
    match = re.search(r"monochrome://(\d+)", source_url or "")
    return match.group(1) if match else ""


def ranked_candidates(results: list[dict[str, Any]], title: str) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for result in results:
        quality = str(result.get("quality") or "")
        result_title = str(result.get("title") or "")
        if quality not in LOSSLESS_QUALITIES:
            continue
        if not loose_title_match(result_title, title):
            continue
        if not parse_track_id(str(result.get("source_url") or "")):
            continue
        candidates.append(result)
    return sorted(
        candidates,
        key=lambda item: QUALITY_RANK.get(str(item.get("quality") or ""), 0),
        reverse=True,
    )[:MAX_CANDIDATES]


def download_track(client: httpx.Client, track_id: str, source_url: str, artist: str, title: str) -> str:
    payload = {
        "video_id": track_id,
        "source": "monochrome",
        "source_url": source_url,
        "artist": artist,
        "title": title,
        "convert_to_flac": True,
        "download_type": "single",
    }
    response = client.post(f"{BASE}/api/download", json=payload)
    response.raise_for_status()
    return str(response.json().get("job_id") or "")


def poll_job(client: httpx.Client, job_id: str) -> tuple[bool, str]:
    deadline = time.time() + JOB_TIMEOUT_SECONDS
    while time.time() < deadline:
        response = client.get(f"{BASE}/api/jobs/{job_id}")
        response.raise_for_status()
        data = response.json()
        status = str(data.get("status") or "")
        if status in {"completed", "completed_with_errors"}:
            return True, status
        if status == "failed":
            return False, str(data.get("error") or "failed")
        time.sleep(JOB_POLL_SECONDS)
    return False, "timeout"


def recent_flacs_under(root: Path, since: float) -> list[Path]:
    if not root.exists():
        return []
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in {".recue-rejected", ".recue-staging"}]
        for filename in filenames:
            if not filename.lower().endswith(".flac"):
                continue
            path = Path(dirpath) / filename
            try:
                if path.stat().st_mtime >= since:
                    found.append(path)
            except OSError:
                continue
    return found


def file_reasonably_matches(path: Path, title: str) -> bool:
    if loose_title_match(fallback_title(path), title):
        return True
    try:
        meta = read_metadata(path)
    except Exception:
        return False
    return loose_title_match(meta.get("title") or "", title)


def locate_landed(title: str, source: Path, target: Path, started_at: float) -> Path | None:
    since = max(0.0, started_at - 5.0, time.time() - LANDED_WINDOW_SECONDS)
    excluded = {source.resolve(strict=False), target.resolve(strict=False)}
    for root in (SINGLES_DIR, MUSIC_ROOT):
        candidates = []
        for path in recent_flacs_under(root, since):
            resolved = path.resolve(strict=False)
            if resolved in excluded:
                continue
            if file_reasonably_matches(path, title):
                candidates.append(path)
        if candidates:
            return max(candidates, key=lambda item: item.stat().st_mtime)
    return None


def reject_landed(path: Path) -> Path:
    REJECT_DIR.mkdir(parents=True, exist_ok=True)
    dest = REJECT_DIR / path.name
    if dest.exists():
        dest = REJECT_DIR / f"{path.stem}-{int(time.time())}{path.suffix}"
    return Path(shutil.move(str(path), str(dest)))


def staging_path_for(source: Path) -> Path:
    try:
        rel = source.relative_to(MUSIC_ROOT)
    except ValueError:
        rel = Path(source.name)
    return STAGING_DIR / rel


def place_landed(which: str, landed: Path, source: Path, target: Path) -> tuple[str, Path]:
    if which == "confirmed":
        dest = target
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            analysis = lossless_detect.analyze_flac(dest)
            verdict = str(analysis.get("verdict") or "unknown")
            if verdict == "lossless":
                rejected = reject_landed(landed)
                return "already_present", rejected
            reject_landed(dest)
        return "placed", Path(shutil.move(str(landed), str(dest)))

    dest = staging_path_for(source)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest = dest.with_name(f"{dest.stem}-{int(time.time())}{dest.suffix}")
    return "staged", Path(shutil.move(str(landed), str(dest)))


def process_track(client: httpx.Client, row: dict[str, Any], dry_run: bool) -> dict[str, str]:
    source = Path(row["source"])
    target = Path(row["target"])
    which = str(row["which"])

    meta = read_metadata(source)
    artist = meta.get("artist") or ""
    title = meta.get("title") or ""
    if not title:
        return {"target": str(target), "status": "no_metadata", "quality": "", "query": ""}

    query = f"{artist} {title}".strip() or title
    results = search_musicgrabber(client, query)
    candidates = ranked_candidates(results, title)
    if not candidates:
        return {"target": str(target), "status": "no_match", "quality": "", "query": query}

    top_quality = str(candidates[0].get("quality") or "")
    if dry_run:
        return {"target": str(target), "status": "dry_run_would_download", "quality": top_quality, "query": query}

    reasons: set[str] = set()
    for index, candidate in enumerate(candidates):
        if index > 0:
            time.sleep(1.0)

        quality = str(candidate.get("quality") or "")
        candidate_source_url = str(candidate.get("source_url") or "")
        track_id = parse_track_id(candidate_source_url)
        started_at = time.time()

        try:
            job_id = download_track(client, track_id, candidate_source_url, artist, title)
            if not job_id:
                reasons.add("download_failed")
                continue

            ok, _job_status = poll_job(client, job_id)
        except Exception:
            reasons.add("download_failed")
            continue

        if not ok:
            reasons.add("download_failed")
            continue

        landed = locate_landed(title, source, target, started_at)
        if not landed:
            reasons.add("landed_not_found")
            continue

        analysis = lossless_detect.analyze_flac(landed)
        verdict = str(analysis.get("verdict") or "unknown")
        if verdict != "lossless":
            reject_landed(landed)
            reasons.add("verify_failed")
            continue

        status, _dest = place_landed(which, landed, source, target)
        return {"target": str(target), "status": status, "quality": quality, "query": query}

    if "verify_failed" in reasons:
        status = "verify_failed_all"
    elif "download_failed" in reasons:
        status = "dl_failed_all"
    elif "landed_not_found" in reasons:
        status = "landed_not_found_all"
    else:
        status = "no_usable_candidate"
    return {"target": str(target), "status": status, "quality": top_quality, "query": query}


def progress_line(done: int, total: int, counts: Counter[str]) -> None:
    summary = dict(sorted(counts.items()))
    print(f"processed {done}/{total}: {summary}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Re-fetch verified-lossless replacements for fake FLACs")
    parser.add_argument("--which", choices=("confirmed", "borderline", "both"), default="both")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    work = load_work(args.which)
    completed = load_completed_targets()
    work = [row for row in work if row["target"] not in completed]
    if args.limit is not None:
        work = work[: args.limit]

    counts: Counter[str] = Counter()
    total = len(work)
    with httpx.Client(timeout=60.0) as client:
        for index, row in enumerate(work, start=1):
            try:
                result = process_track(client, row, args.dry_run)
            except Exception as exc:
                result = {
                    "target": str(row.get("target") or ""),
                    "status": f"error:{exc}",
                    "quality": "",
                    "query": "",
                }

            append_state(
                result.get("target") or str(row.get("target") or ""),
                result.get("status") or "error:missing_status",
                result.get("quality") or "",
                result.get("query") or "",
            )
            counts[result.get("status") or "error:missing_status"] += 1

            if index % 10 == 0:
                progress_line(index, total, counts)
            if index < total:
                time.sleep(TRACK_SLEEP_SECONDS)

    progress_line(total, total, counts)
    print(f"final summary: {dict(sorted(counts.items()))}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
