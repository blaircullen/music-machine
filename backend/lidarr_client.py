#!/usr/bin/env python3
"""Thin synchronous Lidarr API client for fake-FLAC recue flows."""

from __future__ import annotations

import json
import logging
import re
import string
import time
from dataclasses import dataclass
from typing import Any
from urllib import error, parse, request

try:
    import httpx  # type: ignore
except Exception:  # pragma: no cover - exercised only when httpx is absent
    httpx = None  # type: ignore


logger = logging.getLogger(__name__)

DEFAULT_QUALITY_PROFILE_ID = 2
DRY_RUN = False

_ARTIST_CACHE: dict[tuple[str, str], list[dict[str, Any]]] = {}


@dataclass(frozen=True)
class LidarrClient:
    base: str
    api_key: str


def _api_base(base: str) -> str:
    base = str(base or "").rstrip("/")
    if not base.endswith("/api/v1"):
        base = f"{base}/api/v1"
    return base


def _coerce(base_or_client: str | LidarrClient, api_key: str | None = None) -> LidarrClient:
    if hasattr(base_or_client, "base") and hasattr(base_or_client, "api_key"):
        return LidarrClient(_api_base(str(getattr(base_or_client, "base"))), str(getattr(base_or_client, "api_key")))
    return LidarrClient(_api_base(str(base_or_client)), str(api_key or ""))


def _headers(client: LidarrClient) -> dict[str, str]:
    return {"X-Api-Key": client.api_key, "Content-Type": "application/json"}


def _request(
    method: str,
    path: str,
    base_or_client: str | LidarrClient,
    api_key: str | None = None,
    *,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
) -> Any:
    client = _coerce(base_or_client, api_key)
    url = f"{client.base}{path}"
    if httpx is not None:
        with httpx.Client(timeout=30.0) as session:
            response = session.request(method, url, headers=_headers(client), params=params, json=json_body)
            response.raise_for_status()
            if not response.content:
                return {}
            return response.json()

    if params:
        url = f"{url}?{parse.urlencode(params)}"
    data = None
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
    req = request.Request(url, data=data, method=method, headers=_headers(client))
    try:
        with request.urlopen(req, timeout=30.0) as response:  # noqa: S310 - configured local Lidarr API
            body = response.read()
            return json.loads(body.decode("utf-8")) if body else {}
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Lidarr {method} {url} failed: {exc.code} {body}") from exc


def _normalize(value: str) -> str:
    value = str(value or "").lower()
    value = re.sub(r"\b(feat|featuring|ft)\.?\b.*$", "", value)
    value = value.translate(str.maketrans("", "", string.punctuation))
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _tokens(value: str) -> set[str]:
    return {token for token in re.split(r"[\W_]+", _normalize(value)) if token}


def _score(candidate: str, target: str) -> float:
    cand = _normalize(candidate)
    targ = _normalize(target)
    if not cand or not targ:
        return 0.0
    if cand == targ:
        return 1.0
    if cand in targ or targ in cand:
        return 0.92
    cand_tokens = _tokens(candidate)
    targ_tokens = _tokens(target)
    if not cand_tokens or not targ_tokens:
        return 0.0
    return len(cand_tokens & targ_tokens) / max(len(cand_tokens), len(targ_tokens))


def get_all_artists(base_or_client: str | LidarrClient, api_key: str | None = None) -> list[dict[str, Any]]:
    client = _coerce(base_or_client, api_key)
    cache_key = (client.base, client.api_key)
    if cache_key not in _ARTIST_CACHE:
        _ARTIST_CACHE[cache_key] = list(_request("GET", "/artist", client) or [])
    return _ARTIST_CACHE[cache_key]


def find_artist(name: str, base_or_client: str | LidarrClient, api_key: str | None = None) -> dict[str, Any] | None:
    artists = get_all_artists(base_or_client, api_key)
    ranked = sorted(
        ((_score(str(artist.get("artistName") or artist.get("name") or ""), name), artist) for artist in artists),
        key=lambda item: item[0],
        reverse=True,
    )
    if ranked and ranked[0][0] >= 0.72:
        return ranked[0][1]
    return None


def find_album(
    artist_id: int,
    album_title: str,
    base_or_client: str | LidarrClient,
    api_key: str | None = None,
) -> dict[str, Any] | None:
    albums = list(_request("GET", "/album", base_or_client, api_key, params={"artistId": artist_id}) or [])
    ranked = sorted(
        ((_score(str(album.get("title") or ""), album_title), album) for album in albums),
        key=lambda item: item[0],
        reverse=True,
    )
    if ranked and ranked[0][0] >= 0.72:
        return ranked[0][1]
    return None


