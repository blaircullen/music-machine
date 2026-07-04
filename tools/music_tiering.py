#!/usr/bin/env python3
"""
Plex-star-rating-driven music tiering: volume3 (SSD, hot) -> volume2 (HDD, cold).

(Originally designed around Plex play-recency; switched to explicit star
ratings 2026-07-03 after a dry run showed near-continuous PoolPi/Plexamp
shuffle playback makes "last played" meaningless as a hot/cold signal —
virtually the whole library gets touched by rotation within any window.
Star ratings are explicit and deliberate instead.)

Runs FROM Beast (has Plex API + NFS-mounted view of volume3 for cheap stat
reads), but every filesystem MUTATION (copy, checksum, rename, symlink) is
dispatched as a shell command over SSH to the NAS itself, so every mutating
command operates on NAS-native paths (/volume3/..., /volume2/...) with no
cross-host path ambiguity.

Design: /private/tmp/.../scratchpad/music-tiering-design.md (v2, post-rushmore)
Code reviewed by GLM-5.2 + DeepSeek V4 Pro (2026-07-03); fixes applied inline,
see CHANGELOG at bottom of this docstring.

Safety model for replacing an album directory with a symlink (a directory
can't be atomically replaced by a symlink via a single rename() the way a
single file can, so this uses a park-then-swap sequence instead):
  1. copy album dir -> volume2 cold destination (original untouched)
  2. verify checksums, cold copy vs original (NUL-safe, full library scan)
  3. rename the ORIGINAL album dir sideways to <album>.parked-<ts> (still on
     volume3, still fully intact, instantaneous local rename)
  4. create the symlink at the original album path -> cold destination
  5. sanity-check the symlink resolves and at least one file is readable
  6. leave the parked original in place; a separate --cleanup-parked pass
     deletes parked dirs older than N days, so there's a full physical
     rollback available during the highest-risk window without needing to
     copy anything back from volume2.

CHANGELOG (post-review fixes):
- migrate_album now hard-requires the source to still be a real directory
  (not a symlink) before touching it — prevents a second/overlapping run
  from cp -a'ing a symlink over the cold copy and destroying the album
  (DeepSeek: data-loss showstopper).
- Checksum comparison is NUL-safe (find -print0 | md5sum -z) so filenames
  with spaces/newlines/unusual bytes can't corrupt the parse (GLM + DeepSeek:
  showstopper for a real music library).
- Manifest build no longer computes full checksums per candidate (that was
  hours of unnecessary SSH+hashing just to get a size) — build uses a cheap
  size-only stat pass; checksums only run during --execute, once per
  candidate actually being migrated.
- Multi-disc albums (Disc 1/, CD2/, etc.) are now merged into their parent
  release directory as one migration unit, instead of being classified and
  moved independently (GLM + DeepSeek: structurally guaranteed to misfire).
- Post-swap sanity check no longer shells out an unquoted `$(ls ...)` — uses
  a NUL-safe remote find instead (DeepSeek: crashes on filenames with spaces
  or empty albums, after the risky rename already happened).
- A stale cold-destination guard: if a previous run died mid-copy, the next
  attempt wipes the incomplete cold copy before retrying, instead of nesting
  into it (DeepSeek).
- A Beast-side flock prevents two --execute runs overlapping; a NAS-side
  free-space check aborts before starting a batch that won't fit.
- precondition_check is still a hard gate by default, but can be bypassed
  with --skip-precondition-check for a deliberate, informed override (the
  original hard-fail-always version would block legitimate runs for a user
  who just hasn't listened in the last 30 days).
"""
import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

# ---- Config ------------------------------------------------------------

MUSIC_MACHINE_ENV = "/home/olares/projects/music-machine/.env"
MUSIC_SECTION_ID = "5"

BEAST_MOUNT_PREFIX = "/mnt/nas/music"      # how Beast (and Plex) see the hot library
NAS_HOT_PREFIX = "/volume3/music"          # same directory, NAS-native path
NAS_COLD_PREFIX = "/volume2/music-cold"    # destination root, NAS-native path

NAS_SSH_HOST = "sunygxc@10.0.0.7"

