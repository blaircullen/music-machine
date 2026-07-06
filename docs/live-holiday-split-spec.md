# Live & Holiday Library Split — Build-Ready Spec

**Status:** corrected / implementation-ready
**Date:** 2026-07-06
**Supersedes:** `docs/plans/2026-07-06-001-feat-live-holiday-library-segmentation-plan.md` (prior draft — see "Corrections vs. prior draft" at the end; the playlist decision is reversed).

> Grounding note: sections marked **[VERIFIED]** cite real code/schema in this repo
> (read on 2026-07-06). Sections marked **[VERIFY AT BUILD]** are recommendations
> the builder must confirm against live state on Beast before relying on them
> (the local `plex-dedup.db` copy is empty; production DB is `/data/music-machine.db`
> inside the `music-machine` container on Beast `olares@10.0.0.13`).

---

## 1. Goal & Scope

### Goal
Keep live-performance and holiday/occasion tracks out of the **default Plex/Plexamp
shuffle and radio** on the main Music library, while keeping them fully findable by
search and addable to playlists. Done by moving those tracks into two **separate Plex
libraries** fed by **separate on-disk folders**, driven by a periodic sweep over
AudD-corrected metadata already in the Music Machine DB. This is an ongoing system, not
a one-time cleanup.

Separate libraries (not labels/smart-playlists) is a settled decision: Plex's native
library shuffle/radio has no label/genre/mood **exclusion** control, so only a separate
library removes a track from the default shuffle.

### In scope
- Two new Plex libraries: **Live Performances** and **Holiday**, each backed by a new
  on-disk folder placed **outside** the main Music library's scanned root (§4).
- Per-**track** classification from DB metadata (a live album's studio bonus track stays;
  a studio album's one live bonus track moves). No new fingerprinting/AudD spend.
- Confidence-tiered workflow: dry-run report first → auto-move only high-confidence →
  everything else to a manual review queue (§6, resolves Issue 1).
- Physical file move with DB path update in the same committed step, a move ledger, and
  full undo (§7, §8; resolves Issues 3 & 5).
- **Playlist protection**: snapshot affected playlists before moving, auto-repair
  membership after, and emit a report for anything that couldn't be auto-repaired (§9;
  resolves Issue 4).
- A periodic sweep (nightly) decoupled from Lidarr, that only re-examines tracks new or
  changed since the last run (§10).
- Enabling Plexamp "Search all Libraries" + per-library "include in global search" so
  cross-library search still works (§11).

### Out of scope
- Real-time Lidarr import hook (sweep only).
- Sub-classification of live (by venue/tour) or holiday (by occasion into more than one
  library) — all occasion music → the single **Holiday** library.
- Any behavior change to existing dedup / FLAC-upgrade / trash logic (the post-move DB
  path update is a data-integrity necessity, not a behavior change).
- Audio-level "crowd/applause" corroboration — an optional future stretch, explicitly not
  required for this spec to ship.

---

## 2. Data sources — DB schema this reads and writes

### Reads **[VERIFIED — `backend/database.py`]**

- **`tracks`** (`database.py:69-87`) — the live per-track row. Columns used:
  `id`, `file_path` (**UNIQUE NOT NULL** — the one path key on this table),
  `artist`, `album_artist`, `album`, `title`, `sha256`, `status` (`'active'`),
  `scanned_at`. **There is no `genre` column on `tracks`.**
- **`fingerprint_results`** (`database.py:280-312`) — AudD-corrected metadata, one row
  per track (`UNIQUE(track_id)`). Columns used:
  `matched_artist`, `matched_title`, `matched_album`, `matched_genre`,
  `matched_genre_raw`, `audd_data` (JSON), `composite_confidence`, `match_source`
  (`'audd'` when AudD-sourced), `status` (`'applied'` when the correction was written
  back to the file/tags).

  **Metadata resolution rule:** for each track, prefer
  `fingerprint_results.matched_{artist,title,album,genre,genre_raw}` when a row exists
  with `status='applied'`; otherwise fall back to `tracks.{artist,title,album}` (raw
  tags). This makes coverage universal — no track is silently skipped for lacking AudD
  data. Genre is only available from `fingerprint_results` (`matched_genre` /
  `matched_genre_raw`); when absent, genre-based signals simply don't fire.

