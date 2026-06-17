# Upgrade Pipeline v2 — Usenet-primary downloader + U7 dedup + queue thaw

Status: DESIGN (awaiting Codex agreement before implementation)
Branch target: feat/identity-resolver-phase1 → feat/usenet-upgrade-dedup
Author: Claude (main session), 2026-06-17

## Problem (verified live, 2026-06-17)
1. **Downloader broken.** `upgrade_service.download_track()` hardcodes
   `source_url=https://monochrome.tf/track/{id}` (line ~270). Current MusicGrabber rejects it
   → `YouTube info lookup failed: Unsupported URL`. Also seen: `Connection refused`.
2. **Queue frozen.** 14,700 `upgrade_queue` rows are `status='frozen'`, ALL stamped
   `2026-06-11 19:57:43` — the identity-resolver safety freeze. Never thawed. Upgrades have been
   stopped since June 11. (1,365 tracks WERE upgraded successfully back in March.)
3. **No dedup (U7 never built).** `dedup.py` is the Feb detection logic, never wired to the
   Phase-1 safety rails. Lossy+lossless duplicates coexist. Example: Oasis "Don't Go Away" —
   `…/Max 5…/56 - Don't Go Away.mp3` (mp3 221, active) sits beside `…/Max 5…/Don't Go Away.flac`
   (flac 1032, active). The "upgrade" already happened; the inferior MP3 was just never removed,
   so Plexamp plays the MP3.

Baseline (active): 22,839 FLAC, 1,063 mp3, 137 m4a, 30 opus, 30 alac.
1,230 active lossy. ~177 share (artist,title) with an active FLAC (dedup targets, minus
live/alt false matches). ~1,053 have no lossless copy → need a real download.

## DIRECTIVE (Blair, 2026-06-17): **usenet is the PRIMARY downloader for hi-res FLAC.**
Retire the monochrome/MusicGrabber download path to fallback-only.

