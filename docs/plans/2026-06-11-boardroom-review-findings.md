# ADVERSARIAL PLAN REVIEW — Music Machine (M²) Identity Resolver, Phase 1

**Lens for every finding:** 100% precision on automatic actions. Never write a wrong tag, never trash/overwrite a correct original, never place a wrong or fake file. *Slowness* and *do nothing* are always acceptable. Any path where one bug, race, partial failure, crash, or silent degradation can cause an irreversible wrong write is high severity.

The plan's architecture is defensible — resolve-only sweep, identity-gated dedup, staged placement, reversible trash, "do nothing" as a first-class outcome. But as written it asserts atomicity, durability, and mutual exclusion that NFS does not provide, and it gates *irreversible* actions on identity signals weaker than the invariant tolerates. Findings are severity-ordered; each carries a concrete ordered scenario and a fix tied to a named Unit/file.

---

## CRITICAL

### 1. Non-atomic cross-device move destroys the original before the replacement is durably in place
**Scenario:** U8 placement is `stage temp → verify → manifest-trash original → move → update DB`. If the temp is staged on `/tmp`, a container layer, or a different NFS export than the destination, the final "move" is not `rename(2)` — the kernel returns `EXDEV` and glibc/`shutil` silently falls back to copy-then-unlink. A crash (power, OOM, `ESTALE`, container stop) mid-copy leaves a **partial/corrupt file at the live destination** while the original has *already* been trashed in the prior step. The slot holds garbage; the only good copy is in trash, recoverable only if the manifest survived (#3). U7 dedup has the same hazard if `/music` unions multiple exports and `.trash` is not co-located with the file being trashed.
**Fix (`backend/upgrader.py`, `backend/dedup.py`):** Invert to **write-then-swap on one filesystem**: (1) write new file to `<dest>/.staging/…`, (2) `fsync` file + parent dir, (3) `rename` original → `.trash/` (atomic, same export), (4) `rename` staging → final path, (5) update DB, (6) write manifest as audit log. Resolve the destination device via `st_dev`/`stat -f` and **assert staging, destination, and trash share one export** before any move; any cross-device result → route to review / do nothing. Trash is **per-export** (one `.trash` per mount, chosen by the file's own `st_dev`), never a single global `/music/.trash`.

### 2. Crash between file-move and DB-update silently diverges disk from DB; no journal/reconciler exists
**Scenario:** Placement and dedup both end `… → move → update DB`. Crash after the destructive move but before the DB write: disk holds the new file (or an empty slot), DB still references the old path/`sha256_original`/pre-placement `state`. Nothing on restart reconciles this; a later dedup or re-sweep reasons over a DB that disagrees with disk and can trash or re-place incorrectly. Symmetric failure: crash after trashing original but before DB update leaves a dangling DB reference.
**Fix (`backend/services/file_txn.py`):** Make placement and dedup an **idempotent journaled state machine** with a `.placement-in-progress` / `.replacing` sentinel written before step 1 and removed after the final DB commit. Persist explicit op states (`intent → original_quarantined → replacement_staged → replacement_committed → db_committed → finalized`), `fsync` file then parent dir at each step. On lifespan startup, a **recovery reconciler replays/rolls-forward or rolls-back any operation lacking a completion marker before any worker is allowed to run**. Invariant: filesystem reality is always ahead-of-or-equal-to DB state.

### 3. "Intent-first manifest" has no durability/atomicity semantics; on NFS the reversibility guarantee silently breaks
**Scenario:** Two modes break "reversible." (a) **No fsync** — NFS `write`+`close` only gives close-to-open consistency; manifest → `rename(original→trash)` → crash → manifest bytes lost on client cache while the destructive rename committed → **file in trash with no manifest record, DB pointing at an empty path = silent data loss.** (b) **Read-modify-write of one JSON array** — a crash mid-rewrite, or concurrent dedup+upgrade writers, corrupts the *entire* manifest, destroying restore info for *all* prior operations.
**Fix (`backend/services/trash_manifest.py`):** Manifest is **append-only JSONL, one record per trashed object** (named by op UUID / content hash) — never a shared RMW array. Per destructive op: write record to a temp file in the same dir, `fsync(fd)`, `rename`, `fsync(dirfd)`, **then** perform the rename. Embed a payload checksum + transaction UUID; verify read-after-write before the destructive step and reject the op if durability verification fails. Add a startup reconciler that quarantines any `.trash` file lacking a manifest record.

### 4. SQLite (and manifest) possibly on NFS — corrupts the very metadata that makes actions reversible
**Scenario:** The plan never states where the DB lives. SQLite's POSIX advisory locking over NFS is documented-unreliable; with sweep, dedup, and upgrade workers all touching the DB, a lock failure corrupts the database holding `track_identity`, manifest pointers, `sha256_original`, and upgrade state. Losing that DB turns "reversible" trash and "loss-proof" placement into irrecoverable states.
**Fix:** Mandate **SQLite and the JSONL manifest on local disk, never the NFS mount**; **WAL mode with a single serialized writer** (one connection/queue for all workers). If colocation with `/music` is unavoidable, document it as a blocking risk.

### 5. Chromaprint cross-similarity ≥0.75 fallback admits a wrong master/mix, then launders it into `confirmed`
**Scenario:** U8 accepts an upgrade as same-recording on same `mb_recording_id` **OR** `chromaprint_cross ≥0.75 + duration ±3s`. 0.75 means 25% of the fingerprint differs — squarely within the band where **different masters/remasters/mixes of the same performance** match. Concretely: user holds a 2015 remaster (3:45); usenet post is the 1987 original mix (3:42) — chromaprint ≈0.82, duration Δ 3s within window → **system replaces the remaster with the wrong mix**, irreversibly. Worse, U8 then "copies the identity row to the upgraded track" — a fallback-grade match is stamped **`confirmed`**, and future dedup trashes *other* copies based on this laundered confirmation. One weak match → cascading wrong writes.
**Fix (`backend/upgrader.py` identity gate):** For Phase 1, **remove the chromaprint fallback from the automatic placement path entirely.** Auto-place only when the *downloaded file independently resolves to the same confirmed `mb_recording_id`*; otherwise return `REVIEW` (human decides), surfacing the similarity score as advisory UI evidence only. Never inherit `confirmed` from the original — the upgraded row's state must reflect **how the new file was matched**, recorded as match provenance. (Chromaprint identifies *compositions*, not *recordings*; it cannot prove same-master at any home-tunable threshold.)

### 6. Irreversible placement gated on beatable `lossless_detect` from a ~40%-fake source
**Scenario:** `lossless_detect` is spectral heuristics; transcode-then-FLAC and masked-cutoff fakes evade it (false negative). MusicGrabber is explicitly ~40% fake. A fake passes `lossless_detect`, matches via the weak fallback (#5), and is placed over a real lossless original.
**Fix (`backend/services/upgrade_validator.py`):** For Phase 1, **disable auto-placement from untrusted/fallback sources** (MusicGrabber). Restrict auto-placement to strong-provenance sources with same-`mb_recording_id` proof; everything else → review queue. Keep `lossless_detect` as an **advisory signal, never the gate of truth**; require it to PASS *and* a second independent integrity signal before any auto-place.

### 7. `identity_sweep_active` boolean is a TOCTOU / split-brain control; NFS locks are unreliable
**Scenario:** Dedup reads flag=false; sweep sets it true a moment later; both run. Or a crash leaves the flag stuck (blocks forever — safe-but-degraded, see #12) or a lifespan reset clears it while a detached sweep keeps running → dedup runs concurrently with sweep, trashing a file while identity rows are still being rewritten. `flock`/`fcntl` over NFS depend on NLM/lockd (frequently silently local-only); `O_EXCL` lockfiles have real NFSv3 races.
**Fix (`backend/storage/job_locks.py`):** Replace the boolean with a **lease-based lock row + fencing token in local SQLite**: `job_locks(job_name, owner_id, lease_expires_at, fence)`. Acquire via `BEGIN IMMEDIATE` compare-and-set, renew via heartbeat, **carry the fence in every mutating write**, and re-assert lease ownership at each critical phase (pre-trash, pre-place). A zombie/resurrected worker that lost its lease self-aborts. Lock state is DB-resident on local disk, never an NFS lockfile.

### 8. Identity re-validated at *download* time, not at the *destructive* step — wide TOCTOU window
**Scenario:** U8 re-validates `status='active' AND state='confirmed'` "at download time." Usenet fetch + par2 repair can take hours/days; during it a re-sweep can revise the row to `review`/`conflict`. The worker then trashes the original and places the upgrade against a confirmation that **no longer holds**. Separately, the file hashed at approval may have changed (concurrent dedup move, an unguarded scan write, NFS stale read) — `sha256_original` is never re-checked.
**Fix (`backend/upgrader.py`):** Re-validate `status='active' AND state='confirmed'` **again immediately before the destructive trash-and-move, inside the same lease/transaction** that performs the placement. Also **re-compute `sha256_current` at download time and again pre-placement**, comparing to `sha256_original`; on any mismatch, abort → `review`, leaving the original untouched.

### 9. Kill-switch gates 4 high-level call sites but five independent writers exist — gate must be at the lowest primitive
**Scenario:** Context names **five** auto-writers; U1 gates four paths (`_auto_fix_track`, 2 AM playlist sync, `tagger.tag_file`, upgrade endpoints). Any writer (manual retag endpoint, import hook, maintenance task) that reaches a lower-level move/tag primitive *not* funneled through `tagger.tag_file` is unguarded and fires on restart, re-introducing wrong labels during the freeze. That the team had to **stop the container** to halt mislabeling is itself evidence the runtime gate isn't trusted as complete.
**Fix:** Move the gate **down to the single lowest-level tag-write and file-move primitives** — one chokepoint every writer must traverse, which **fails closed** (raises/no-ops + audit-logs) when `identity_act_enabled` is false. Add a static test/audit proving no module performs a tag-write or file-move except through that chokepoint. High-level gating is necessary but not the enforcement point.

---

## HIGH

### 10. Restore is non-idempotent and can clobber a newer correct file
**Scenario:** File `A.flac` is trashed by dedup. Later the user imports a new correct `A.flac` (or an upgrade replaces A). A naive restore replays the manifest and overwrites the live correct file — or recreates a duplicate the system immediately re-trashes. Interleaved dedup-then-upgrade makes the original path ambiguous.
**Fix (`backend/services/restore_service.py`):** Restore is **non-destructive and idempotent**: if the destination exists, restore to a conflict/quarantine path (`A.restored.<txn>.flac`) and flag `needs_manual_merge` — **never overwrite**. Manifest stores original absolute path + `sha256` + **inode/device id**; restore re-hashes the trashed file and verifies before any placement, aborting on mismatch. Test restore across an interleaved dedup→upgrade sequence.

### 11. Heartbeat false-positive spawns a second, unfenced sweep
**Scenario:** An NFS server pause makes the live sweep miss heartbeats; the monitor declares it dead and starts sweep #2; the original unblocks → two writers to `track_identity`, last-writer-wins flapping, and dedup may read a transient wrong state.
**Fix:** Fence with the **epoch/lease token from #7**: a resurrected sweep verifies it still holds the current lease before *every* DB write and exits if revoked; monitor restart **bumps the epoch** to fence the old worker out.

### 12. Stale lock permanently blocks all operations (no expiry/override)
**Scenario:** The sweep is OOM-killed or loses NFS connectivity; the `identity_sweep_active` flag stays set. Dedup/upgrades/1 AM scan are blocked forever with no documented timeout or manual override.
**Fix:** The lease row carries `last_heartbeat`; updated every N≈30s. A checker treats it stale when `now − last_heartbeat > 2N` **and** the stored PID is dead (`kill -0`). Provide `cli.py /force-release-sweep-lock` (logs a warning, requires confirmation). (This is the "do nothing safely" direction — blocking is acceptable; just make it recoverable.)

### 13. No mutex between dedup and upgrade on the same recording — keeper can be destroyed under dedup
**Scenario:** `identity_sweep_active` only gates sweep vs. {dedup,upgrade}. Dedup picks keeper A for `mb_recording_id=X`, trashes loser B; concurrently the upgrade worker targets A for replacement, trashes A, places D. Result: B trashed *and* its keeper A independently destroyed — the invariant "every trashed file has a surviving same-recording keeper" is violated.
**Fix (`backend/dedup.py`, `backend/upgrader.py`, `backend/models.py`):** Add a **per-`mb_recording_id` action lock** (DB advisory lock / `active_operation` column). Both dedup and upgrade must acquire it before any file movement for that recording; if held, **defer, do not proceed**.

### 14. Dedup TOCTOU on keeper confirmation + shared-recording-id alone too weak for irreversible trash
**Scenario:** Dedup selects keeper (confirmed) + losers sharing its `mb_recording_id`; between selection and the trash rename a concurrent re-sweep flips the keeper to `review`. Dedup trashes losers on a retracted confirmation. Semantics: two files can share a recording id yet be different masters/mixes/editions; if `quality_score` reuses the legacy completeness-biased formula, dedup trashes the *better* file.
**Fix (`backend/dedup.py`):** Snapshot keeper+losers under the action lock and **re-assert `state='confirmed'` on the keeper immediately before each trash, same transaction**. Restrict auto-trash to a **strict duplicate class** — identical decoded audio hash/fingerprint + duration + channels + sample-rate (or exact file hash). Same recording but non-identical audio → `review`, never auto-trash. Define `quality_score` independently of the legacy confidence formula; demote fake-FLAC via `lossless_detect` as advisory.

### 15. External writers (Lidarr/SAB/1 AM scan) mutate paths during verify→place
**Scenario:** Upgrade verifies a staged candidate against the original path; Lidarr imports/renames the same file before placement commits; the system trashes/replaces the wrong file on a stale pathname.
**Fix:** Configure Lidarr/SAB to write **into an inbox only**; M² alone performs library placement. Acquire the per-track action lock (#13) before any FS mutation, and **re-`stat`+re-hash the original immediately before trash/replace, aborting on mismatch** (ties to #8).

### 16. Migration parks only `approved`; in-flight upgrade rows get placed by the *old* pipeline on restart
**Scenario:** Container stopped mid-upgrade with rows `downloading`/`staged`/`verifying`. Lifespan migration parks `approved → frozen` but is silent on the rest. On restart a resumable worker picks up a `staged` row produced by the legacy unverified flow and places it without the new identity/lossless gates. Also: workers may start before the migration finishes.
**Fix:** Migration parks **every non-terminal upgrade state** (`downloading`, `staged`, `verifying`, `approved`) → `frozen`/quarantine, requiring re-entry through the new verification pipeline from scratch. Run it in a **blocking startup phase inside a transaction, with a version marker that workers check before they may start.**

### 17. 1 AM "read-only" scan may write and may re-import freshly trashed files
**Scenario:** U1 lets the 1 AM scan keep running; U7 requires it to skip dot-dirs — but if the dot-dir skip ships in a *later* release, the scan walks `.trash`, "discovers" the trashed file as new, and re-enqueues/re-tags it, undoing dedup. Scanners also commonly write last-seen/fingerprint caches or auto-create `track_identity` rows, so "read-only" may be a misnomer that pollutes the sweep's input.
**Fix (`backend/scanner.py`):** **Ship the dot-dir skip in the same release that enables any trash flow**, gate the scan behind the same lease so it can't run during trashing, and **audit every scanner write path** — route discovered metadata to a separate `scan_staging` table the resolver merges only when the kill-switch is on, or gate those writes under the kill-switch. Add a startup self-check that the trash root is excluded; fail closed otherwise.

### 18. T1 (ISRC + duration) auto-confirms wrong recording on ISRC reuse/multi-mapping
**Scenario:** Two MB recordings share/reuse an ISRC (or upstream data is wrong); durations within ±3s → T1 returns `confirmed` → dedup/upgrade act destructively on the wrong identity.
**Fix (`backend/resolver.py`):** T1 requires a **unique MB recording after ISRC lookup**. If the ISRC maps to >1 recording, downgrade to `review`/`conflict` unless independent evidence (exact artist-credit + release, high-confidence fingerprint) disambiguates.

### 19. Duration compared in truncated seconds — boundary false-confirms and unstable veto edges
**Scenario:** U2 has `mb_local` return seconds (MB stores ms). Two different recordings at 182.9s and 183.1s both truncate to 183s → perfect false match. The dead-band boundary becomes rounding-dependent: 3499ms→±3s (confirm) but 3501ms→±4s (dead-band). T2 uses AudD's `duration_ms`; mixing truncated and rounded sides shifts a track across the window.
**Fix (`backend/normalize.py`, `backend/resolver.py`, `backend/mb_local.py`):** Compare durations in **milliseconds end-to-end** (convert to seconds only at display). Local duration is **decoded audio length (ffprobe/sample count), never the stored tag** (the tag may have been written by the legacy mislabeler). Windows: confirm `≤3000ms`, dead-band `3000 < Δ ≤ 4000`, hard veto `>4000` — closed/open inequalities pinned, with boundary unit tests. Duration may only **confirm**, never **establish**, identity.

### 20. T2 treats AcoustID and AudD as independent witnesses — correlated errors → false `confirmed`
**Scenario:** Both sources derive from MusicBrainz/AcoustID-adjacent corpora; on a mislabeled track they can be wrong *the same way*, so their "agreement" is not two independent confirmations.
**Fix (`backend/resolver.py`):** Verify AudD's actual provenance; if it shares lineage with AcoustID, **downgrade T2 to `review` unless corroborated by a genuinely independent signal** (ISRC-in-MB, or a tight *decoded*-duration match against MB canonical). Document and test the independence assumption.

### 21. Ambiguity veto runs on the wrong candidate set and checks only artist
**Scenario:** U2 keeps candidates ≥0.3 "for veto visibility" but only ≥0.5 "participate in tiers." If the top-2 veto considers only ≥0.5 participants, a strong competing wrong-artist candidate at 0.48 is invisible → false confirm. Conversely the veto checks *artist* difference only: two different *recordings* by the same artist (0.72 vs 0.69, same artist) pass with no veto.
**Fix (`backend/resolver.py`):** Run all conflict/ambiguity vetoes against **all retained candidates (≥0.3)**, and assert by test that veto evaluation **strictly precedes tier assignment**. Fire the veto when the top-2 **`mb_recording_id` values differ** and scores are within 0.05, *regardless of artist*. A same-recording-id candidate from a different source should *strengthen*, not weaken, confidence.

---

## MEDIUM / MINOR

### 22. Kill-switch has no cooperative cancellation — true→false doesn't stop in-flight workers
**Scenario:** User flips the switch during a 4 AM run; workers already past the gate keep tagging/moving. The "switch" bounds new *runs*, not active mislabeling — which is why the container had to be stopped.
**Fix:** Long-running gated workers re-check the flag at each unit of work via a cooperative cancellation token, and re-check immediately before each irreversible step (consistent with #8). Document that the switch bounds new actions within one work-unit.

### 23. Budget-truncated/partial sweep feeds dedup over an incomplete view
**Scenario:** A budget-stopped sweep clears the lease while many tracks are still `deferred`/`unknown`; dedup runs over a partial identity view, and a later sweep can revise identities *after* dedup already acted.
**Fix (`backend/dedup.py`):** Dedup requires its candidate set to be **terminally resolved** (no pending `deferred`/`unknown`) and re-checked under lock before acting.

### 24. State/tier spec is incomplete (missing T3; six states lack transition rules)
**Scenario:** Tiers are T1, T2, **T4** — no T3; six states (`confirmed/review/unknown/conflict/deferred/error`) have no documented transition map → inconsistent application.
**Fix (`backend/resolver.py`):** Resolve the T3 gap (typo vs. removed tier) and document every state transition explicitly before implementation.

### 25. New `normalize.py` can drift from the three pinned legacy normalizers at decision boundaries
**Scenario:** The resolver normalizes artist/title differently than the legacy tag/search modules; T4 "AcoustID≈tags" then matches inconsistently, manufacturing unstable confidence.
**Fix (`backend/normalize.py`):** Add a **differential test corpus** (unicode, diacritics, punctuation, "feat."). For any field used in a *decision*, the resolver uses the canonical normalizer only; legacy normalizers remain read-only compatibility shims until retired.

### 26. No pre-run backup/checkpoint before destructive windows
**Scenario:** A latent manifest/dedup bug or operator error quarantines/replaces many files with no known-good snapshot for fast recovery.
**Fix (ops/runbook + scheduler):** Require a **fresh pre-run snapshot** (local SQLite backup + filesystem hardlink/rsync snapshot) before dedup/upgrade windows; the job **refuses to start without a recent backup marker**.

### 27. Dot-dir trash exclusion is single-layer and brittle
**Scenario:** A future scanner or config change indexes hidden dirs; trashed files re-enter the pipeline.
**Fix:** Defense-in-depth exclusion: scanner config **plus** an app-level path denylist **plus** a `.nomedia`/ignore marker in each trash root, with a fail-closed startup self-check.

---

## VERDICT

**NOT READY — required changes before implementation.** The intent is sound and "do nothing is acceptable" is correctly load-bearing, but the plan asserts atomicity, durability, and mutual exclusion that NFS does not provide and gates irreversible actions on identity proof weaker than the 100%-precision invariant tolerates. None of the fixes below trades precision for throughput — they all move toward "slower, or do nothing."

**Three blocking issue clusters that gate readiness:**

1. **Crash-/atomicity-unsafe destructive flows (#1–#4, #10).** Cross-device renames, an un-fsync'd/RMW manifest, SQLite+manifest possibly on NFS, DB-last ordering with no reconciler, and non-idempotent restore. *Required:* same-export write-then-swap with `st_dev` assertions; append-only fsync'd JSONL manifest with a startup reconciler; DB+manifest pinned to local disk (WAL, single writer); idempotent journaled placement/dedup state machine with crash recovery on lifespan startup; non-destructive verified restore.

2. **Identity-laundering on weak proof (#5, #6, #20, #18).** The chromaprint-0.75 fallback admits wrong masters, MusicGrabber+`lossless_detect` admits fakes, and the result is stamped `confirmed` and propagated to dedup. *Required:* strong-`mb_recording_id`-only for any irreversible placement (fallbacks → review), no inheritance of `confirmed`, provenance recorded on the upgraded row, AcoustID/AudD independence verified before trusting T2, unique-recording requirement on T1.

3. **Worker mutual exclusion & lifecycle (#7, #8, #9, #11–#13, #16).** The `identity_sweep_active` boolean is a TOCTOU with NFS-unreliable locking; heartbeats can spawn an unfenced second sweep; identity is re-validated too early; no dedup↔upgrade mutex; the kill-switch gates the wrong layer; the `frozen` migration leaves in-flight rows for the old pipeline. *Required:* a DB-resident CAS lease with epoch fencing and stale-lock recovery, per-recording action locks, re-validation (state + sha256) inside the destructive transaction, the gate relocated to the lowest write/move primitive (fail-closed), and a migration that parks **all** non-terminal upgrade states behind a blocking version marker.

Address these three clusters and fold in the remaining HIGH items, and the plan becomes **ready-with-required-changes**. Until then, an adversary controlling NFS timing, external-API responses, and process lifecycle has multiple distinct paths to a single irreversible wrong write.