- **`genre_map`** (`database.py:342-346`) + `genre_normalizer.py` — the normalizer already
  maps `christmas`/`holiday`/`xmas` → `"Holiday"` (`genre_normalizer.py:116-117`). Use the
  **normalized** genre value as the strong genre signal for holiday. **[VERIFY AT BUILD]**
  whether AudD ever emits a `"Live"` genre; if `matched_genre_raw`/`matched_genre` can equal
  `"Live"`, treat it as a strong live signal, otherwise rely on title/album markers only.

### Writes (new tables — §3)
- `segmentation_candidates` (review queue), `segmentation_moves` (move ledger),
  `settings` kill-switch rows, and an additive `tracks.move_status` claim column.
- Updates `tracks.file_path` (+ `scanned_at`) on every executed move.

---

## 3. New schema

All added via a new `_migrate_segmentation(db)` in `backend/database.py`, wired into
`init_db()` (**mandatory** — an unwired `_migrate_*` fn silently never runs; this repo's
documented #1 gotcha, `CLAUDE.md` → "Migrations on docker restart"). Verify post-deploy
with `PRAGMA table_info(...)`. Pattern to mirror: `_migrate_dedup_actions`
(`database.py:577-600`) and the `log_recue()` append helper (`database.py:653-682`).

```sql
-- Review queue: ambiguous / non-auto matches await explicit approval.
CREATE TABLE IF NOT EXISTS segmentation_candidates (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    track_id         INTEGER REFERENCES tracks(id),
    source_path      TEXT,
    dest_path        TEXT,
    target_library   TEXT,        -- 'live' | 'holiday'
    matched_field    TEXT,        -- 'title' | 'album' | 'genre'
    matched_pattern  TEXT,        -- the rule/marker that fired
    confidence_tier  TEXT,        -- 'auto' | 'review'
    confidence_reason TEXT,       -- human-readable why
    detected_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    status           TEXT DEFAULT 'proposed'
                     -- 'proposed' | 'approved' | 'rejected' | 'moved'
);

-- Move ledger (append-only, reversible). Models dedup_actions + adds path/rating-key/rule.
CREATE TABLE IF NOT EXISTS segmentation_moves (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    track_id         INTEGER,
    source_path      TEXT,
    dest_path        TEXT,
    target_library   TEXT,        -- 'live' | 'holiday'
    matched_pattern  TEXT,
    confidence_tier  TEXT,        -- 'auto' | 'reviewed'
    old_rating_key   TEXT,        -- Plex ratingKey in section 5 before move
    new_rating_key   TEXT,        -- Plex ratingKey in target section after rescan
    sha_before       TEXT,
    sha_after        TEXT,
    run_id           TEXT,        -- groups a sweep/bulk run (for whole-run undo)
    state            TEXT DEFAULT 'pending',   -- 'pending' | 'done'
    moved_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    rolled_back      INTEGER DEFAULT 0,
    rolled_back_at   TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_segmoves_run ON segmentation_moves(run_id);
CREATE INDEX IF NOT EXISTS idx_segmoves_track ON segmentation_moves(track_id);

-- Playlist membership snapshot, captured before a run's moves (Issue 4).
CREATE TABLE IF NOT EXISTS segmentation_playlist_snapshot (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id           TEXT,
    playlist_rating_key TEXT,
    playlist_title   TEXT,
    track_id         INTEGER,
    old_rating_key   TEXT,
    file_path        TEXT,        -- match key used to re-resolve after move
    repaired         INTEGER DEFAULT 0,   -- 1 = re-added post-move
    repair_error     TEXT,
    snapshot_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

Additive column on `tracks` (via `PRAGMA table_info` guard + `ALTER TABLE ADD COLUMN`;
never recreate `tracks`):
```sql
-- optimistic move claim so the sonic-analyzer sidecar and a second mover skip a track mid-move
ALTER TABLE tracks ADD COLUMN move_status TEXT;   -- NULL | 'claiming' | 'moving'
```

Kill-switch settings (fail-closed, modeled on `identity_act_enabled()`
`database.py:603-616`), seeded `'false'`:
- `segmentation_move_enabled` — gates any physical auto-move.
- `segmentation_sweep_enabled` — gates the nightly loop.

---

## 4. Filesystem & mount topology **[VERIFIED mount / VERIFY AT BUILD exact root]**

- **[VERIFIED]** The `music-machine` container sees the NAS music share at `/music`
  (NFS, rw); `reorg_worker.MUSIC_ROOT` defaults to `/music/FLAC`
  (`reorg_worker.py:42`). Beast host path is `/mnt/nas/music` (`CLAUDE.md` → "Beast NFS
  mount"; `/mnt/music` and `/mnt/nas_music` are empty decoys — do not use). Plex Music =
  **section 5**, Plex host = Beast `10.0.0.13:32400` (`plex_playlist_sync.py:19-23`,
  `CLAUDE.md` → "Plex Library Scan").

- **Load-bearing placement rule:** the two new folders **must live outside the exact
  on-disk root that Plex section 5 indexes.** If they are placed *inside* section 5's tree,
  Plex keeps indexing them into the main library and they still surface in shuffle —
  defeating the whole feature.
  - **[VERIFY AT BUILD]** the exact root section 5 scans on Beast (`/mnt/nas/music` vs
    `/mnt/nas/music/FLAC` vs other). If it cannot be confirmed, **stop and surface** — do
    not guess placement.
  - Suggested layout (adjust to the verified root): Beast `/mnt/nas/Live Performances`
    and `/mnt/nas/Holiday`, siblings of the section-5 root, bind-mounted into the
    container as `/live_performances` and `/holiday` (rw).

- **sonic-analyzer sidecar:** it mounts the music share read-only and reads by path. Add
  the **same two folders as read-only binds** to the `sonic-analyzer` service in
  `docker-compose.override.yml`, or moved tracks drop out of sonic analysis. Placement
  must keep them reachable from the sidecar's mount while still outside section 5's tree.

- **Mount sentinel guard:** before any move, require a sentinel file (e.g. `.mm_mount_ok`)
  present NAS-side in each target root, AND compare `st_dev` of the dest root against the
  known `/music` device id. If either fails, **abort the whole run, zero files moved** —
  otherwise a missing bind mount lets the move write into the container's ephemeral
  overlay and the source is then lost.

---

## 5. Classification rules

Classification runs on the **resolved** metadata from §2 (AudD-corrected preferred, raw
tags fallback). It inspects **title, album, and genre** fields. Output per track:
`(matched: bool, target_library, matched_field, matched_pattern, tier)` where
`tier ∈ {auto, review}`.

Encode all patterns as editable data (lists of regexes), case-insensitive, Unicode-normalized,
so tuning needs no control-flow change. All regexes below are **word/marker-anchored** —
never bare substring — to avoid the false positives that motivated this rewrite.

Precedence when a track matches both live and holiday: **holiday wins** (a "Christmas
(Live)" track is more surprising in summer shuffle than in a live-only library) — document
and unit-test this.

### 5A. LIVE

**High-confidence → `auto`** (anchored structural markers, checked against **title**;
also honor a genre signal if present):
- Parenthetical/bracket tag: `\((?:live[^)]*)\)` , `\[(?:live[^\]]*)\]`
  (matches `(Live)`, `(Live at Wembley)`, `[Live]`).
- Positional phrase: `\blive (?:at|from|in|on)\b` (`Live at Wembley`, `Live from Texas`).
- ` - Live` trailing marker: `-\s*live\b` (`Thunderstruck - Live`).
- `\blive version\b`, `\blive recording\b`.
- `\bunplugged\b`, `\bmtv unplugged\b`.
- **Genre signal [VERIFY AT BUILD]:** resolved genre exactly `"Live"` (if AudD emits it).

**Ambiguous → `review`** (dump to queue, never auto):
- A bare `live`/`living`/`alive`/`livin` token **not** matching any high-confidence marker
  above. Maintain an explicit stoplist of known non-live titles to *suppress even from the
  review queue* to cut noise: `livewire`, `alive`, `live and let die`, `livin' on a prayer`,
  `living on a prayer`, `stayin' alive`, `live to tell`, `live and learn`.