STAR_HOT_THRESHOLD = 6   # Plex userRating is 0-10 in 2pt/star increments (2=1*,4=2*,6=3*,...);
                          # an album is "hot" (stays on volume3) if ANY track has >= 3 stars.
                          # Unstarred (no rating) or 1-2 star tracks count toward "cold" if no
                          # other track in the album clears the bar.

DISC_SUBDIR_RE = re.compile(r"^(disc|cd|d)\s*\d+$", re.IGNORECASE)

MANIFEST_DIR = Path(__file__).parent / "manifests"
RUN_LOCK_PATH = MANIFEST_DIR / ".run.lock"

# ---- Plex API helpers ---------------------------------------------------

def load_plex_env():
    """Pull PLEX_URL / PLEX_TOKEN from Music Machine's .env — never hardcode the token."""
    env = {}
    with open(MUSIC_MACHINE_ENV) as f:
        for line in f:
            line = line.strip()
            if line.startswith("PLEX_URL=") or line.startswith("PLEX_TOKEN="):
                k, v = line.split("=", 1)
                env[k] = v
    if "PLEX_URL" not in env or "PLEX_TOKEN" not in env:
        sys.exit("PLEX_URL/PLEX_TOKEN not found in music-machine .env")
    return env["PLEX_URL"], env["PLEX_TOKEN"]


def plex_get(url, token, path, params=None):
    params = dict(params or {})
    params["X-Plex-Token"] = token
    full = f"{url}{path}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(full, timeout=30) as r:
        return ET.fromstring(r.read())


def fetch_all_tracks(url, token, page_size=500):
    """Paginate the whole music library section. Handles multi-part media and
    dedups by file path in case the server ignores container-size paging."""
    seen_files = set()
    tracks = []
    start = 0
    stall_guard = 0
    while True:
        root = plex_get(
            url, token, f"/library/sections/{MUSIC_SECTION_ID}/all",
            {"type": "10", "X-Plex-Container-Start": start, "X-Plex-Container-Size": page_size},
        )
        batch = root.findall("Track")
        if not batch:
            break
        new_in_batch = 0
        for t in batch:
            rating = t.get("userRating")
            added_at = t.get("addedAt")
            album_key = t.get("parentRatingKey")
            for part in t.findall("Media/Part"):
                f = part.get("file")
                if f and f not in seen_files:
                    seen_files.add(f)
                    new_in_batch += 1
                    tracks.append({
                        "file": f,
                        "userRating": float(rating) if rating else 0.0,
                        "addedAt": int(added_at) if added_at else None,
                        # Plex's OWN album grouping (parentRatingKey), not a directory guess —
                        # essential because this library isn't uniformly organized: some artists
                        # have clean one-album-per-folder layouts, others dump many different
                        # albums' tracks flat in one artist folder (verified 2026-07-03, e.g.
                        # /volume3/music/Neil Young/ has 423 tracks spanning several real albums
                        # with no album subdirectory at all).
                        "album_key": album_key,
                        "album_title": t.get("parentTitle"),
                        "artist_title": t.get("grandparentTitle"),
                    })
        start += page_size
        total = int(root.get("totalSize", start))
        if start >= total:
            break
        if new_in_batch == 0:
            # server ignored container-size paging and is repeating itself — bail rather than loop forever
            stall_guard += 1
            if stall_guard > 2:
                print("[warn] pagination stalled (no new tracks in last pages), stopping early", file=sys.stderr)
                break
        else:
            stall_guard = 0
    return tracks


def precondition_check(url, token, skip=False):
    """Go/no-go gate: does this library actually have meaningful star-rating data?
    (Switched from Plex play-recency to explicit star ratings 2026-07-03 — with
    near-continuous shuffle playback via PoolPi/Plexamp, 'last played' couldn't
    distinguish loved music from background rotation; almost nothing qualified
    as 'played but stale' even at a 6-month window. Star ratings are an explicit,
    deliberate signal instead.)"""
    root = plex_get(
        url, token, f"/library/sections/{MUSIC_SECTION_ID}/all",
        {"type": "10", "userRating>>": "0", "X-Plex-Container-Start": 0, "X-Plex-Container-Size": 0},
    )
    rated_count = int(root.get("totalSize", "0"))
    print(f"[precondition] {rated_count} tracks have a star rating set")
    ok = rated_count >= 20
    if not ok and not skip:
        sys.exit(f"Precondition check failed: only {rated_count} rated tracks found. With this few, "
                 "'unstarred -> cold' would classify almost the entire library as cold. Rate more "
                 "tracks first, or re-run with --skip-precondition-check if that's genuinely intended.")