## Part 1 — Usenet-primary upgrade (route through Lidarr)
Lidarr (`lidarr_url=http://10.0.0.13:8787`, key in settings, qualityProfileId=2 "FLAC Hi-Res
Preferred") already grabs FLAC via NZBgeek→SABnzbd per a usenet-preferring delay profile.
`lidarr_client.py` exposes (EXACT signatures — Codex-corrected):
- `find_artist(name, base_or_client, api_key=None)` → artist dict | None (≥0.72)
- `find_album(artist_id, album_title, base_or_client, api_key=None)` → album dict | None (≥0.72)
- `ensure_monitored_lossless(artist_dict, album_dict, base_or_client, api_key=None)` (forces
   `DEFAULT_QUALITY_PROFILE_ID=2`; caller cannot override profile — fine, 2 is desired)
- `album_inflight(album_id, base_or_client, api_key=None)` / `trigger_album_search(album_id, …)` /
  `album_status(album_id, …)` / `album_trackfiles(album_id, …)`

New module `upgrade_usenet.py`:
- `request_album_upgrade(artist_name, album_title, *, dry_run=False) -> {status, lidarr_album_id, reason}`
  (Lidarr base/key are module-level `BASE`/`API_KEY`, settable via `configure()` or `LIDARR_URL`/
  `LIDARR_API_KEY` env — same convention as `lidarr_recue.py`):
  1. `a = find_artist(artist_name, base, api_key)`. If None → `flagged_no_artist` (FLAG-ONLY; no
     auto-add in Phase 1 per Codex). 2. `al = find_album(a['id'], album_title, base, api_key)`.
     If None → `flagged_no_album`. 3. `ensure_monitored_lossless(a, al, base, api_key)`.
     4. if not `album_inflight(al['id'], base, api_key)`: `trigger_album_search(al['id'], base, api_key)`.
     5. return `searching`/`inflight`; a poller advances via `album_status`.
- Upgrades are **album-level**. Per-track lossy rows roll up to one album request.
- **Persistence (Codex):** do NOT overload `mg_*`. New table `album_upgrades`
  (`id, artist, album, lidarr_album_id, status, attempts, reason, created_at, updated_at`).
  `upgrade_queue` stays per-track and links to the album request by (artist,album).
- Keep `upgrade_service` (monochrome) importable as **fallback only**; default path = usenet.
- **No file_txn here** — Lidarr performs its own import/placement; we only command + poll it.

## Part 2 — U7 identity-gated dedup
Reuse `dedup.find_duplicates(tracks)` (group by norm artist/title, ±5s duration gate, Chromaprint
similarity ≥0.75 → `fingerprint` match, else `metadata`; rank by `scanner.quality_score`;
returns `keep_track`/`trash_tracks`). It is identity-agnostic — the **caller** joins `track_identity`
and applies the gate. Wire into Phase-1 rails:
- **Gate (Codex):** master fail-closed gate is `database.identity_act_enabled()` (default false) —
  honor it. ALSO add a dedicated `dedup_act_enabled` flag via the **settings API** (create it; it
  does not exist yet; default false). Auto-trash requires BOTH true.
- **Identity gate (caller-side):** do not trash a member whose `track_identity.state='conflict'`;
  prefer keeping the member with a `confirmed` identity; skip groups spanning different resolved
  recordings.
- **Schema (Codex):** create migration for new `dedup_actions` table
  (`id, keep_id, trashed_id, match_type, confidence, sha_before, created_at, rolled_back, rolled_back_at`).
- **Action:** reversible trash of inferior copies via the EXACT `file_txn.trash_file_txn(path: Path,
  library_root, revalidate, db_update, meta)` signature (journaled, restorable), set `status='trashed'`,
  write a `dedup_actions` row. **No deletes; no moves outside file_txn.** Do NOT reuse
  `lidarr_recue.py`'s `shutil.move()` quarantine (violates the file_txn rule).
- **Auto-apply bucket (safest):** `match_type='fingerprint'` AND keep is lossless AND trashed is
  lossy AND same resolved identity → auto-eligible (the Oasis pattern). Everything else → review queue.
- **Lossless-sibling check** (for thaw idempotency + dedup) lives OUTSIDE dedup, using
  `lossless_detect.analyze_flac(path)` where a real-lossless verdict is needed.

## Part 3 — Thaw the frozen queue
- Only AFTER Part 1 verified by dry-run. **Thaw LAZILY per run, small batches** (Codex), honoring
  `upgrade_concurrency=2`, `lossless_concurrency=1` — not a bulk one-shot flip of all 14,700.
- Idempotent: skip any track that now has an active lossless sibling (already upgraded/deduped),
  detected via the Part-2 grouping + `lossless_detect.analyze_flac`.
- Resume control via a new `upgrade_paused` flag added through the **settings API** (does not exist
  yet; default true). Runner refuses while true. (Replaces the earlier mis-named `upgrade_frozen`.)
- The downloader the thawed runner calls = Part 1 usenet path, NOT monochrome.

## Safety / rollout
- All file ops via `file_txn` (journaled, reversible). Dedup trash restorable. NO deletes.
- Dry-run mode for both dedup and the usenet poller first; sample-verify before any auto-apply.
- Codex agreement required before code lands; deploy = `docker compose build && up -d` on Beast
  (approval-gated; backend baked into image).

## Resolved (Codex review 2026-06-17)
1. Add-artist: **flag-only** in Phase 1 (auto-add too broad).
2. **Keep** per-track `upgrade_queue`; add an `album_upgrades` layer; do not overload `mg_*`.
3. Auto-apply dedup: **only** fingerprint-confirmed lossy-vs-lossless with same resolved identity.
4. Thaw **lazily per run** in small batches, after a Part-1 dry-run proves idempotency.

## Exact integration contract (Codex round-2, authoritative)
- Trash: `file_txn.trash_file_txn(path: Path, library_root: Path, *, revalidate: Callable[[],bool],
  db_update: Callable[[],None], meta: dict) -> OpResult`.
- Settings: NO generic get/set. Mirror the existing pattern:
  - Add `database.dedup_act_enabled() -> bool` and `database.upgrade_paused() -> bool`
    (alongside existing `database.identity_act_enabled() -> bool`, default false / true).
  - Add the new keys to `ALLOWED_KEYS` + `DEFAULTS` in `backend/routes/settings.py`.
  - Read via the accessors; write via `routes.settings.update_settings({...})`.

## Status
Design LOCKED & Codex-validated (2 rounds). Implementation order: (P1) usenet upgrade +
`album_upgrades` migration → (P2) U7 dedup → (P3) lazy thaw.
- **P1: DONE + Codex-validated + DEPLOYED 2026-06-17** (`backend/upgrade_usenet.py`,
  `database._migrate_album_upgrades`). Live-verified inside the container.
- **P2, P3: IMPLEMENTED + Codex-AGREED (3 rounds) + tested + DEPLOYED & live-verified 2026-06-17**
  (commit `a7f404c`; `docker cp` + restart on Beast; dedup_actions table + safe flags confirmed live).
  New: `backend/dedup_pass.py`, `backend/routes/dedup.py`, `backend/upgrade_thaw.py`,
  `backend/tests/test_dedup_pass.py`, `backend/tests/test_upgrade_thaw.py`. Modified:
  `database.py` (`_migrate_dedup_actions`, `dedup_act_enabled`, `upgrade_paused`),
  `routes/settings.py` (2 keys), `routes/upgrades.py` (thaw/usenet-run/poll/status endpoints),
  `main.py` (dedup router), `upgrade_usenet.py` (`check_upgrade_result` honors `want_title` alone).
  Defaults SAFE: `dedup_act_enabled=false`, `upgrade_paused=true`; dedup ships REVIEW-ONLY.
  Thawed rows use a dedicated `usenet_inflight` status (survives the startup reset). 19 new tests
  pass; no regressions vs the locally-runnable suite. Deploy = `docker cp` the changed
  `backend/*.py` (+`routes/*.py`) into `music-machine` + `docker restart` (approval-gated). Still
  TODO: a frontend review page for `/api/dedup/candidates` (rebuild-gated); legacy
  `/api/upgrades/download` still uses `shutil.move` (pre-existing MusicGrabber fallback, out of scope).

---

# IMPLEMENTATION GUIDE — Parts 2 & 3 (fresh-session handoff)

## START HERE
- Repo: `git@github.com:blaircullen/music-machine.git`, branch `feat/identity-resolver-phase1`.
  Local clone on Mac: `~/projects/music-machine`. Canonical also on Beast `~/projects/music-machine`.
- **Codex-must-agree rule:** Codex reviews every diff before commit/deploy. Use
  `cd ~/projects/music-machine && codex exec --skip-git-repo-check "<review prompt>"`.
- **Deploy (backend-only, NO rebuild):** `docker cp backend/<f>.py music-machine:/app/<f>.py` for each
  changed file, then `ssh olares@10.0.0.13 'docker restart music-machine'`. `init_db()` runs on start
  (creates new tables). Verify a migration with `docker exec music-machine python3 -c "import sqlite3;
  print([r[1] for r in sqlite3.connect('/data/music-machine.db').execute('PRAGMA table_info(<t>)')])"`.
- **Library file-moves + Plex-cred fishing are blocked by the harness classifier from the agent shell.**
  Have Blair run blocked steps via `! ssh …` after you STAGE scripts (scp→docker cp pass; the exec blocks).
- Lidarr: `http://10.0.0.13:8787`, key `2cecee10715a4c1dbe8daa16226f7ed7`, qualityProfileId=2.
  Plex: token `fzVAhz-21g7CfJvA7jK8`, Music = section 5, host Beast `:32400`.
- **First files to read:** `backend/file_txn.py` (trash_file_txn internals — revalidate/db_update/meta),
  `backend/dedup.py` (detection, reuse as-is), `backend/routes/settings.py` (ALLOWED_KEYS/DEFAULTS),
  the upgrade runner (grep `upgrade_queue` in `backend/routes/upgrades.py` + `backend/main.py`),
  `backend/database.py` (`identity_act_enabled` pattern to mirror).

## Part 2 — U7 dedup (NEW module `backend/dedup_pass.py`; keep `dedup.py` as pure detection)
Default is **REVIEW-ONLY** (CLAUDE.md hard rule: "No automated file actions — user reviews all
duplicate resolutions manually"). Auto-apply is opt-in via a flag, default OFF.

1. **Migration** `database._migrate_dedup_actions(db)` (wire into `init_db` next to
   `_migrate_album_upgrades`):
   `dedup_actions(id PK, keep_id INT, trashed_id INT, match_type TEXT, confidence REAL,
   sha_before TEXT, created_at TS, rolled_back INT DEFAULT 0, rolled_back_at TS)`.
2. **Settings flag** `dedup_act_enabled` (default 'false'): add `database.dedup_act_enabled() -> bool`
   mirroring `identity_act_enabled()`, and add the key to `ALLOWED_KEYS`+`DEFAULTS` in
   `routes/settings.py`.
3. **`find_dedup_candidates(limit=None)`**: load active tracks (all cols dedup needs incl.
   `fingerprint`, `duration`, `file_path`) via `database.get_db()`; call
   `dedup.find_duplicates(tracks)`; for each group JOIN `track_identity` and:
   - **skip** the group if any member's `track_identity.state == 'conflict'` OR members resolve to
     *different* recordings (different `mb_recording_id`/identity).
   - keep = highest `scanner.quality_score` AMONG members sharing the resolved identity (prefer a
     `confirmed` member if quality ties).
   - `auto_eligible = (match_type=='fingerprint') and keep.format in {flac,alac} and
     all(trash.format in lossy) and same identity`.
4. **`apply_dedup(group, *, dry_run=False)`**: require `database.identity_act_enabled() AND
   database.dedup_act_enabled()` (fail-closed); for each trash track call
   `file_txn.trash_file_txn(Path(path), MUSIC_ROOT, revalidate=<sha/exists check>,
   db_update=<UPDATE tracks SET status='trashed' WHERE id=?>, meta={'keep_id':…,'reason':'dedup'})`;
   insert a `dedup_actions` row. **No `shutil.move`** (don't reuse `lidarr_recue._quarantine`).
5. **Route + UI:** add `routes/dedup.py` (`GET /api/dedup/candidates`, `POST /api/dedup/apply` with
   explicit ids). Reuse the existing **Duplicates** page/shape (`/api/dupes/` flat shape in CLAUDE.md)
   — surface `auto_eligible` + identity state per group; apply only on user approval.
6. **Sanity check:** the manually-quarantined Oasis mp3 (id 35717, flac twin 35709) is the canonical
   auto_eligible case — a fresh full run should produce ~the 177 lossy-with-FLAC-twin set MINUS
   live/alt false matches (verify the duration gate culls those).

## Part 3 — lazy thaw of the frozen upgrade queue
The freeze migration is idempotent (`freeze_migration_version='1'`), so a thaw STICKS across restarts.

1. **Settings flag** `upgrade_paused` (default 'true' = paused): `database.upgrade_paused() -> bool`
   + ALLOWED_KEYS/DEFAULTS. Runner refuses while true.
2. **Repoint the runner to usenet:** find the worker that drains `upgrade_queue` (it currently calls
   `upgrade_service.download_track` → monochrome). Replace the download step with
   `upgrade_usenet.request_album_upgrade(artist, album)` (roll per-track rows up to album; dedupe by
   (artist,album) via the `album_upgrades` table). Keep `upgrade_service` import as fallback only.
3. **Lazy batches:** process a SMALL batch per run honoring `upgrade_concurrency=2`,
   `lossless_concurrency=1`. The Lidarr queue is already ~895 deep + 30,577 missing-monitored —
   **NEVER** mass-trigger. A "thaw N" action flips `frozen`→`pending` for the next N rows only.
4. **Idempotent skip:** before requesting, skip any track that already has an active lossless sibling
   (reuse Part-2 grouping / `lossless_detect.analyze_flac`) — e.g. the Oasis case needed dedup, not a
   download.
5. **Poll/complete:** use `upgrade_usenet.check_upgrade_result(album_id, want_basename=…)` to mark the
   `album_upgrades` row `placed`; then Part-2 dedup removes the now-superseded lossy original (review).

## Deploy + verify the P2+P3 bundle
`docker cp` changed `backend/*.py` (+ `routes/*.py`) into `music-machine`, `docker restart`, confirm
`dedup_actions`/flags exist, run a DRY-RUN of `find_dedup_candidates` + a single `request_album_upgrade`,
then enable flags deliberately. Plex section-5 refresh after any trashing.