- `\bacoustic\b`, `\bsession(s)?\b`, `\bconcert\b` in title with no anchored live marker.

### 5B. HOLIDAY / occasion (tightened — resolves Issue 2)

The old draft treated bare `christmas`/`jingle bell`/`silent night` and single words like
`winter`/`snow`/`bell` as sufficient — too broad. New rule: **strong signals auto-move;
weak single words never fire alone.**

**High-confidence → `auto`** — requires at least one **strong** signal:
1. **Strong signal in ALBUM (preferred anchor):** album name contains an explicit
   whole-word occasion term:
   `\bchristmas\b`, `\bx-?mas\b`, `\bholiday(s)?\b` (as in *"A Holiday Album"* / *"Holiday
   Collection"*), `\bnoël?\b`, `\bnavidad\b`, `\bhanukkah\b`, `\bkwanzaa\b`.
   Album-level anchoring is the strongest signal because holiday **compilations** are
   album-scoped (e.g. *"Now That's What I Call Christmas"*, *"A Very Special Christmas"*,
   *"Merry Christmas"* by any artist).
2. **Strong signal in GENRE:** resolved genre normalizes to `"Holiday"` — the normalizer
   already maps `christmas`/`holiday`/`xmas` → `"Holiday"` (`genre_normalizer.py:116-117`).
3. **Strong signal in TITLE** — explicit occasion term as a whole word:
   `\bchristmas\b`, `\bx-?mas\b`, `\bhanukkah\b`, `\bfeliz navidad\b`,
   `\bauld lang syne\b`, `\bhalloween\b`.

**Ambiguous / secondary → `review`** (weak signals — dump to queue, **never auto alone**):
- Single weak words that collide with ordinary songs: `\bwinter\b`, `\bsnow\b`, `\bbell(s)?\b`,
  `\bjingle\b`, `\bsanta\b`, `\bsleigh\b`, `\bmistletoe\b`, `\breindeer\b`, `\bfrosty\b`,
  `\bnoel\b` (title-only), `\bspooky\b`, `\bpumpkin\b`, `\bfireworks\b`,
  `\bindependence day\b`, `\bbirthday\b`, `\bnew year'?s?\b`, `\bthanksgiving\b`,
  `\beaster\b`.
  - These false-positive constantly ("Winter" the indie song, "Snow (Hey Oh)" by RHCP,
    "White Christmas" studio covers that aren't tagged holiday, "Bells" as an art-rock
    title, "Birthday" by the Beatles/Katy Perry, "Independence Day" by Martina McBride).
  - **Promotion rule:** a weak word may **only** promote to `auto` when it **co-occurs with
    a strong signal** (e.g. title `"Jingle Bells"` while album genre normalizes to
    `"Holiday"`, or album contains `christmas`). A weak word alone always goes to `review`.
- `\bwhite christmas\b` deserves special care: the title term `christmas` is strong, but
  because non-holiday covers/samples exist, require it to co-occur with a holiday album or
  genre signal to auto-move; otherwise → `review`.

### 5C. Confidence-tier decision (the single rule — resolves Issue 1)

> A track is **`auto`** (physically moved without human review) **only if** it hits a
> §5A high-confidence live marker OR a §5B strong holiday signal (album term, genre =
> Holiday, or explicit whole-word title occasion term), and passes the collision guard
> (§7). **Every other match — including any weak/single-word holiday keyword, any bare
> `live` substring, and any within-batch or existing-file dest collision — is written to
> `segmentation_candidates` with `status='proposed'` and moves nothing until Blair
> explicitly approves it.** Appearing in the report is *not* approval.

This is the definitive policy. There is no "auto-apply directly from DB without review"
path for anything below high-confidence.

---

## 6. Dry-run report (resolves Issue 1, reporting half)

Every run first executes in **dry-run**: classify the scope, compute each candidate's
`dest_path`, and write results **without moving anything**. Outputs:

1. **DB:** insert/update `segmentation_candidates` rows (`status='proposed'` for review-tier;
   auto-tier rows are recorded too, tagged `confidence_tier='auto'`, so the report shows
   exactly what *would* auto-move).
2. **Flat manifest file:** `/data/segmentation_last_run.json` — mirrors the existing
   `reorg_last_run.json` convention (`routes/reorg.py`). Contains: `run_id`, timestamp,
   totals, per-category counts (live/holiday × auto/review), the **full collision list**,
   and a sample of `src → dest` pairs.
3. **Review UI page** (primary surface): a "Segmentation" page listing pending candidates
   (track, matched field+pattern, source → dest, confidence reason, target library) with
   per-row approve/reject and an "apply approved" action. Model on
   `FingerprintReview.tsx` / `Dedup.tsx`; poll `/api/segmentation/status` via a
   `useRef<setInterval>` hook (repo leak-prevention pattern). Register in **both**
   `App.tsx` (route) and `Sidebar.tsx` (`NAV_ITEMS`) — pages are not auto-discovered.

**Report row format (each candidate):**
`track_id | artist – title | album | matched_field | matched_pattern | tier (auto/review) | confidence_reason | source_path → dest_path | target_library`

---

## 7. Move execution — ordered steps (resolves Issue 3)

The mover handles **one candidate** at a time. It does file + DB + ledger atomically and
does **not** trigger Plex scans (the worker batches those, §10). Use a
copy → verify → commit → unlink discipline (not a bare `shutil.move`), because the
container↔NFS boundary makes a move a non-atomic copy+unlink. This ordering means the
**source file survives until the DB path update is committed**, which is exactly the
"roll back on DB-update failure" guarantee Issue 3 demands — expressed in the safest form
(if the DB step fails, the source was never deleted; the dest copy is removed and the
candidate is reopened, so the net state is "file stayed put, DB unchanged").

Real target column: **`tracks.file_path`** (the only path-keyed column on `tracks`;
`UNIQUE NOT NULL`). **[VERIFY AT BUILD]** whether any *other* table stores a per-track path
(grep the live schema; `analysis_queue`, dedup, upgrade tables key off `track_id`, not
path, in what was read — confirm before build). If another path column exists, it must be
updated in the same transaction.

**Ordered steps:**
1. **Guards (abort run on failure — zero files moved):** mount sentinel + `st_dev` check
   (§4); kill switch `segmentation_move_enabled` is `true` (defense-in-depth, worker also
   checks); never-overwrite — refuse if `dest` exists and is not a stale non-active row
   being replaced.
2. **Claim** the track: `UPDATE tracks SET move_status='claiming' WHERE id=? AND
   move_status IS NULL`; if rowcount 0, another mover has it → skip.
3. **Intent ledger row:** insert `segmentation_moves` with `state='pending'`, `sha_before`
   (from `tracks.sha256` or freshly hashed), `run_id`, `matched_pattern`, tier,
   `old_rating_key` (resolved from Plex before the move — needed for playlist repair, §9).
4. **Copy:** `os.makedirs(dirname(dest), exist_ok=True)`; `shutil.copy2(src, dest)`.
   `dest` preserves the relative `Artist/Album/NN - Title.ext` structure under the target
   library root (reuse `reorg_worker.build_dest_path` logic against the new root).
5. **Verify:** `sha256(dest) == sha_before` and file sizes match. Mismatch → delete dest,
   fail loudly, leave ledger `pending` for the reconciler, clear `move_status`.
6. **Single committed DB transaction** (`get_db()`, WAL — `database.py` uses
   `PRAGMA journal_mode=WAL`):
   - `DELETE FROM tracks WHERE file_path = <dest> AND status != 'active'` (clear stale row,
     avoid `UNIQUE(file_path)` violation — mirrors `reorg_worker.py:180-190`).
   - `UPDATE tracks SET file_path=<dest>, scanned_at=CURRENT_TIMESTAMP, move_status=NULL
     WHERE file_path=<src> AND status='active'`.
   - `UPDATE segmentation_moves SET state='done', sha_after=<sha> WHERE id=<ledger_id>`.
   - **If this transaction raises → roll back: delete the dest copy, leave source and DB
     untouched, mark the candidate for review, clear `move_status`. The source file is
     never unlinked before this commit succeeds.** (This is the concrete resolution of
     Issue 3: the DB path update and the physical relocation commit together; failure of
     the DB update un-does the file move.)
7. **Unlink source** only after the transaction commits. A crash between commit and unlink
   leaves a harmless duplicate the reconciler cleans up (DB already points at dest).
8. Mark the `segmentation_candidates` row `status='moved'`.

> Contrast with the existing `reorg_worker.py:170-192`, which does `shutil.move` then only
> **logs a warning** if the DB update fails (`reorg_worker.py:192`) — leaving a stale-path
> orphan. That is exactly the bug Issue 3 forbids; this mover must not repeat it.

**Reconciler** (run at container start and at each sweep entry): for every
`segmentation_moves` row still `state='pending'` — if `tracks.file_path` already equals
`dest`, roll forward (mark `done`); else delete any dest orphan, clear the claim, reopen
the candidate. No data loss either way.

---

## 8. Move ledger & undo (resolves Issue 5)

- **Ledger = `segmentation_moves`** (§3), append-only, one row per attempted move. It
  captures everything needed to fully reverse: `source_path`, `dest_path`, `moved_at`,
  `matched_pattern`, `confidence_tier`, `old_rating_key`, `new_rating_key`, `sha_before`,
  `sha_after`, `track_id`, `run_id`, `state`, `rolled_back`/`rolled_back_at`. The flat
  `/data/segmentation_last_run.json` (§6) is a secondary human-readable artifact; the DB
  table is the authority.
- **Undo granularity:** single move **and** whole-run (`WHERE run_id=?`). Required for at
  least the first bulk run; available for every run.
- **`reverse_move(ledger_id)`** — gate on four invariants before touching disk, then invert:
  1. `rolled_back = 0` (not already undone).
  2. `dest` exists AND `sha256(dest) == sha_after` — refuse if a downstream pipeline
     rewrote the file (don't clobber a legitimate change).
  3. Source slot free on disk AND no `tracks` row occupies `source_path`.
  4. The `tracks` row still holds `file_path = dest`.
  - Then: move `dest → source_path`, `UPDATE tracks SET file_path=<source_path>`, set
    `rolled_back=1`, `rolled_back_at=now`. Trigger rescans of **both** the target section
    (drop the vanished entry) and section 5 (re-index the restored file), and re-run the
    playlist repair (§9) so the restored track returns to its playlists.
  - Any invariant fails → refuse with a clear reason (never collapse to null/false — repo
    error-evidence rule); flag for manual handling.

---

## 9. Playlist protection (resolves Issue 4) — REVERSES the prior draft

> The prior draft put playlist preservation **out of scope** ("a moved track may drop
> out of / break in any playlist — no remediation"). That is wrong for this task: Blair
> does not care about the *membership philosophy* but does **not** want playlists to
> **silently** break. This section makes protection mandatory.

**Why it breaks:** when a track's file changes library section, **Plex assigns it a new
`ratingKey`.** Any existing playlist references the *old* ratingKey and silently drops the
track — no error, no warning.

**Mechanism** (all Plex helpers below **[VERIFIED — `plex_playlist_sync.py`]**):

**Before the first move of a run (snapshot):**
1. List all playlists: `GET /playlists` (Plex). For each, `GET /playlists/<id>/items`
   → member `ratingKey`s (`_get_playlist_track_keys()`, `plex_playlist_sync.py:345-357`).
2. Resolve each member `ratingKey` to its **file path** (`/library/metadata/<ratingKey>`
   → `Media/Part/@file`).
3. Intersect with the set of tracks this run will move (match by `file_path`). For every
   `(playlist, track)` pair where the track will move, insert a
   `segmentation_playlist_snapshot` row (`run_id`, `playlist_rating_key`, `playlist_title`,
   `track_id`, `old_rating_key`, `file_path`).
   *(Also stash the `old_rating_key` into the corresponding `segmentation_moves` row.)*

**After the run's moves + the target-section rescan (repair):**
4. For each snapshot row, re-resolve the track's **new** `ratingKey` in the target library
   section — by file path via `GET /library/sections/<new_section_id>/all` and matching
   `Media/Part/@file == dest_path`, or via a title/artist search scoped to the new section.
   *(Note: `search_plex_track()` `plex_playlist_sync.py:207` currently hard-codes
   `MUSIC_SECTION_ID=5`; it must be parameterized by section, or use the file-path match
   above.)*
5. Re-add the track to the playlist: `PUT /playlists/<playlist_rating_key>/items?uri=<uri>`
   where `uri = _build_uri(get_machine_id(), [new_rating_key])`
   (`_plex_put` + `_build_uri` + `get_machine_id`, `plex_playlist_sync.py:40-49,319-322,72-81`;
   this is the exact call `sync_m3u_to_plex` uses at `:416`). On success set
   `repaired=1`.
6. **Any pair that cannot be auto-repaired** (new ratingKey not resolvable, PUT fails) →
   set `repair_error` and include it in a **"playlists needing manual repair" report**
   (written to `/data/segmentation_playlist_repair.json` and surfaced on the Segmentation
   page). This guarantees no silent playlist breakage.

**Decision:** auto-repair is the primary path (feasible via the verified Plex playlist-item
API); the manual-repair report is the guaranteed fallback so nothing fails silently.
Smart/dynamic playlists (rule-based) are unaffected by ratingKey changes and need no repair.

---

## 10. Sweep cadence & scope (avoiding re-scan of classified tracks)

- **Where:** a new daemon-thread loop `_scheduled_live_holiday_loop()` added in
  `backend/main.py` lifespan, on the open **3 AM** slot (existing loops **[VERIFIED
  `main.py:24-131`]**: 1 AM full scan, 2 AM playlist sync, 4 AM fingerprint, station
  refresh; all are bare `while True: … time.sleep(...)` daemon threads). Do **not** add
  APScheduler/cron — match the existing pattern.
- **Gating:** run only when `segmentation_sweep_enabled` is `true`; **skip** if the 1 AM
  scan is still running (it mutates `tracks`) or a segmentation run is already in progress.
  Wrap each cycle in `try/except` that logs and continues — an uncaught exception in a bare
  daemon thread kills it silently; surface a `last_run`/heartbeat timestamp for the UI.
- **Incremental scope (don't re-examine already-classified tracks):**
  - Maintain a watermark in `settings` (`segmentation_last_sweep_ts`). Each nightly run
    evaluates only tracks with `tracks.scanned_at > watermark` (new/changed since last run),
    then advances the watermark.
  - Additionally exclude tracks already resolved: skip any `track_id` that already has a
    `segmentation_moves` row with `state='done'` and `rolled_back=0` (already moved), or a
    `segmentation_candidates` row with `status IN ('rejected','moved')` (already
    decided). A `status='rejected'` track is not re-proposed unless its metadata changes
    (its `scanned_at` advances past the watermark).
  - Tracks physically living under the Live/Holiday roots are inherently out of the sweep's
    section-5 scope, so they're not re-processed.
- **First bulk run:** same code path, unbounded (`watermark = NULL / 0`), run in **dry-run
  first**, reviewed, then applied. Bulk-apply guard rails: process in fixed batches (e.g.
  50), commit the ledger per batch, re-read the fail-closed kill switch between batches, and
  **abort the whole pass** (not skip one track) if collisions/sha-mismatches exceed a small
  bound (e.g. >2% or 5 consecutive failures) or the auto-move count wildly exceeds a sane
  estimate — a direct guard against the AudD bulk-retag incident (2,468 wrongly-changed
  tracks). Hold the process-wide mover lock so the sweep and the "apply approved" endpoint
  never race the same track.
- **After a batch with any moves:** the *worker* (not the mover) triggers **one**
  `trigger_plex_scan()` per affected new section, plus a **section-5 `refresh` then
  `emptyTrash`** (`PUT /library/sections/5/emptyTrash`) so the vacated track actually
  leaves the main index (a bare `refresh` only marks it trashed). Then run playlist repair
  (§9).

---

## 11. Cross-library search mitigation (confirmed)

The one real downside of splitting — "I can't find my live version by search" — is
mitigated and this is the confirmed mechanism:

- **Plexamp:** enable **Settings → Experience → "Search all Libraries"** so a single search
  spans Music + Live Performances + Holiday. A moved track is still found and can be added
  to any playlist without switching the active library.
- **Plex Server:** ensure each new library's **"Include in global search"** (a.k.a. include
  in dashboard/search) flag is on for the Live Performances and Holiday sections, so
  server-side global search also returns them.
- Plex playlists may mix tracks from multiple libraries of the same media type, so a moved
  track remains addable to the pool playlist etc.

**[VERIFY AT BUILD]** the exact current label of the Plexamp toggle and the per-library
server flag against the running Plex/Plexamp version on Beast (UI labels drift across
versions); confirm live rather than trusting this wording.

---

## 12. Known limitations

- **Pattern set is a starting point.** Expect to tune §5 against real false positives after
  the first bulk run. The review tier is the safety net by design — misclassifications land
  in the queue, not on disk.
- **AudD coverage is partial.** Tracks never run through AudD fall back to raw tags, which
  are noisier; genre signals only fire where `fingerprint_results.matched_genre` exists.
- **Genre = "Live" is unconfirmed.** Whether AudD emits a `"Live"` genre is
  **[VERIFY AT BUILD]**; if it doesn't, live detection is title-marker-only.
- **Playlist repair depends on unique file-path re-resolution.** If two tracks share a
  dest-equivalent path or Plex hasn't finished the target-section scan when repair runs,
  some pairs land in the manual-repair report rather than auto-repairing. Repair should run
  only after `wait_for_plex_scan()` confirms the target scan completed.
- **Manual Plexamp/Plex UI settings.** "Search all Libraries" and per-library search flags
  are one-time UI toggles on Beast, not code — a fresh Plex setup needs them re-enabled.
- **No audio-level live corroboration.** Ambiguous live titles stay in review; there is no
  crowd/applause detector in this scope.
- **SQLite concurrency.** A new writer thread joins existing daemon writers; rely on WAL
  (already set) + per-thread connections + the process-wide mover lock. **[VERIFY AT
  BUILD]** each loop opens its own connection.

---

## Corrections vs. prior draft (the 5 flagged issues)

| # | Issue | Resolved in |
|---|-------|-------------|
| 1 | Approval contradiction (dry-run-required vs. auto-apply-from-DB) | **§5C** ("Confidence-tier decision"), **§6** ("Dry-run report") — single policy: dry-run always; auto-move only high-confidence; everything else (incl. any weak holiday keyword) → review queue. |
| 2 | Over-broad holiday regex / false positives | **§5B** ("HOLIDAY / occasion") — album- and genre-anchored strong signals; weak single words (`winter`/`snow`/`bell`/`jingle`) never auto alone, only promote when co-occurring with a strong signal, else → review. |
| 3 | Missing DB path update after move | **§7** ("Move execution — ordered steps") — `tracks.file_path` update committed in the same transaction as the move; source unlinked only after commit; DB-update failure rolls back the file relocation. |
| 4 | Playlists silently lose tracks on ratingKey change | **§9** ("Playlist protection") — snapshot affected playlists before moving, auto-repair via `PUT /playlists/<id>/items`, manual-repair report as guaranteed fallback. **Reverses the prior draft's out-of-scope decision.** |
| 5 | No move ledger / undo for the first bulk run | **§3** (`segmentation_moves` schema) + **§8** ("Move ledger & undo") — append-only ledger with old/new ratingKey, sha, run_id; `reverse_move()` for single and whole-run undo. |