# ---- Classification -------------------------------------------------------

def canonical_album_dir(file_path):
    """Album unit = immediate parent directory, UNLESS that directory looks like a
    disc subfolder (Disc 1, CD2, D1, ...) of a multi-disc release, in which case
    the grandparent is the unit — keeps a multi-disc album migrating as one piece
    instead of splitting hot/cold across discs."""
    parent = Path(file_path).parent
    if DISC_SUBDIR_RE.match(parent.name):
        return str(parent.parent)
    return str(parent)


def build_album_groups(tracks):
    """Group by Plex's own album identity (album_key), not by directory — a
    directory guess breaks the moment one folder holds more than one real
    album's tracks (confirmed present in this library). Also builds
    dir_to_keys, mapping each canonical directory to the set of album_keys
    whose tracks live there, used to decide per-album whether its directory
    is safe to move wholesale or whether only its specific files should move.
    """
    albums = defaultdict(lambda: {
        "tracks": [], "max_rating": 0.0, "min_added": None,
        "album_title": None, "artist_title": None,
    })
    dir_to_keys = defaultdict(set)
    for t in tracks:
        key = t["album_key"] or f"__no_album_key__:{canonical_album_dir(t['file'])}"
        a = albums[key]
        a["tracks"].append(t)
        a["max_rating"] = max(a["max_rating"], t["userRating"])
        a["album_title"] = a["album_title"] or t["album_title"]
        a["artist_title"] = a["artist_title"] or t["artist_title"]
        if t["addedAt"]:
            a["min_added"] = min(a["min_added"] or t["addedAt"], t["addedAt"])
        dir_to_keys[canonical_album_dir(t["file"])].add(key)
    return albums, dir_to_keys


def classify_cold(albums):
    """An album is cold if NO track in it clears the 3-star bar — i.e. every
    track is unstarred or rated 1-2 stars. One highly-rated track protects the
    whole album from migration."""
    cold = []
    for album_key, info in albums.items():
        if info["max_rating"] < STAR_HOT_THRESHOLD:
            cold.append((album_key, info))
    return cold


def decide_migration_unit(info, dir_to_keys, this_key):
    """Whole-directory move if this album's tracks live in exactly one directory
    AND no OTHER album's tracks live anywhere in that directory's subtree —
    safe, keeps companion files (art, .cue, etc.) together. Otherwise (flat
    multi-album dump, tracks scattered across dirs, or a nested subdirectory
    belonging to a different album) fall back to moving this album's specific
    files individually, leaving any sibling files belonging to OTHER albums
    untouched.

    The subtree check (not just the immediate directory) matters because
    cp -a is recursive: confirmed 2026-07-03 that checking only the immediate
    directory let a dir-mode move ("Handel's Messiah Complete", tracks placed
    directly in /volume3/music/London Philharmonic Orchestra/) silently
    scoop up unrelated subdirectories nested beneath it (The 99 Most Essential
    Classical Pieces in Movies/, belonging to a different album entirely,
    "The 50 Greatest Pieces of Classical Music"), corrupting that other
    album's later per-file migration. 3 files were affected library-wide;
    recovered from the parked original. See tools/manifests/ + git history
    for the incident writeup.
    """
    dirs_used = {canonical_album_dir(t["file"]) for t in info["tracks"]}
    if len(dirs_used) == 1:
        d = next(iter(dirs_used))
        nested_other_keys = {
            key for other_dir, keys in dir_to_keys.items()
            for key in keys
            if key != this_key and (other_dir == d or other_dir.startswith(d + "/"))
        }
        if not nested_other_keys:
            return {"kind": "dir", "beast_paths": [d]}
    return {"kind": "files", "beast_paths": sorted({t["file"] for t in info["tracks"]})}


