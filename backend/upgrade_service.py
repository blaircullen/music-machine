import asyncio
import logging
import os
import subprocess
import urllib.parse
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

SLSKD_URL = os.environ.get("SLSKD_URL", "http://slskd:5030")
SLSKD_API_KEY = os.environ.get("SLSKD_API_KEY", "plex-dedup-api-key")

_HEADERS = {"X-API-Key": SLSKD_API_KEY, "Content-Type": "application/json"}

# Timeout for HTTP calls to slskd (not search timeout — that is passed in the search body)
HTTP_TIMEOUT = 30


def _score_slskd_result(entry: dict) -> int:
    """Score a slskd search result file. Higher is better."""
    filename = (entry.get("filename") or "").lower()
    file_size = entry.get("size") or 0
    score = 0

    # Hi-res indicators in filename
    hi_res_markers = ["24bit", "24-bit", "24 bit", "[24-", "flac 24"]
    for marker in hi_res_markers:
        if marker in filename:
            score += 1000
            break

    # Sample rate hints
    for hint in ["96", "192", "88", "176"]:
        if hint in filename:
            score += 500
            break

    # File size as proxy for quality
    if file_size > 50_000_000:
        score += 100
    elif file_size > 20_000_000:
        score += 50

    # Prefer users with higher upload speed
    upload_speed = entry.get("uploadSpeed") or 0
    if upload_speed > 0:
        score += min(upload_speed // 100_000, 50)

    return score


def _classify_match_quality(entry: dict) -> str:
    """Classify a found file as hi_res or lossless."""
    filename = (entry.get("filename") or "").lower()
    hi_res_markers = ["24bit", "24-bit", "24 bit", "[24-", "flac 24"]
    sample_rate_hints = ["96khz", "192khz", "88khz", "176khz", "96 khz", "192 khz"]

    for marker in hi_res_markers + sample_rate_hints:
        if marker in filename:
            return "hi_res"

    file_size = entry.get("size") or 0
    if file_size > 80_000_000:
        return "hi_res"

    return "lossless"


async def search_for_flac(
    artist: str,
    album: str,
    title: str,
    timeout_s: int = 20,
) -> dict | None:
    """
    Search slskd for a FLAC version of the given track.
    Returns the best match dict or None.

    Return shape: {username, filename, file_size, match_quality, slskd_search_id}
    """
    query = f"{artist} {album}" if album else f"{artist} {title}"

    async with httpx.AsyncClient(headers=_HEADERS, timeout=HTTP_TIMEOUT) as client:
        # Start a search
        try:
            resp = await client.post(
                f"{SLSKD_URL}/api/v0/searches",
                json={
                    "searchText": query,
                    "fileType": "Audio",
                    "searchTimeout": timeout_s * 1000,
                },
            )
            resp.raise_for_status()
            search_data = resp.json()
        except Exception as e:
            logger.warning(f"slskd search initiation failed for '{query}': {e}")
            return None

        search_id = search_data.get("id")
        if not search_id:
            logger.warning(f"slskd returned no search ID for '{query}'")
            return None

        # Poll for results until the search ends or timeout
        elapsed = 0
        poll_interval = 3
        best_entry = None
        best_score = -1

        while elapsed < timeout_s + 5:
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

            try:
                resp = await client.get(
                    f"{SLSKD_URL}/api/v0/searches/{search_id}/responses"
                )
                resp.raise_for_status()
                responses = resp.json()
            except Exception as e:
                logger.debug(f"slskd poll error for search {search_id}: {e}")
                continue

            # Check if search is still running
            try:
                status_resp = await client.get(
                    f"{SLSKD_URL}/api/v0/searches/{search_id}"
                )
                if status_resp.status_code == 200:
                    status_data = status_resp.json()
                    search_state = status_data.get("state", "")
                    if search_state in ("Completed", "Cancelled", "TimedOut"):
                        # One final response fetch and break
                        try:
                            resp2 = await client.get(
                                f"{SLSKD_URL}/api/v0/searches/{search_id}/responses"
                            )
                            if resp2.status_code == 200:
                                responses = resp2.json()
                        except Exception:
                            pass
                        break
            except Exception:
                pass

            # Process responses: each response is a user's result set
            for user_response in (responses or []):
                username = user_response.get("username", "")
                upload_speed = user_response.get("uploadSpeed", 0)
                files = user_response.get("files") or []

                for file_entry in files:
                    filename = file_entry.get("filename") or ""
                    if not filename.lower().endswith(".flac"):
                        continue

                    enriched = {
                        **file_entry,
                        "username": username,
                        "uploadSpeed": upload_speed,
                    }
                    score = _score_slskd_result(enriched)
                    if score > best_score:
                        best_score = score
                        best_entry = enriched

    if not best_entry:
        return None

    return {
        "username": best_entry.get("username"),
        "filename": best_entry.get("filename"),
        "file_size": best_entry.get("size"),
        "match_quality": _classify_match_quality(best_entry),
        "slskd_search_id": search_id,
    }


async def download_file(
    username: str,
    filename: str,
    file_size: int | None = None,
) -> bool:
    """
    Initiate a download via slskd.
    POST /api/v0/transfers/downloads/{username}  body: [{"filename": ..., "size": ...}]
    Returns True on 201 success, False otherwise.
    """
    url = f"{SLSKD_URL}/api/v0/transfers/downloads/{username}"

    body = [{"filename": filename}]
    if file_size is not None:
        body[0]["size"] = file_size

    async with httpx.AsyncClient(headers=_HEADERS, timeout=HTTP_TIMEOUT) as client:
        try:
            resp = await client.post(url, json=body)
            if resp.status_code == 201:
                return True
            logger.warning(
                f"slskd download returned {resp.status_code} for {username}/{filename}: {resp.text[:200]}"
            )
            return False
        except Exception as e:
            logger.error(f"slskd download error for {username}/{filename}: {e}")
            return False


async def get_download_status(username: str, filename: str) -> dict | None:
    """
    Get download status for a specific file.
    Returns {state, bytes_transferred, size, local_filename} or None.

    slskd response shape: {username, directories: [{directory, files: [...]}]}
    State examples: "Queued", "InProgress", "Completed, Succeeded", "Completed, Errored"
    """
    async with httpx.AsyncClient(headers=_HEADERS, timeout=HTTP_TIMEOUT) as client:
        try:
            resp = await client.get(
                f"{SLSKD_URL}/api/v0/transfers/downloads/{username}"
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.debug(f"get_download_status error for {username}: {e}")
            return None

    target_basename = Path(filename).name.lower()
    # Response is {username, directories: [{directory, files: [...]}]}
    directories = data.get("directories") or [] if isinstance(data, dict) else (data or [])

    for group in directories:
        files = group.get("files") or [] if isinstance(group, dict) else []
        for f in files:
            f_filename = f.get("filename") or ""
            if Path(f_filename).name.lower() == target_basename or f_filename == filename:
                state = f.get("state", "")
                # Construct the local path: /downloads/{username}/{filename_forward_slashes}
                norm_filename = f_filename.replace("\\", "/")
                local_filename = f"/downloads/{username}/{norm_filename}"
                return {
                    "state": state,
                    "bytes_transferred": f.get("bytesTransferred", 0),
                    "size": f.get("size", 0),
                    "local_filename": local_filename,
                }

    return None


async def cancel_download(username: str, filename: str) -> None:
    """Cancel a pending or in-progress download."""
    # DELETE /api/v0/transfers/downloads/{username}/{id}  (requires transfer id, not filename)
    # Best-effort: find the transfer id first, then delete
    async with httpx.AsyncClient(headers=_HEADERS, timeout=HTTP_TIMEOUT) as client:
        try:
            resp = await client.get(f"{SLSKD_URL}/api/v0/transfers/downloads/{username}")
            if resp.status_code != 200:
                return
            transfers = resp.json()
            target_basename = Path(filename).name.lower()
            for group in (transfers or []):
                for f in (group.get("files") or []):
                    f_name = f.get("filename") or ""
                    if Path(f_name).name.lower() == target_basename:
                        transfer_id = f.get("id")
                        if transfer_id:
                            await client.delete(
                                f"{SLSKD_URL}/api/v0/transfers/downloads/{username}/{transfer_id}"
                            )
                        return
        except Exception as e:
            logger.debug(f"cancel_download error for {username}/{filename}: {e}")


def fetch_completed_file(local_filename: str, staging_root: str) -> str:
    """
    SCP a completed slskd download from BuyVM to the local staging directory.

    local_filename: path as reported by slskd (used to derive the basename).
    slskd strips path prefixes when saving — we locate the file on BuyVM by basename using find.
    Returns the local staging path where the file was placed.
    """
    buyvm_host = os.environ.get("BUYVM_HOST", "198.98.58.109")
    buyvm_port = os.environ.get("BUYVM_PORT", "65222")
    buyvm_user = os.environ.get("BUYVM_USER", "root")
    buyvm_password = os.environ.get("BUYVM_PASSWORD", "")

    basename = Path(local_filename).name
    dest_path = str(Path(staging_root) / basename)

    # Find the actual file on BuyVM (slskd strips path prefixes, so we search by name)
    find_cmd = [
        "sshpass", f"-p{buyvm_password}",
        "ssh",
        "-p", buyvm_port,
        "-o", "StrictHostKeyChecking=no",
        f"{buyvm_user}@{buyvm_host}",
        f"find /home/slskd/downloads -name '{basename}' -not -name '*_[0-9]*[0-9].flac' 2>/dev/null | head -1",
    ]
    find_result = subprocess.run(find_cmd, capture_output=True, text=True, timeout=30)
    remote_path = find_result.stdout.strip()

    if not remote_path:
        # Fallback: any file with this basename (may have timestamp suffix from duplicates)
        find_cmd2 = [
            "sshpass", f"-p{buyvm_password}",
            "ssh",
            "-p", buyvm_port,
            "-o", "StrictHostKeyChecking=no",
            f"{buyvm_user}@{buyvm_host}",
            f"find /home/slskd/downloads -name '{basename}*' 2>/dev/null | head -1",
        ]
        find_result2 = subprocess.run(find_cmd2, capture_output=True, text=True, timeout=30)
        remote_path = find_result2.stdout.strip()

    if not remote_path:
        raise FileNotFoundError(f"Could not locate {basename} in BuyVM /home/slskd/downloads/")

    scp_cmd = [
        "sshpass", f"-p{buyvm_password}",
        "scp",
        "-P", buyvm_port,
        "-o", "StrictHostKeyChecking=no",
        f"{buyvm_user}@{buyvm_host}:{remote_path}",
        dest_path,
    ]
    result = subprocess.run(scp_cmd, capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        raise RuntimeError(f"scp from BuyVM failed: {result.stderr.strip()}")

    logger.info(f"Fetched {remote_path} → {dest_path}")

    # Delete the file from BuyVM after successful transfer to save disk space
    rm_cmd = [
        "sshpass", f"-p{buyvm_password}",
        "ssh",
        "-p", buyvm_port,
        "-o", "StrictHostKeyChecking=no",
        f"{buyvm_user}@{buyvm_host}",
        f"rm -f '{remote_path}'",
    ]
    rm_result = subprocess.run(rm_cmd, capture_output=True, text=True, timeout=30)
    if rm_result.returncode == 0:
        logger.info(f"Deleted remote file: {remote_path}")
    else:
        logger.warning(f"Failed to delete remote file {remote_path}: {rm_result.stderr.strip()}")

    return dest_path


async def check_connected() -> bool:
    """Return True if slskd API is reachable and responding."""
    async with httpx.AsyncClient(headers=_HEADERS, timeout=5) as client:
        try:
            resp = await client.get(f"{SLSKD_URL}/api/v0/application")
            return resp.status_code == 200
        except Exception:
            return False
