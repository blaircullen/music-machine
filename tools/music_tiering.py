#!/usr/bin/env python3
"""
Plex-play-data-driven music tiering: volume3 (SSD, hot) -> volume2 (HDD, cold).

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

COLD_MONTHS_NEVER_PLAYED = 12   # viewCount==0 and added longer ago than this -> cold
COLD_MONTHS_SINCE_PLAYED = 6    # last played longer ago than this -> cold

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
            view_count = int(t.get("viewCount", "0"))
            last_viewed = t.get("lastViewedAt")
            added_at = t.get("addedAt")
            for part in t.findall("Media/Part"):
                f = part.get("file")
                if f and f not in seen_files:
                    seen_files.add(f)
                    new_in_batch += 1
                    tracks.append({
                        "file": f,
                        "viewCount": view_count,
                        "lastViewedAt": int(last_viewed) if last_viewed else None,
                        "addedAt": int(added_at) if added_at else None,
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
    """Go/no-go gate: is Plex's play data plausible at all? (design v2 §Precondition gate)"""
    root = plex_get(
        url, token, f"/library/sections/{MUSIC_SECTION_ID}/all",
        {"type": "10", "sort": "viewCount:desc", "X-Plex-Container-Start": 0, "X-Plex-Container-Size": 20},
    )
    top = root.findall("Track")
    view_counts = [int(t.get("viewCount", "0")) for t in top]
    last_viewed = [int(t.get("lastViewedAt")) for t in top if t.get("lastViewedAt")]
    now = time.time()
    recent_30d = [ts for ts in last_viewed if (now - ts) < 30 * 86400]
    ok = bool(view_counts) and max(view_counts) > 0 and len(recent_30d) >= 1
    print(f"[precondition] top-20 by viewCount: max={max(view_counts) if view_counts else 0}, "
          f"recently-played(<=30d)={len(recent_30d)}/20 -> {'PASS' if ok else 'FAIL'}")
    if not ok and not skip:
        sys.exit("Precondition check failed: Plex play data looks empty/stale (no plays in the "
                 "last 30 days among the 20 most-played tracks). This could mean Plex isn't the "
                 "primary playback client, OR just that nobody's listened recently — re-run with "
                 "--skip-precondition-check if you're confident the data is still meaningful.")


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


def group_by_album(tracks):
    albums = defaultdict(lambda: {"tracks": [], "max_last_viewed": None, "min_added": None, "any_played": False})
    for t in tracks:
        a = albums[canonical_album_dir(t["file"])]
        a["tracks"].append(t)
        if t["viewCount"] > 0:
            a["any_played"] = True
        if t["lastViewedAt"]:
            a["max_last_viewed"] = max(a["max_last_viewed"] or 0, t["lastViewedAt"])
        if t["addedAt"]:
            a["min_added"] = min(a["min_added"] or t["addedAt"], t["addedAt"])
    return albums


def classify_cold(albums):
    now = time.time()
    never_played_cutoff = now - COLD_MONTHS_NEVER_PLAYED * 30 * 86400
    since_played_cutoff = now - COLD_MONTHS_SINCE_PLAYED * 30 * 86400
    cold = []
    for album_dir, info in albums.items():
        if not info["any_played"]:
            if info["min_added"] and info["min_added"] < never_played_cutoff:
                cold.append((album_dir, info))
        else:
            if info["max_last_viewed"] and info["max_last_viewed"] < since_played_cutoff:
                cold.append((album_dir, info))
    return cold


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
    """Idempotency check: is this album dir already a symlink from a prior run?"""
    r = ssh_run(f"test -L {shq(nas_hot_path)}", check=False)
    return r.returncode == 0


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
    """Cheap size-only pass — used during manifest build for every candidate.
    No checksumming here; that's reserved for migrate_album (execute time)."""
    r = ssh_run(
        f"find {shq(path)} -type f -printf '%s\\n' 2>/dev/null | awk '{{s+=$1}} END{{print s+0}}'"
    )
    return int(r.stdout.strip() or "0")


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


def nas_free_bytes(path):
    r = ssh_run(f"df --output=avail -B1 {shq(path)} | tail -1")
    return int(r.stdout.strip())


# ---- Active-session guard ---------------------------------------------------

def currently_playing_nas_paths(url, token):
    root = plex_get(url, token, "/status/sessions", {})
    playing = set()
    for v in root.findall(".//Part"):
        f = v.get("file")
        if f and f.startswith(BEAST_MOUNT_PREFIX):
            playing.add(beast_to_nas_path(canonical_album_dir(f), NAS_HOT_PREFIX))
    return playing


# ---- Manifest build (dry run) ---------------------------------------------------

def build_manifest(url, token, target_free_gb):
    tracks = fetch_all_tracks(url, token)
    albums = group_by_album(tracks)
    cold = classify_cold(albums)

    candidates = []
    for album_dir, info in cold:
        try:
            nas_hot = beast_to_nas_path(album_dir, NAS_HOT_PREFIX)
        except ValueError:
            continue
        if already_migrated(nas_hot):
            continue
        candidates.append({
            "beast_path": album_dir,
            "nas_hot_path": nas_hot,
            "nas_cold_path": beast_to_nas_path(album_dir, NAS_COLD_PREFIX),
            "any_played": info["any_played"],
            "max_last_viewed": info["max_last_viewed"],
            "min_added": info["min_added"],
            "track_count": len(info["tracks"]),
        })

    # coldest first: never-played oldest-added first, then longest-since-played
    candidates.sort(key=lambda c: (c["any_played"], -(c["min_added"] or 0), (c["max_last_viewed"] or 0)))

    target_bytes = target_free_gb * (1024 ** 3) if target_free_gb else None
    running_total = 0
    selected = []
    for c in candidates:
        if target_bytes is not None and running_total >= target_bytes:
            break
        c["size_bytes"] = nas_dir_size(c["nas_hot_path"])
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