def _under_root(path, root):
    """Boundary-aware containment check: path must equal root or be root + '/...'
    after normalization, rejecting prefix-collisions ('/mnt/nas/musicXYZ') and
    '..' traversal. Used both when deriving NAS paths and again defensively at
    execute time, in case a stale/hand-edited manifest is passed via --manifest."""
    norm_root = os.path.normpath(root)
    norm_path = os.path.normpath(path)
    return norm_path == norm_root or norm_path.startswith(norm_root + os.sep)


def beast_to_nas_path(beast_album_dir, prefix_root):
    """Map a Beast-mounted Plex path to the NAS-native path under a given root."""
    if not _under_root(beast_album_dir, BEAST_MOUNT_PREFIX):
        raise ValueError(f"unexpected path outside mount prefix: {beast_album_dir}")
    rel = os.path.normpath(beast_album_dir)[len(os.path.normpath(BEAST_MOUNT_PREFIX)):].lstrip("/")
    result = str(Path(prefix_root) / rel)
    if not _under_root(result, prefix_root):
        raise ValueError(f"computed path escapes intended root: {result}")
    return result


def already_migrated(nas_hot_path):
    """Idempotency check: is this album dir already a symlink from a prior run?
    Single-path version — kept for use in migrate_album's execute-time re-check."""
    r = ssh_run(f"test -L {shq(nas_hot_path)}", check=False)
    return r.returncode == 0


def already_migrated_batch(paths):
    """Batched idempotency check: one ssh round-trip for the whole candidate set
    instead of one per album (same rationale as nas_dir_sizes_batch)."""
    if not paths:
        return set()
    remote_script = (
        "while IFS= read -r -d '' p; do "
        "test -L \"$p\" && printf '%s\\0' \"$p\"; "
        "done; true"
    )
    proc = subprocess.run(
        ["ssh", NAS_SSH_HOST, remote_script],
        input="\0".join(paths) + "\0",
        capture_output=True, text=True,
    )
    return {p for p in proc.stdout.split("\0") if p}


# ---- SSH / NAS execution ---------------------------------------------------

def shq(s):
    return "'" + s.replace("'", "'\\''") + "'"


def ssh_run(remote_cmd, check=True, capture=True):
    cmd = ["ssh", NAS_SSH_HOST, remote_cmd]
    r = subprocess.run(cmd, capture_output=capture, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"NAS command failed ({r.returncode}): {remote_cmd}\nstderr: {r.stderr}")
    return r


def nas_dir_size(path):
    """Cheap size-only pass for a single directory. No checksumming — that's
    reserved for migrate_album (execute time)."""
    r = ssh_run(
        f"find {shq(path)} -type f -printf '%s\\n' 2>/dev/null | awk '{{s+=$1}} END{{print s+0}}'"
    )
    return int(r.stdout.strip() or "0")


def nas_dir_sizes_batch(paths):
    """Size every candidate in ONE ssh round-trip instead of one-per-album — with
    a star-rating-based cold set likely covering most of the library (thousands
    of albums, not the small hand-picked set play-recency gave us), one SSH
    handshake per album would be the exact 'thousands of round trips' cost GLM
    and DeepSeek flagged against the size-per-candidate approach. Paths are
    piped over stdin (NUL-separated) rather than passed as argv to avoid
    ARG_MAX limits at this scale."""
    if not paths:
        return {}
    remote_script = (
        "while IFS= read -r -d '' p; do "
        "sz=$(find \"$p\" -type f -printf '%s\\n' 2>/dev/null | awk '{s+=$1} END{print s+0}'); "
        "printf '%s\\t%s\\0' \"$sz\" \"$p\"; "
        "done"
    )
    proc = subprocess.run(
        ["ssh", NAS_SSH_HOST, remote_script],
        input="\0".join(paths) + "\0",
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"batch sizing failed: {proc.stderr}")
    sizes = {}
    for record in proc.stdout.split("\0"):
        if not record:
            continue
        sz, _, path = record.partition("\t")
        sizes[path] = int(sz)
    return sizes