def ensure_monitored_lossless(
    artist: dict[str, Any],
    album: dict[str, Any],
    base_or_client: str | LidarrClient,
    api_key: str | None = None,
) -> None:
    album_id = int(album["id"])
    desired_profile = int(album.get("qualityProfileId") or artist.get("qualityProfileId") or DEFAULT_QUALITY_PROFILE_ID)
    desired_profile = DEFAULT_QUALITY_PROFILE_ID if desired_profile != DEFAULT_QUALITY_PROFILE_ID else desired_profile
    if bool(album.get("monitored")) and int(album.get("qualityProfileId") or 0) == desired_profile:
        return

    fresh = _request("GET", f"/album/{album_id}", base_or_client, api_key)
    fresh["monitored"] = True
    fresh["qualityProfileId"] = desired_profile
    if DRY_RUN:
        logger.info("DRY_RUN Lidarr PUT /album/%s monitored=true qualityProfileId=%s", album_id, desired_profile)
        return
    _request("PUT", f"/album/{album_id}", base_or_client, api_key, json_body=fresh)


def delete_trackfile(
    trackfile_id: int,
    base_or_client: str | LidarrClient,
    api_key: str | None = None,
    delete_files: bool = False,
) -> Any:
    del delete_files
    params = {"deleteFiles": "false"}
    if DRY_RUN:
        logger.info("DRY_RUN Lidarr DELETE /trackfile/%s?deleteFiles=false", trackfile_id)
        return {"id": trackfile_id, "dryRun": True, "deleteFiles": False}
    return _request("DELETE", f"/trackfile/{int(trackfile_id)}", base_or_client, api_key, params=params)


def _records(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, dict):
        return list(data.get("records") or data.get("items") or [])
    if isinstance(data, list):
        return data
    return []


def _contains_album_id(value: Any, album_id: int) -> bool:
    if isinstance(value, dict):
        if int(value.get("albumId") or 0) == album_id:
            return True
        album = value.get("album")
        if isinstance(album, dict) and int(album.get("id") or 0) == album_id:
            return True
        return any(_contains_album_id(child, album_id) for child in value.values())
    if isinstance(value, list):
        return any(_contains_album_id(child, album_id) for child in value)
    return False


def album_inflight(album_id: int, base_or_client: str | LidarrClient, api_key: str | None = None) -> bool:
    album_id = int(album_id)
    queue = _request("GET", "/queue", base_or_client, api_key, params={"pageSize": 1000})
    if any(_contains_album_id(record, album_id) for record in _records(queue)):
        return True

    history = _request("GET", "/history", base_or_client, api_key, params={"pageSize": 100})
    active_events = {"grabbed", "downloadFolderImported", "trackFileImported"}
    cutoff = time.time() - 24 * 60 * 60
    for record in _records(history):
        if not _contains_album_id(record, album_id):
            continue
        event_type = str(record.get("eventType") or record.get("event") or "")
        date_text = str(record.get("date") or "")
        if event_type in active_events:
            return True
        if not date_text:
            continue
        try:
            parsed = time.mktime(time.strptime(date_text[:19], "%Y-%m-%dT%H:%M:%S"))
            if parsed >= cutoff:
                return True
        except Exception:
            continue
    return False


def trigger_album_search(album_id: int, base_or_client: str | LidarrClient, api_key: str | None = None) -> str:
    payload = {"name": "AlbumSearch", "albumIds": [int(album_id)]}
    if DRY_RUN:
        logger.info("DRY_RUN Lidarr POST /command %s", payload)
        return f"dry-run-album-search-{album_id}"
    data = _request("POST", "/command", base_or_client, api_key, json_body=payload)
    return str(data.get("id") or "")


def album_trackfiles(album_id: int, base_or_client: str | LidarrClient, api_key: str | None = None) -> list[dict[str, Any]]:
    return list(_request("GET", "/trackfile", base_or_client, api_key, params={"albumId": int(album_id)}) or [])


def album_track_paths(album_id: int, base_or_client: str | LidarrClient, api_key: str | None = None) -> list[str]:
    return [str(item.get("path") or "") for item in album_trackfiles(album_id, base_or_client, api_key) if item.get("path")]


def album_status(album_id: int, base_or_client: str | LidarrClient, api_key: str | None = None) -> dict[str, Any]:
    data = _request("GET", f"/album/{int(album_id)}", base_or_client, api_key)
    stats = data.get("statistics") or {}
    return {
        "trackFileCount": int(stats.get("trackFileCount") or data.get("trackFileCount") or 0),
        "trackCount": int(stats.get("trackCount") or data.get("trackCount") or 0),
        "monitored": bool(data.get("monitored")),
    }
