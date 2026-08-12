"""
correction_pass.py — Phase 2 of the Identity Resolver.

Reads the verdicts the sweep wrote into `track_identity` and, in apply mode,
corrects on-disk file tags to match the resolver's PICK — snapshotting the
original tags first so every change is reversible.

HYBRID policy (Blair, 2026-06-14):
  - AUTO-APPLY tier: state='conflict' AND tier='T1' (ISRC-proof) AND the PICK
    actually disagrees with the current tag. Unambiguous; applied without a
    per-track click.
  - REVIEW tier: any other conflict/review row becomes eligible only once a
    human approves it in the /identity UI (track_identity.reviewed_at set).

Safety:
  - dry_run=True is the DEFAULT — writes NOTHING, returns old->new diffs.
  - Real writes require identity_act_enabled=true (kill switch, enforced again
    inside tagger.write_metadata) AND go through tag_backup.snapshot_tags()
    first, so rollback_correction(id) / rollback_batch() fully revert.
  - Every applied change is logged in `identity_corrections` for audit.

This module performs NO writes on import. Apply is opt-in via apply=True or the
--apply CLI flag.
"""

import argparse
import logging
from dataclasses import dataclass, field

from database import get_db, identity_act_enabled
from tag_backup import rollback_tags, snapshot_tags
from tagger import write_metadata

logger = logging.getLogger(__name__)

# Fields the correction pass compares + writes. Cover art is intentionally left
# alone here — identity correction is about who/what the track is, not artwork.
_PICK_FIELDS = ("artist", "title", "album")


def ensure_schema() -> None:
    """Create the audit table if missing. Mirrors database.py's IF NOT EXISTS style."""
    with get_db() as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS identity_corrections (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                track_id     INTEGER NOT NULL REFERENCES tracks(id),
                snapshot_id  INTEGER,
                tier         TEXT,
                mode         TEXT NOT NULL CHECK(mode IN ('auto','review')),
                status       TEXT NOT NULL DEFAULT 'pending'
                                 CHECK(status IN ('pending','applied','failed')),
                old_artist   TEXT, old_title TEXT, old_album TEXT,
                new_artist   TEXT, new_title TEXT, new_album TEXT,
                sha_before   TEXT, sha_after TEXT,
                created_at   TEXT DEFAULT (datetime('now')),
                applied_at   TEXT,
                rolled_back  INTEGER DEFAULT 0,
                rolled_back_at TEXT
            )
            """
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_identity_corrections_track "
            "ON identity_corrections(track_id)"
        )


@dataclass
class Candidate:
    track_id: int
    file_path: str
    tier: str | None
    mode: str  # 'auto' | 'review'
    mb_recording_id: str | None
    current: dict   # current on-disk tag values (from tracks)
    pick: dict      # resolver PICK (from track_identity)
    fields: tuple = _PICK_FIELDS  # identity fields THIS batch is allowed to change

    @property
    def diff(self) -> dict:
        """Fields (restricted to self.fields) where the PICK differs from the tag."""
        out = {}
        for f in self.fields:
            new = self.pick.get(f)
            old = self.current.get(f)
            if new and (new != old):
                out[f] = (old, new)
        return out


@dataclass
class RunResult:
    dry_run: bool
    candidates: list = field(default_factory=list)
    applied: list = field(default_factory=list)
    skipped_no_diff: int = 0
    errors: list = field(default_factory=list)

    def summary(self) -> dict:
        return {
            "dry_run": self.dry_run,
            "candidates": len(self.candidates),
            "with_changes": sum(1 for c in self.candidates if c.diff),
            "skipped_no_diff": self.skipped_no_diff,
            "applied": len(self.applied),
            "errors": len(self.errors),
        }


def _row_to_candidate(row, mode: str, fields=_PICK_FIELDS) -> Candidate:
    return Candidate(
        track_id=row["track_id"],
        file_path=row["file_path"],
        tier=row["tier"],
        mode=mode,
        mb_recording_id=row["mb_recording_id"],
        current={
            "artist": row["cur_artist"],
            "title": row["cur_title"],
            "album": row["cur_album"],
        },
        pick={
            "artist": row["artist"],
            "title": row["title"],
            "album": row["album"],
            "date": row["date"],
            "track_number": row["track_no"],
        },
        fields=tuple(fields),
    )


# Column list shared by both selectors so candidates are built identically.
_SELECT_COLS = """
        SELECT ti.track_id, ti.tier, ti.mb_recording_id,
               ti.artist, ti.title, ti.album, ti.date, ti.track_no,
               t.file_path,
               t.artist AS cur_artist, t.title AS cur_title, t.album AS cur_album
        FROM track_identity ti JOIN tracks t ON t.id = ti.track_id