def nas_dir_md5_multiset(path):
    """Full checksum set for every regular file under path, NUL-safe so filenames
    with spaces/newlines/odd bytes can't corrupt the parse. Only used at migrate
    time (once per album actually being moved), not during manifest build."""
    r = ssh_run(
        f"cd {shq(path)} && find . -type f -print0 | xargs -0 -r md5sum -z 2>/dev/null"
    )
    raw = r.stdout
    md5s = {}
    for record in raw.split("\0"):
        if not record:
            continue
        # md5sum -z format: "<32-hex-digest>  <relpath>" (no trailing newline, NUL-terminated)
        digest, relpath = record[:32], record[34:]
        md5s[relpath.lstrip("./")] = digest
    return md5s


def nas_file_md5(path):
    """Checksum of a single file — used by the per-file migration path (files-mode
    candidates), where nas_dir_md5_multiset's whole-directory scan doesn't apply."""
    r = ssh_run(f"md5sum {shq(path)} 2>/dev/null")
    return r.stdout.split()[0] if r.stdout.strip() else None


def nas_free_bytes(path):
    r = ssh_run(f"df --output=avail -B1 {shq(path)} | tail -1")
    return int(r.stdout.strip())


# ---- Active-session guard ---------------------------------------------------

def currently_playing_nas_paths(url, token):
    """Returns (playing_dirs, playing_files) as NAS-native paths — dirs for the
    whole-directory migration mode, individual files for the per-file mode."""
    root = plex_get(url, token, "/status/sessions", {})
    playing_dirs, playing_files = set(), set()
    for v in root.findall(".//Part"):
        f = v.get("file")
        if f and f.startswith(BEAST_MOUNT_PREFIX):
            playing_dirs.add(beast_to_nas_path(canonical_album_dir(f), NAS_HOT_PREFIX))
            playing_files.add(beast_to_nas_path(f, NAS_HOT_PREFIX))
    return playing_dirs, playing_files


# ---- Manifest build (dry run) ---------------------------------------------------

def build_manifest(url, token, target_free_gb):
    tracks = fetch_all_tracks(url, token)
    albums, dir_to_keys = build_album_groups(tracks)
    cold = classify_cold(albums)

    pre_candidates = []
    for album_key, info in cold:
        unit = decide_migration_unit(info, dir_to_keys, album_key)
        try:
            nas_hot_paths = [beast_to_nas_path(p, NAS_HOT_PREFIX) for p in unit["beast_paths"]]
            nas_cold_paths = [beast_to_nas_path(p, NAS_COLD_PREFIX) for p in unit["beast_paths"]]
        except ValueError:
            continue
        pre_candidates.append((album_key, info, unit["kind"], nas_hot_paths, nas_cold_paths))

    all_paths = [p for _, _, _, hot_paths, _ in pre_candidates for p in hot_paths]
    already = already_migrated_batch(all_paths)

    candidates = []
    for album_key, info, kind, nas_hot_paths, nas_cold_paths in pre_candidates:
        # fully migrated already (every path is a symlink) -> skip entirely; a
        # PARTIALLY migrated files-mode candidate is kept, migrate_album skips
        # the already-done files and only processes what's left.
        if all(p in already for p in nas_hot_paths):
            continue
        candidates.append({
            "album_key": album_key,
            "album_title": info["album_title"],
            "artist_title": info["artist_title"],
            "kind": kind,
            "nas_hot_paths": nas_hot_paths,
            "nas_cold_paths": nas_cold_paths,
            "max_rating": info["max_rating"],
            "min_added": info["min_added"],
            "track_count": len(info["tracks"]),
        })

    # every candidate here already failed the star-rating bar equally (binary, not
    # graduated), so there's no "coldest first" tiebreak — size candidates up front
    # and take biggest-first, so a target-free-space run reclaims space in the
    # fewest moves rather than nibbling at small singles first. Batched into one
    # SSH round-trip since this set is likely most of the library now, not a
    # small hand-picked one. (nas_dir_sizes_batch's `find <path> -type f` works
    # fine on an individual file path too, so this covers both dir- and
    # files-mode candidates uniformly.)
    all_hot_paths = [p for c in candidates for p in c["nas_hot_paths"]]
    sizes = nas_dir_sizes_batch(all_hot_paths)
    for c in candidates:
        c["size_bytes"] = sum(sizes.get(p, 0) for p in c["nas_hot_paths"])
    candidates.sort(key=lambda c: c["size_bytes"], reverse=True)

    target_bytes = target_free_gb * (1024 ** 3) if target_free_gb else None
    running_total = 0
    selected = []
    for c in candidates:
        if target_bytes is not None and running_total >= target_bytes:
            break
        running_total += c["size_bytes"]
        selected.append(c)

    MANIFEST_DIR.mkdir(exist_ok=True)
    manifest_path = MANIFEST_DIR / f"candidates-{int(time.time())}.json"
    manifest_path.write_text(json.dumps({
        "generated_at": time.time(),
        "total_cold_albums_seen": len(candidates),
        "selected_count": len(selected),
        "selected_total_bytes": running_total,
        "target_free_gb": target_free_gb,
        "candidates": selected,
    }, indent=2))
    print(f"[manifest] {len(selected)}/{len(candidates)} cold albums selected, "
          f"{running_total / (1024**3):.1f} GB, written to {manifest_path}")
    return manifest_path


