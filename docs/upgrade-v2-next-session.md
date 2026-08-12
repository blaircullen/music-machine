# Upgrade Pipeline v2 — what's left (fresh-session handoff)

Companion to `docs/upgrade-v2-spec.md`. P1, P2, P3 are **built + Codex-AGREED + deployed**. This
file is the remaining work and the operational rollout. Read the spec's IMPLEMENTATION GUIDE for
the design; read this for what to do next.

## Current state (2026-06-17)
- Branch `feat/identity-resolver-phase1`. Code commit **`a7f404c`** (P2+P3), doc commit `9d0a99e`.
- **Deployed to Beast** via `docker cp` + `docker restart` (no rebuild — no new deps). Container
  `music-machine` on `olares@10.0.0.13`, app at `/app/` inside it, port 8686.
- Live-verified: `dedup_actions` table exists; flags seeded SAFE — `dedup_act_enabled=false`,
  `upgrade_paused=true`. Endpoints respond 200. **Nothing has auto-run** (dedup review-only, thaw
  paused, `pending=0`, `frozen=14700`).
- Local clone on Mac at `~/projects/music-machine` (also canonical on Beast). ⚠️ Mac↔Goat rsync
  (~900s) mutates files in `~/projects/*` mid-session — `git status` to confirm your change surface
  before committing; re-Read a file right before Edit if it's been a while.

## Remaining work (priority order)

### 1. Frontend dedup review page  (the real next step — REBUILD-GATED)
The backend review API exists; there is no UI yet, so the "user reviews all duplicate resolutions
manually" rule is currently only servable via the API.
- New page `frontend/src/pages/Dedup.tsx` consuming `GET /api/dedup/candidates`
  (`?auto_only=`, `?limit=`, `?include_skipped=`) and `POST /api/dedup/apply`
  (`{keep_id, trash_ids, dry_run}`). Reuse the **Duplicates** page shape/components.
  - Each candidate carries `auto_eligible`, `keep`, `trash[]`, and per-track `identity_state` +
    `mb_recording_id` — surface those so the human sees WHY a group is/ isn't auto-eligible.
  - Apply only on explicit user click; the server re-verifies the group, so passing wrong ids is
    safe (400), but the UI should still send the exact keep/trash from the candidate.
  - Show the two gate flags (`identity_act_enabled`, `dedup_act_enabled`) returned by the endpoint;
    if either is false, apply will 403 — make that legible (e.g. a banner).
- Wiring (per CLAUDE.md "Adding a new page"): add lazy import + `<Route>` in `App.tsx` AND an entry
  in `NAV_ITEMS` in `Sidebar.tsx`. Pages are not auto-discovered.
- **Deploy = FULL rebuild** (frontend is baked, no volume mount):
  `rsync -av frontend/src/ olares@10.0.0.13:~/projects/music-machine/frontend/src/` then
  `ssh olares@10.0.0.13 'cd ~/projects/music-machine && docker compose build --no-cache && docker compose up -d'`.
  Must be `--no-cache` (cached frontend layers otherwise). HTTP-check after.

### 2. Operational rollout (only when Blair says go — high blast: trashes files / triggers Lidarr)
Do BOTH dry-runs and sample-verify before flipping any flag.
- **Dedup (Part 2):**
  1. Dry-run survey: `GET /api/dedup/candidates?auto_only=true` — eyeball the auto bucket
     (expect ~the 177 lossy-with-FLAC-twin set minus live/alt false matches; verify the ±5s
     duration gate culled those).
  2. To act: set **both** `identity_act_enabled=true` AND `dedup_act_enabled=true` via
     `PUT /api/settings`. Then `POST /api/dedup/apply {keep_id, trash_ids, dry_run:false}` per group.
     Trash is reversible (file_txn journal under `<MUSIC_ROOT>/.m2-trash/`; `dedup_actions` row).
  3. After trashing, refresh Plex section 5:
     `curl -s "http://10.0.0.13:32400/library/sections/5/refresh?X-Plex-Token=fzVAhz-21g7CfJvA7jK8"`.