"""


def select_candidates(mode: str, limit: int | None = None,
                       fields=_PICK_FIELDS) -> list:
    """
    mode='auto'   -> conflict + tier T1 (provably wrong, no human click needed)
    mode='review' -> any conflict/review row a human has approved (reviewed_at set)
    """
    if mode == "auto":
        where = "ti.state = 'conflict' AND ti.tier = 'T1'"
    elif mode == "review":
        where = ("ti.state IN ('conflict','review') "
                 "AND ti.reviewed_at IS NOT NULL")
    else:
        raise ValueError(f"unknown mode {mode!r}")

    if limit is not None and limit <= 0:
        raise ValueError("limit must be a positive integer")

    sql = _SELECT_COLS + f" WHERE {where} AND t.status = 'active' ORDER BY ti.track_id"
    if limit:
        sql += f" LIMIT {int(limit)}"

    with get_db() as db:
        rows = db.execute(sql).fetchall()
    return [_row_to_candidate(r, mode, fields) for r in rows]


def select_by_ids(track_ids, fields=_PICK_FIELDS, mode: str = "review") -> list:
    """Build candidates for an explicit, externally-curated set of track_ids.

    Used for category batches (e.g. the concat-bug fix) where a separate
    read-only selector decides membership and this module only does the safe
    snapshot->write->log. Parameterized IN-clause — no string interpolation of ids.
    """
    ids = [int(i) for i in track_ids]
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    sql = (_SELECT_COLS
           + f" WHERE ti.track_id IN ({placeholders}) AND t.status = 'active'"
           + " ORDER BY ti.track_id")
    with get_db() as db:
        rows = db.execute(sql, ids).fetchall()
    return [_row_to_candidate(r, mode, fields) for r in rows]


def _apply_one(c: Candidate) -> dict:
    """Snapshot -> pending audit row -> write -> finalize.

    Caller guarantees the kill switch is on and c.diff is truthy. Order matters:
    the audit row is written BEFORE the file mutation so a successful write can
    always be traced back to its snapshot, and a write failure triggers an
    immediate rollback attempt (switch is still on) with the row marked failed.
    """
    snapshot_id = snapshot_tags(c.track_id, c.file_path, None)
    if snapshot_id is None:
        raise RuntimeError(f"snapshot failed for track {c.track_id} ({c.file_path})")

    with get_db() as db:
        cur = db.execute(
            """INSERT INTO identity_corrections
               (track_id, snapshot_id, tier, mode, status,
                old_artist, old_title, old_album,
                new_artist, new_title, new_album)
               VALUES (?,?,?,?,'pending',?,?,?,?,?,?)""",
            (
                c.track_id, snapshot_id, c.tier, c.mode,
                c.current.get("artist"), c.current.get("title"), c.current.get("album"),
                c.pick.get("artist"), c.pick.get("title"), c.pick.get("album"),
            ),
        )
        correction_id = cur.lastrowid

    # Write ONLY the fields that actually differ, so the on-disk change exactly
    # matches the diff we reported and logged. (Identity fields only — date /
    # track_number correction is a separate, later pass.)
    meta = {f: c.pick[f] for f in c.diff}
    try:
        sha_before, sha_after = write_metadata(
            c.file_path, meta, recording_id=c.mb_recording_id
        )
    except Exception:
        reverted = rollback_tags(snapshot_id)  # switch still on here
        with get_db() as db:
            db.execute(
                "UPDATE identity_corrections SET status='failed', rolled_back=?, "
                "rolled_back_at=CASE WHEN ?=1 THEN datetime('now') END WHERE id=?",
                (1 if reverted else 0, 1 if reverted else 0, correction_id),
            )
        raise

    with get_db() as db:
        db.execute(
            "UPDATE identity_corrections SET status='applied', "
            "applied_at=datetime('now'), sha_before=?, sha_after=? WHERE id=?",
            (sha_before, sha_after, correction_id),
        )
    return {"track_id": c.track_id, "correction_id": correction_id,
            "snapshot_id": snapshot_id, "diff": c.diff}


def run(mode: str = "auto", dry_run: bool = True, limit: int | None = None,
        track_ids=None, fields=_PICK_FIELDS) -> RunResult:
    """
    Select candidates (by mode, or by an explicit track_ids set), then either
    report (dry_run) or apply. Apply refuses unless identity_act_enabled is true.
    `fields` restricts which identity fields may change/write (e.g. artist only).
    """
    ensure_schema()
    res = RunResult(dry_run=dry_run)
    if track_ids is not None:
        res.candidates = select_by_ids(track_ids, fields=fields, mode=mode)
    else:
        res.candidates = select_candidates(mode, limit=limit, fields=fields)

    if not dry_run and not identity_act_enabled():
        raise RuntimeError(
            "identity_act_enabled is false — flip the kill switch to apply"
        )

    for c in res.candidates:
        if not c.diff:
            res.skipped_no_diff += 1
            continue
        if dry_run:
            continue
        try:
            res.applied.append(_apply_one(c))
        except Exception as e:  # one bad file must not abort the batch
            logger.error("correction failed for track %s: %s", c.track_id, e)
            res.errors.append({"track_id": c.track_id, "error": str(e)})

    return res


def rollback_correction(correction_id: int) -> bool:
    """Revert a single applied correction via its tag snapshot.

    NOTE: rollback writes tags too (via tag_backup.rollback_tags ->
    write_metadata), so it requires identity_act_enabled=true. We pre-check so a
    disabled switch fails loudly instead of as an opaque ValueError mid-restore.
    """
    if not identity_act_enabled():
        logger.error(
            "cannot roll back correction %s: identity_act_enabled is false — "
            "re-enable the kill switch first (rollback rewrites tags)",
            correction_id,
        )
        return False
    with get_db() as db:
        row = db.execute(
            "SELECT snapshot_id, rolled_back FROM identity_corrections WHERE id=?",
            (correction_id,),
        ).fetchone()
    if not row:
        logger.warning("correction %s not found", correction_id)
        return False
    if row["rolled_back"]:
        return True
    if row["snapshot_id"] is None:
        logger.error("correction %s has no snapshot — cannot roll back", correction_id)
        return False

    if not rollback_tags(row["snapshot_id"]):
        return False
    with get_db() as db:
        db.execute(
            "UPDATE identity_corrections "
            "SET rolled_back=1, rolled_back_at=datetime('now') WHERE id=?",
            (correction_id,),
        )
    return True


def rollback_batch(correction_ids: list) -> dict:
    ok, fail = [], []
    for cid in correction_ids:
        (ok if rollback_correction(cid) else fail).append(cid)
    return {"rolled_back": ok, "failed": fail}


def _print_report(res: RunResult, mode: str) -> None:
    s = res.summary()
    print(f"\n=== correction_pass mode={mode} dry_run={res.dry_run} ===")
    print(f"candidates={s['candidates']}  with_changes={s['with_changes']}  "
          f"no_diff={s['skipped_no_diff']}  applied={s['applied']}  errors={s['errors']}\n")
    shown = 0
    for c in res.candidates:
        if not c.diff:
            continue
        print(f"[{c.tier or '-'}] track {c.track_id}  {c.file_path}")
        for fld, (old, new) in c.diff.items():
            print(f"    {fld:7} {old!r}  ->  {new!r}")
        shown += 1
        if shown >= 50 and res.dry_run:
            print(f"    ... ({s['with_changes'] - shown} more not shown)")
            break


def main() -> None:
    ap = argparse.ArgumentParser(description="Identity correction pass")
    ap.add_argument("--mode", choices=["auto", "review"], default="auto")
    ap.add_argument("--apply", action="store_true",
                    help="actually write tags (default is dry-run)")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--fields", default="artist,title,album",
                    help="comma list of identity fields eligible to change")
    ap.add_argument("--ids-file",
                    help="file of track_ids (whitespace-separated) to scope the batch")
    ap.add_argument("--rollback", type=int, metavar="CORRECTION_ID")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if args.rollback is not None:
        ok = rollback_correction(args.rollback)
        print(f"rollback correction {args.rollback}: {'OK' if ok else 'FAILED'}")
        return

    fields = tuple(f.strip() for f in args.fields.split(",") if f.strip())
    bad = set(fields) - set(_PICK_FIELDS)
    if bad:
        ap.error(f"--fields must be within {_PICK_FIELDS}; got unknown {sorted(bad)}")

    track_ids = None
    if args.ids_file:
        with open(args.ids_file) as fh:
            tokens = [x for x in fh.read().split() if x.strip()]
        try:
            track_ids = [int(x) for x in tokens]
        except ValueError as e:
            ap.error(f"--ids-file {args.ids_file} has a non-integer token: {e}")
        if not track_ids:
            ap.error(f"--ids-file {args.ids_file} contained no track_ids")

    res = run(mode=args.mode, dry_run=not args.apply, limit=args.limit,
              track_ids=track_ids, fields=fields)
    _print_report(res, args.mode)


if __name__ == "__main__":
    main()