# ---- Migration (execute) ---------------------------------------------------

def _migrate_one_dir(url, token, hot, cold, playing_dirs):
    if hot in playing_dirs:
        print(f"[skip] currently playing: {hot}")
        return "skipped-playing"

    is_dir = ssh_run(f"test -d {shq(hot)} -a ! -L {shq(hot)}", check=False).returncode == 0
    if not is_dir:
        print(f"[skip] not a plain directory (already migrated or missing): {hot}")
        return "skipped-not-a-dir"

    # stale cold copy from a previously-aborted run: wipe rather than nest into it
    if ssh_run(f"test -e {shq(cold)}", check=False).returncode == 0:
        print(f"[warn] stale cold destination found, removing before retry: {cold}")
        ssh_run(f"rm -rf {shq(cold)}")

    ssh_run(f"mkdir -p {shq(str(Path(cold).parent))}")
    ssh_run(f"cp -a {shq(hot)} {shq(cold)}")

    hot_md5 = nas_dir_md5_multiset(hot)
    cold_md5 = nas_dir_md5_multiset(cold)
    if hot_md5 != cold_md5:
        ssh_run(f"rm -rf {shq(cold)}", check=False)
        raise RuntimeError(f"checksum mismatch after copy, aborting this album: {hot}")

    # re-check right before the swap — copy can take a while for a big directory
    if hot in _dirs_playing_now(url, token):
        print(f"[skip] became active during copy, leaving original untouched: {hot}")
        ssh_run(f"rm -rf {shq(cold)}", check=False)
        return "skipped-playing"

    parked = f"{hot}.parked-{int(time.time())}"
    ssh_run(f"mv {shq(hot)} {shq(parked)}")
    ssh_run(f"ln -s {shq(cold)} {shq(hot)}")

    sanity = ssh_run(f"find -L {shq(hot)} -maxdepth 1 -type f -print -quit", check=False)
    if sanity.returncode != 0 or not sanity.stdout.strip():
        ssh_run(f"rm -f {shq(hot)}", check=False)
        ssh_run(f"mv {shq(parked)} {shq(hot)}", check=False)
        raise RuntimeError(f"post-swap sanity check failed, restored original: {hot}")

    print(f"[migrated] {hot} -> {cold} ({len(cold_md5)} files), original parked at {parked}")
    return "migrated"


def _dirs_playing_now(url, token):
    dirs, _ = currently_playing_nas_paths(url, token)
    return dirs


