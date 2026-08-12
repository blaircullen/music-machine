#!/usr/bin/env python3
"""Lidarr-primary orchestration for replacing fake FLACs."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any

BACKEND_DIR = Path(__file__).resolve().parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import lidarr_client as lc  # noqa: E402
from lossless_detect import analyze_flac  # noqa: E402


logger = logging.getLogger(__name__)

BASE = os.environ.get("LIDARR_URL", "http://10.0.0.13:8787")
API_KEY = os.environ.get("LIDARR_API_KEY", "2cecee10715a4c1dbe8daa16226f7ed7")
TRASH_DIR = Path(os.environ.get("FAKE_FLAC_TRASH_DIR", "/mnt/nas/music/.fake-flac-trash-20260611"))
MANIFEST_PATH = Path(os.environ.get("FAKE_FLAC_MANIFEST", "data/fake-flac-trash-manifest-20260611.json"))


def _normalize(value: str) -> str:
    value = str(value or "").lower()
    value = re.sub(r"\b(feat|featuring|ft)\.?\b.*$", "", value)
    value = re.sub(r"^\s*\d{1,3}\s*[-.]\s*", "", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _loose_match(candidate: str, target: str) -> bool:
    cand = _normalize(candidate)
    targ = _normalize(target)
    if not cand or not targ:
        return False
    if cand == targ or cand in targ or targ in cand:
        return True
    cand_tokens = set(cand.split())
    targ_tokens = set(targ.split())
    return bool(cand_tokens and targ_tokens and len(cand_tokens & targ_tokens) / max(len(cand_tokens), len(targ_tokens)) >= 0.60)


def _title_from_path(path: str) -> str:
    return Path(path).stem


def _trackfile_title(trackfile: dict[str, Any]) -> str:
    for key in ("title", "trackTitle"):
        if trackfile.get(key):
            return str(trackfile[key])
    tracks = trackfile.get("tracks")
    if isinstance(tracks, list) and tracks:
        titles = [str(track.get("title") or "") for track in tracks if isinstance(track, dict)]
        return " ".join(title for title in titles if title)
    return _title_from_path(str(trackfile.get("path") or ""))


def _matching_trackfile(trackfiles: list[dict[str, Any]], track_meta: dict[str, Any]) -> dict[str, Any] | None:
    target_path = str(track_meta.get("file_path") or "")
    target_basename = Path(target_path).name
    target_title = str(track_meta.get("title") or "")
    for trackfile in trackfiles:
        path = str(trackfile.get("path") or "")
        if target_basename and Path(path).name == target_basename:
            return trackfile
    for trackfile in trackfiles:
        if _loose_match(_trackfile_title(trackfile), target_title) or _loose_match(_title_from_path(str(trackfile.get("path") or "")), target_title):
            return trackfile
    return None


def _append_manifest(orig: Path, trash: Path, track_meta: dict[str, Any]) -> None:
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "orig": str(orig),
        "trash": str(trash),
        "artist": track_meta.get("artist") or "",
        "album": track_meta.get("album") or "",
        "title": track_meta.get("title") or "",
        "ts": int(time.time()),
    }
    rows: list[dict[str, Any]] = []
    if MANIFEST_PATH.exists():
        try:
            with MANIFEST_PATH.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, list):
                rows = [item for item in data if isinstance(item, dict)]
        except Exception as exc:
            logger.warning("Could not read manifest %s before append: %s", MANIFEST_PATH, exc)
    rows.append(row)
    with MANIFEST_PATH.open("w", encoding="utf-8") as fh:
        json.dump(rows, fh, indent=2, sort_keys=True)
        fh.write("\n")


def _quarantine(path: str, track_meta: dict[str, Any]) -> Path:
    source = Path(path)
    TRASH_DIR.mkdir(parents=True, exist_ok=True)
    dest = TRASH_DIR / source.name
    if dest.exists():
        dest = TRASH_DIR / f"{source.stem}-{int(time.time())}{source.suffix}"
    moved = Path(shutil.move(str(source), str(dest)))
    _append_manifest(source, moved, track_meta)
    return moved


def configure(base: str | None = None, api_key: str | None = None) -> None:
    global BASE, API_KEY
    if base:
        BASE = base
    if api_key:
        API_KEY = api_key


def recue_via_lidarr(track_meta: dict[str, Any], in_place_path: str | None, dry_run: bool = False) -> str:
    old_dry_run = lc.DRY_RUN
    lc.DRY_RUN = dry_run
    try:
        artist = lc.find_artist(str(track_meta.get("artist") or ""), BASE, API_KEY)
        if not artist:
            return "lidarr_no_album"
        album = lc.find_album(int(artist["id"]), str(track_meta.get("album") or ""), BASE, API_KEY)
        if not album:
            return "lidarr_no_album"

        album_id = int(album["id"])
        if lc.album_inflight(album_id, BASE, API_KEY):
            return "lidarr_inflight"

        if in_place_path and os.path.exists(in_place_path):
            if dry_run:
                logger.info("DRY_RUN quarantine %s -> %s", in_place_path, TRASH_DIR)
            else:
                _quarantine(in_place_path, track_meta)

        trackfile = _matching_trackfile(lc.album_trackfiles(album_id, BASE, API_KEY), track_meta)
        if trackfile and trackfile.get("id") is not None:
            lc.delete_trackfile(int(trackfile["id"]), BASE, API_KEY, delete_files=False)

        lc.ensure_monitored_lossless(artist, album, BASE, API_KEY)
        lc.trigger_album_search(album_id, BASE, API_KEY)
        return "lidarr_searching"
    finally:
        lc.DRY_RUN = old_dry_run


def check_lidarr_result(track_meta: dict[str, Any], album_id: int) -> str:
    target_title = str(track_meta.get("title") or "")
    target_basename = Path(str(track_meta.get("file_path") or "")).name
    for path in lc.album_track_paths(int(album_id), BASE, API_KEY):
        if target_basename and Path(path).name == target_basename:
            matches = True
        else:
            matches = _loose_match(_title_from_path(path), target_title)
        if not matches or not os.path.exists(path):
            continue
        analysis = analyze_flac(path)
        if str(analysis.get("verdict") or "") == "lossless":
            return "placed_lidarr"
    return "lidarr_pending"