def migrate_album(url, token, candidate):
    hot, cold = candidate["nas_hot_path"], candidate["nas_cold_path"]

    # Defense in depth: re-validate containment even if this candidate came from
    # a --manifest file on disk (stale or hand-edited), since hot/cold paths are
    # about to be used in rm -rf / mv / ln -s. (Codex finding: a bad manifest
    # entry could otherwise mutate outside the intended trees.)
    if not _under_root(hot, NAS_HOT_PREFIX) or not _under_root(cold, NAS_COLD_PREFIX):
        raise ValueError(f"refusing to touch out-of-bounds path: hot={hot} cold={cold}")

    # Hard guard: source must still be a real directory. If it's already a
    # symlink (prior run migrated it, or an overlapping run got here first),
    # do NOT touch it — cp -a on a symlink would copy garbage over the cold
    # data and destroy the album (the DeepSeek data-loss finding).
    is_dir = ssh_run(f"test -d {shq(hot)} -a ! -L {shq(hot)}", check=False).returncode == 0
    if not is_dir:
        print(f"[skip] not a plain directory (already migrated or missing): {hot}")
        return "skipped-not-a-dir"

    # re-check active sessions immediately before touching this specific album
    if hot in currently_playing_nas_paths(url, token):
        print(f"[skip] currently playing: {hot}")
        return "skipped-playing"

    # stale cold copy from a previously-aborted run: wipe rather than nest into it
    cold_exists = ssh_run(f"test -e {shq(cold)}", check=False).returncode == 0
    if cold_exists:
        print(f"[warn] stale cold destination found, removing before retry: {cold}")
        ssh_run(f"rm -rf {shq(cold)}")

    cold_parent = str(Path(cold).parent)
    ssh_run(f"mkdir -p {shq(cold_parent)}")
    ssh_run(f"cp -a {shq(hot)} {shq(cold)}")

    hot_md5 = nas_dir_md5_multiset(hot)
    cold_md5 = nas_dir_md5_multiset(cold)
    if hot_md5 != cold_md5:
        ssh_run(f"rm -rf {shq(cold)}", check=False)
        raise RuntimeError(f"checksum mismatch after copy, aborting this album: {hot}")

    # re-check active sessions again right before the swap (copy can take a while for big albums)
    if hot in currently_playing_nas_paths(url, token):
        print(f"[skip] became active during copy, leaving original untouched: {hot}")
        ssh_run(f"rm -rf {shq(cold)}", check=False)
        return "skipped-playing"

    parked = f"{hot}.parked-{int(time.time())}"
    ssh_run(f"mv {shq(hot)} {shq(parked)}")          # instantaneous local rename, original fully intact
    ssh_run(f"ln -s {shq(cold)} {shq(hot)}")          # symlink now live at the original path

    # sanity check: symlink resolves and at least one regular file is readable through it —
    # NUL-safe, no unquoted shell expansion of a filename (the DeepSeek "$(ls ...)" bug)
    sanity = ssh_run(
        f"find -L {shq(hot)} -maxdepth 1 -type f -print -quit", check=False
    )
    if sanity.returncode != 0 or not sanity.stdout.strip():
        # symlink didn't resolve to anything readable — restore the parked original immediately
        ssh_run(f"rm -f {shq(hot)}", check=False)
        ssh_run(f"mv {shq(parked)} {shq(hot)}", check=False)
        raise RuntimeError(f"post-swap sanity check failed, restored original: {hot}")

    print(f"[migrated] {hot} -> {cold} ({sum(1 for _ in cold_md5)} files), original parked at {parked}")
    return "migrated"


PARKED_SUFFIX_RE = re.compile(r"^(?P<original>.+)\.parked-(?P<ts>\d+)$")


def cleanup_parked(older_than_days):
    """Delete parked originals older than N days.

    Two fixes over the first cut (Codex finding): mv/rename preserves the
    directory's original mtime, so `find -mtime` measures how old the ALBUM's
    *content* is — which for a cold, unplayed album is already old on day one,
    meaning it could get deleted immediately instead of after a grace period.
    Age is now parsed from the `.parked-<epoch>` timestamp embedded in the
    directory name instead. Also: a parked dir is only deleted once its
    corresponding un-parked path is confirmed to be a symlink resolving into
    NAS_COLD_PREFIX (i.e. the migration actually completed) — otherwise it's
    left alone and flagged for manual review.
    """
    out = ssh_run(f"find {shq(NAS_HOT_PREFIX)} -type d -name '*.parked-*'").stdout
    now = time.time()
    candidates = [d for d in out.strip().splitlines() if d and _under_root(d, NAS_HOT_PREFIX)]
    print(f"[cleanup] {len(candidates)} parked dirs found")
    for d in candidates:
        m = PARKED_SUFFIX_RE.match(d)
        if not m:
            print(f"  [skip] unrecognized parked-dir name format: {d}")
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
    ap.add_argument("--skip-precondition-check", action="store_true", help="bypass the Plex-play-data sanity gate")
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
            print(f"[error] {c['nas_hot_path']}: {e}")
            outcome = "error"
        results[outcome] += 1

    print(f"[done] {dict(results)}")


if __name__ == "__main__":
    main()