def _migrate_one_file(url, token, hot, cold, playing_files):
    if hot in playing_files:
        print(f"[skip] currently playing: {hot}")
        return "skipped-playing"

    # Hard guard, file-mode equivalent of the dir-mode symlink check: if it's
    # already a symlink, either a prior run finished this exact file or an
    # overlapping run got here first — don't touch it either way.
    is_file = ssh_run(f"test -f {shq(hot)} -a ! -L {shq(hot)}", check=False).returncode == 0
    if not is_file:
        return "skipped-not-a-file"

    if ssh_run(f"test -e {shq(cold)}", check=False).returncode == 0:
        print(f"[warn] stale cold destination found, removing before retry: {cold}")
        ssh_run(f"rm -f {shq(cold)}")

    ssh_run(f"mkdir -p {shq(str(Path(cold).parent))}")
    ssh_run(f"cp -a {shq(hot)} {shq(cold)}")

    hot_md5, cold_md5 = nas_file_md5(hot), nas_file_md5(cold)
    if hot_md5 is None or hot_md5 != cold_md5:
        ssh_run(f"rm -f {shq(cold)}", check=False)
        raise RuntimeError(f"checksum mismatch after copy, aborting this file: {hot}")

    _, playing_files_now = currently_playing_nas_paths(url, token)
    if hot in playing_files_now:
        print(f"[skip] became active during copy, leaving original untouched: {hot}")
        ssh_run(f"rm -f {shq(cold)}", check=False)
        return "skipped-playing"

    parked = f"{hot}.parked-{int(time.time())}"
    ssh_run(f"mv {shq(hot)} {shq(parked)}")
    ssh_run(f"ln -s {shq(cold)} {shq(hot)}")

    sanity = ssh_run(f"test -f {shq(hot)}", check=False)
    if sanity.returncode != 0:
        ssh_run(f"rm -f {shq(hot)}", check=False)
        ssh_run(f"mv {shq(parked)} {shq(hot)}", check=False)
        raise RuntimeError(f"post-swap sanity check failed, restored original: {hot}")

    print(f"[migrated] {hot} -> {cold}, original parked at {parked}")
    return "migrated"


def migrate_album(url, token, candidate):
    """Dispatches to whole-directory or per-file migration depending on how
    decide_migration_unit classified this album at manifest-build time. Files
    mode can partially succeed (some files migrated, others skipped/errored)
    since each file is an independent atomic unit — that's fine and expected
    for a flat multi-album directory."""
    hot_paths, cold_paths = candidate["nas_hot_paths"], candidate["nas_cold_paths"]

    # Defense in depth: re-validate containment even from a --manifest file on
    # disk (stale or hand-edited), since these paths feed rm -rf/mv/ln -s.
    for p in hot_paths:
        if not _under_root(p, NAS_HOT_PREFIX):
            raise ValueError(f"refusing to touch out-of-bounds hot path: {p}")
    for p in cold_paths:
        if not _under_root(p, NAS_COLD_PREFIX):
            raise ValueError(f"refusing to touch out-of-bounds cold path: {p}")

    playing_dirs, playing_files = currently_playing_nas_paths(url, token)

    if candidate["kind"] == "dir":
        return _migrate_one_dir(url, token, hot_paths[0], cold_paths[0], playing_dirs)

    # files mode: migrate each file independently, aggregate outcomes
    outcomes = defaultdict(int)
    for hot, cold in zip(hot_paths, cold_paths):
        try:
            outcomes[_migrate_one_file(url, token, hot, cold, playing_files)] += 1
        except Exception as e:
            print(f"[error] {hot}: {e}")
            outcomes["error"] += 1
    print(f"[files-album] {candidate.get('artist_title')} / {candidate.get('album_title')}: {dict(outcomes)}")
    if outcomes.get("migrated", 0) > 0:
        return "migrated"
    if outcomes.get("error", 0) > 0:
        return "error"
    return "skipped-not-a-file" if outcomes.get("skipped-not-a-file") else "skipped-playing"


PARKED_SUFFIX_RE = re.compile(r"^(?P<original>.+)\.parked-(?P<ts>\d+)$")