- **Thaw (Part 3):**
  1. `POST /api/upgrades/usenet-run {dry_run:true}` — preview the per-track→album rollup and
     lossless-sibling skips WITHOUT touching the queue or Lidarr (allowed while paused).
  2. Set `upgrade_paused=false`. Thaw a SMALL batch: `POST /api/upgrades/thaw {"n":5}`
     (flips 5 of 14,700 frozen → pending). NEVER bulk-thaw — Lidarr queue is already deep.
  3. `POST /api/upgrades/usenet-run` (real). Rows go to `usenet_inflight`.
  4. Later: `POST /api/upgrades/usenet-poll` advances landed tracks to `found` (per-track via
     `want_title`); then dedup (step above, review) removes the superseded lossy original.
  5. Watch `GET /api/upgrades/thaw-status` between steps.

### 3. Legacy downloader file_txn retrofit (out-of-scope follow-up; Codex round-1 #7)
`routes/upgrades.py` `_run_download_worker` (the OLD MusicGrabber `/api/upgrades/download` path)
still does direct `shutil.move` + `file_manager.trash_file`, bypassing `file_txn`. The spec keeps
MusicGrabber as "fallback only," so this wasn't retrofitted. If the usenet path proves out, either
retire that endpoint or route its trash/replace through `file_txn.replace_file`. Gated by
`identity_act_enabled` today, so not urgent.

### 4. Durability of the current deploy
P2/P3 was a `docker cp` hot-deploy into the container's writable layer (survives restart). Beast's
repo is at `a7f404c`, so a **future `docker compose build`** bakes the code into the image. Until
that rebuild, a `docker compose down/up` (recreate) would revert the container — re-`docker cp` if
that happens. Doing item #1's full rebuild also makes P2/P3 durable.

## Key files
- `backend/dedup_pass.py` — P2 logic (find_dedup_candidates, apply_dedup, _verify_request).
- `backend/routes/dedup.py` — `/api/dedup/candidates`, `/api/dedup/apply`.
- `backend/upgrade_thaw.py` — P3 (thaw_next, run_usenet_upgrade_batch, poll_thawed_upgrades, thaw_status).
- `backend/routes/upgrades.py` — `/thaw`, `/usenet-run`, `/usenet-poll`, `/thaw-status` at the end.
- `backend/database.py` — `_migrate_dedup_actions`, `dedup_act_enabled()`, `upgrade_paused()`.
- `backend/upgrade_usenet.py` — P1; `check_upgrade_result` now honors `want_title` alone.
- Tests: `backend/tests/test_dedup_pass.py`, `backend/tests/test_upgrade_thaw.py`.

## Running tests locally (env gotcha)
The repo venvs (`.venv`, `backend/.venv`) are bare and resolve to a `plex-dedup` path. Use **system
`python3`**, which has `mutagen`+`fastapi` but NOT `musicbrainzngs`/`httpx`/`essentia`/FLAC binaries.
- The P2/P3 tests only need stdlib + the dedup import chain:
  `bash -c "PYTHONPATH=backend python3 -m pytest backend/tests/test_dedup_pass.py backend/tests/test_upgrade_thaw.py -q"`
- Baseline for the locally-runnable subset (ignore the 3 un-collectable modules):
  `--ignore=backend/tests/test_normalizer_golden.py --ignore=backend/tests/test_upgrade_service.py --ignore=backend/tests/test_upgrades_scoped.py`
  → was **215 passed / 12 failed / 11 errors** pre-change (all failures are env-dep gaps), now 234
  passed. The real full-suite gate is Docker/CI.
- Codex-must-agree before commit/deploy: `cd ~/projects/music-machine && codex exec --skip-git-repo-check "<review prompt>"`.

## Deploy recap (backend-only, no rebuild)
`ssh olares@10.0.0.13 'cd ~/projects/music-machine && git pull --ff-only'`, then `docker cp
backend/<f>.py music-machine:/app/<f>.py` (routes → `/app/routes/`) for each changed file, then
`docker restart music-machine`. `init_db()` runs migrations on boot. Verify a migration:
`docker exec music-machine python3 -c "import sqlite3; print([r[1] for r in sqlite3.connect('/data/music-machine.db').execute('PRAGMA table_info(dedup_actions)')])"`.