def cleanup_parked(older_than_days):
    """Delete parked originals (both whole-directory and individual-file mode)
    older than N days.

    Two fixes over the first cut (Codex finding): mv/rename preserves the
    original mtime, so `find -mtime` measures how old the album's *content*
    is — which for a cold, unplayed album is already old on day one, meaning
    it could get deleted immediately instead of after a grace period. Age is
    now parsed from the `.parked-<epoch>` timestamp embedded in the name
    instead. Also: a parked entry is only deleted once its corresponding
    un-parked path is confirmed to be a symlink resolving into NAS_COLD_PREFIX
    (i.e. the migration actually completed) — otherwise it's left alone and
    flagged for manual review.
    """
    out = ssh_run(f"find {shq(NAS_HOT_PREFIX)} \\( -type d -o -type f \\) -name '*.parked-*'").stdout
    now = time.time()
    candidates = [d for d in out.strip().splitlines() if d and _under_root(d, NAS_HOT_PREFIX)]
    print(f"[cleanup] {len(candidates)} parked entries found")
    for d in candidates:
        m = PARKED_SUFFIX_RE.match(d)
        if not m:
            print(f"  [skip] unrecognized parked-entry name format: {d}")
            continue
        age_days = (now - int(m.group("ts"))) / 86400
        if age_days < older_than_days:
            continue
        hot = m.group("original")
        readlink = ssh_run(f"readlink -f {shq(hot)}", check=False)
        is_migrated_symlink = (
            ssh_run(f"test -L {shq(hot)}", check=False).returncode == 0
            and readlink.returncode == 0
            and _under_root(readlink.stdout.strip(), NAS_COLD_PREFIX)
        )
        if not is_migrated_symlink:
            print(f"  [skip] {d}: corresponding hot path isn't a valid migrated symlink — leaving for manual review")
            continue
        ssh_run(f"rm -rf {shq(d)}")
        print(f"  removed {d} (age {age_days:.1f}d)")


# ---- CLI ---------------------------------------------------------------

def acquire_run_lock():
    MANIFEST_DIR.mkdir(exist_ok=True)
    lock_fh = open(RUN_LOCK_PATH, "w")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit("Another --execute run appears to be in progress (lock held). Aborting.")
    return lock_fh  # keep a reference alive for the process lifetime


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--target-free-gb", type=float, default=1200, help="stop selecting candidates once this much would be freed")
    ap.add_argument("--execute", action="store_true", help="actually migrate (default: dry-run manifest only)")
    ap.add_argument("--canary", action="store_true", help="only migrate the single coldest candidate, then stop")
    ap.add_argument("--manifest", type=str, help="re-use an existing manifest instead of rebuilding")
    ap.add_argument("--cleanup-parked", type=int, metavar="DAYS", help="delete parked originals older than DAYS and exit")
    ap.add_argument("--skip-precondition-check", action="store_true", help="bypass the star-rating-data sanity gate")
    args = ap.parse_args()

    url, token = load_plex_env()

    if args.cleanup_parked is not None:
        cleanup_parked(args.cleanup_parked)
        return

    precondition_check(url, token, skip=args.skip_precondition_check)

    if args.manifest:
        manifest_path = Path(args.manifest)
    else:
        manifest_path = build_manifest(url, token, args.target_free_gb)

    if not args.execute:
        print("[dry-run] stopping here. Re-run with --execute (and --canary for the first real pass) to migrate.")
        return

    lock_fh = acquire_run_lock()  # noqa: F841 — held for process lifetime, released on exit

    data = json.loads(manifest_path.read_text())
    candidates = data["candidates"]
    if args.canary:
        candidates = candidates[:1]
        print("[canary] migrating exactly one album")

    ssh_run(f"mkdir -p {shq(NAS_COLD_PREFIX)}")  # df needs the path to exist; harmless if already there
    free_bytes = nas_free_bytes(NAS_COLD_PREFIX)
    needed_bytes = sum(c["size_bytes"] for c in candidates)
    if needed_bytes > free_bytes:
        sys.exit(f"Not enough free space on volume2 for this batch: need {needed_bytes/1e9:.1f}GB, "
                 f"have {free_bytes/1e9:.1f}GB free. Reduce --target-free-gb or free up volume2 first.")

    results = defaultdict(int)
    for c in candidates:
        try:
            outcome = migrate_album(url, token, c)
        except Exception as e:
            print(f"[error] {c['nas_hot_paths']}: {e}")
            outcome = "error"
        results[outcome] += 1

    print(f"[done] {dict(results)}")


if __name__ == "__main__":
    main()
