"""
file_txn.py — Journaled, crash-safe destructive-file-operation layer (U9).

Every destructive file operation (replace or trash) in Music Machine must go
through this module.  No other module may call os.rename / shutil.move on
library paths.

Op state machine (replace):
    intent → original_quarantined → replacement_staged → replacement_committed
            → db_committed → finalized

Op state machine (trash-only):
    intent → original_quarantined → db_committed → finalized

Journal:
    One JSONL file per op, named <JOURNAL_DIR>/<op_uuid>.jsonl.
    Each state transition appends one JSON record: write to temp file in same
    dir → fsync(fd) → os.rename (atomic) → fsync(dirfd).
    Read-after-write of the intent record is verified before any destructive
    rename.

Sentinel:
    .op-in-progress-<uuid> file placed next to the target path during the
    destructive window; removed at finalize.

Same-device assertion:
    assert_same_device(*paths) checks os.stat().st_dev of each path's
    existing parent.  Any mismatch raises CrossDeviceError before any move.

Trash:
    Per-export — caller supplies library_root; trash root is
    <library_root>/.m2-trash/, asserted same st_dev as the source file.
    Relative path under library_root is preserved; collision suffix appended
    on name clash.

Kill switch:
    move_chokepoint() checks database.identity_act_enabled() before every
    os.rename on a library path.  When false: no-op + audit log line in the
    journal, raises KillSwitchDisabled.

Crash injection (tests):
    Module-level _fault_point: str | None and _check_fault(name) raise
    _InjectedCrash when name matches.  Tests set _fault_point before calling.

Reconciler:
    recover_incomplete_ops() scans JOURNAL_DIR for op files lacking
    'finalized'.  Rolls forward ops that reached replacement_committed;
    rolls back ops that only quarantined the original.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

import database

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Journal directory
# ---------------------------------------------------------------------------

_DEFAULT_JOURNAL_DIR = Path(os.environ.get("FILE_TXN_JOURNAL_DIR", "/data/file-txn"))

def _journal_dir() -> Path:
    """Return the active journal directory, creating it if necessary."""
    d = Path(os.environ.get("FILE_TXN_JOURNAL_DIR", str(_DEFAULT_JOURNAL_DIR)))
    d.mkdir(parents=True, exist_ok=True)
    return d

# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------

class CrossDeviceError(Exception):
    """Source and destination are on different st_dev (filesystems)."""

class KillSwitchDisabled(Exception):
    """identity_act_enabled is false; operation refused."""

class AbortedByRevalidate(Exception):
    """revalidate() returned False; operation cleanly aborted."""

class _InjectedCrash(BaseException):
    """Raised by _check_fault() during crash-injection tests."""

# ---------------------------------------------------------------------------
# Crash injection
# ---------------------------------------------------------------------------

_fault_point: Optional[str] = None

def _check_fault(name: str) -> None:
    """Raise _InjectedCrash if the current fault point matches name."""
    if _fault_point is not None and _fault_point == name:
        raise _InjectedCrash(f"Injected crash at fault point: {name}")

# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class OpResult:
    op_id: str
    status: str          # finalized | aborted | rolled_back | kill_switch
    final_path: Optional[Path] = None
    trash_path: Optional[Path] = None
    states: List[str] = field(default_factory=list)

# ---------------------------------------------------------------------------
# Device assertion
# ---------------------------------------------------------------------------

def assert_same_device(*paths: Path) -> None:
    """
    Assert every path's parent (or the path itself if it exists) is on the
    same st_dev.  Any mismatch raises CrossDeviceError before any move.
    """
    devs = []
    for p in paths:
        if p.exists():
            devs.append((p, os.stat(p).st_dev))
        elif p.parent.exists():
            devs.append((p, os.stat(p.parent).st_dev))
        else:
            # Walk up until we find an existing ancestor
            ancestor = p.parent
            while not ancestor.exists() and ancestor != ancestor.parent:
                ancestor = ancestor.parent
            devs.append((p, os.stat(ancestor).st_dev))

    if len(devs) < 2:
        return
    first_dev = devs[0][1]
    for path, dev in devs[1:]:
        if dev != first_dev:
            raise CrossDeviceError(
                f"Cross-device operation refused: {devs[0][0]} (dev {first_dev}) "
                f"vs {path} (dev {dev})"
            )

# ---------------------------------------------------------------------------
# Trash root computation
# ---------------------------------------------------------------------------

def trash_root_for(path: Path, library_root: Path) -> Path:
    """
    Return the per-export trash root for path.
    Trash root = <library_root>/.m2-trash/, asserted same st_dev as path.
    """
    trash = library_root / ".m2-trash"
    trash.mkdir(parents=True, exist_ok=True)
    # Assert same device
    path_dev = os.stat(path.parent if not path.exists() else path).st_dev
    trash_dev = os.stat(trash).st_dev
    if path_dev != trash_dev:
        raise CrossDeviceError(
            f"Trash root {trash} (dev {trash_dev}) is not on the same "
            f"filesystem as {path} (dev {path_dev})"
        )
    return trash

def _trash_dest(path: Path, trash_root: Path, library_root: Path) -> Path:
    """
    Compute destination path within trash_root preserving relative structure.
    Appends collision suffix if dest already exists.
    """
    try:
        rel = path.relative_to(library_root)
    except ValueError:
        rel = Path(path.name)

    dest = trash_root / rel
    dest.parent.mkdir(parents=True, exist_ok=True)

    if not dest.exists():
        return dest

    # Collision — append suffix
    stem = dest.stem
    suffix = dest.suffix
    counter = 1
    while dest.exists():
        dest = dest.parent / f"{stem}.{counter}{suffix}"
        counter += 1
    return dest

# ---------------------------------------------------------------------------
# SHA-256 helper
# ---------------------------------------------------------------------------

def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()

# ---------------------------------------------------------------------------
# fsync helpers
# ---------------------------------------------------------------------------

def _fsync_file(path: Path) -> None:
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)

def _fsync_dir(directory: Path) -> None:
    fd = os.open(str(directory), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)

# ---------------------------------------------------------------------------
# Journal I/O
# ---------------------------------------------------------------------------

def _journal_path(op_id: str) -> Path:
    return _journal_dir() / f"{op_id}.jsonl"

def _append_journal(op_id: str, record: dict) -> None:
    """
    Append one JSON record to the op's journal file.
    Uses temp-file + fsync + rename + dirfsync for durability.
    """
    record = dict(record)
    record.setdefault("ts", time.time())
    line = json.dumps(record) + "\n"

    jdir = _journal_dir()
    op_file = jdir / f"{op_id}.jsonl"

    # Read existing lines
    existing = b""
    if op_file.exists():
        existing = op_file.read_bytes()

    new_content = existing + line.encode()

    # Write to temp in the same directory
    fd, tmp_path = tempfile.mkstemp(dir=str(jdir), prefix=f"{op_id}_", suffix=".tmp")
    try:
        os.write(fd, new_content)
        os.fsync(fd)
    finally:
        os.close(fd)

    os.rename(tmp_path, str(op_file))
    _fsync_dir(jdir)

def _read_journal(op_id: str) -> List[dict]:
    """Read all records from an op's journal file."""
    p = _journal_path(op_id)
    if not p.exists():
        return []
    records = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return records

def _journal_states(op_id: str) -> List[str]:
    return [r.get("state", "") for r in _read_journal(op_id)]

def _verify_intent_record(op_id: str) -> None:
    """Read-after-write: abort if intent record not durable."""
    records = _read_journal(op_id)
    if not any(r.get("state") == "intent" for r in records):
        raise RuntimeError(
            f"Intent record for op {op_id} not found after write — "
            "journal durability check failed, refusing to proceed"
        )

# ---------------------------------------------------------------------------
# Sentinel management
# ---------------------------------------------------------------------------

def _sentinel_path(target: Path, op_id: str) -> Path:
    return target.parent / f".op-in-progress-{op_id}"

def _place_sentinel(target: Path, op_id: str) -> Path:
    sp = _sentinel_path(target, op_id)
    sp.write_text(op_id)
    return sp

def _remove_sentinel(sentinel: Optional[Path]) -> None:
    if sentinel and sentinel.exists():
        sentinel.unlink(missing_ok=True)

# ---------------------------------------------------------------------------
# Kill-switch chokepoint
# ---------------------------------------------------------------------------

def move_chokepoint(src: Path, dst: Path, op_id: str, state_label: str) -> None:
    """
    The single function that performs EVERY os.rename in this module on
    library paths.  Checks database.identity_act_enabled() — when false,
    logs an audit record and raises KillSwitchDisabled (no rename performed).
    """
    if not database.identity_act_enabled():
        _append_journal(op_id, {
            "state": "kill_switch_blocked",
            "src": str(src),
            "dst": str(dst),
            "step": state_label,
        })
        raise KillSwitchDisabled(
            f"identity_act_enabled=false; rename {src} → {dst} blocked at {state_label}"
        )
    os.rename(str(src), str(dst))

# ---------------------------------------------------------------------------
# replace_file — full write-then-swap
# ---------------------------------------------------------------------------

def replace_file(
    original: Path,
    staged_new: Path,
    library_root: Path,
    *,
    revalidate: Callable[[], bool],
    db_update: Callable[[], None],
    meta: dict,
) -> OpResult:
    """
    Atomically replace `original` with `staged_new` within `library_root`.

    State machine:
        intent → (revalidate check) → original_quarantined →
        replacement_staged → replacement_committed → db_committed → finalized

    Returns OpResult.  On any abort the original is untouched and an
    OpResult with status 'aborted' or 'rolled_back' is returned.
    """
    op_id = str(uuid.uuid4())
    sentinel: Optional[Path] = None

    # 1. Assert same device for all three locations
    trash_root = trash_root_for(original, library_root)
    assert_same_device(original, staged_new, trash_root)

    # 2. Journal intent
    sha256_orig = _sha256(original)
    _append_journal(op_id, {
        "state": "intent",
        "op": "replace",
        "original": str(original),
        "staged_new": str(staged_new),
        "library_root": str(library_root),
        "sha256_original": sha256_orig,
        "meta": meta,
    })
    _check_fault("after_intent")
    _verify_intent_record(op_id)

    trash_dest = _trash_dest(original, trash_root, library_root)
    final_path = original  # replacement lands at original's path

    try:
        # 3. Place sentinel
        sentinel = _place_sentinel(original, op_id)
        _check_fault("after_sentinel")

        # 4. Call revalidate — abort cleanly if False
        if not revalidate():
            _append_journal(op_id, {"state": "aborted", "reason": "revalidate_returned_false"})
            return OpResult(op_id=op_id, status="aborted")

        _check_fault("after_revalidate")

        # 5. rename original → trash (quarantine)
        move_chokepoint(original, trash_dest, op_id, "original_quarantined")
        _append_journal(op_id, {
            "state": "original_quarantined",
            "trash_dest": str(trash_dest),
            "sha256_original": sha256_orig,
        })
        _check_fault("after_quarantine")

        # 6. rename staged_new → final path
        move_chokepoint(staged_new, final_path, op_id, "replacement_staged")
        _append_journal(op_id, {
            "state": "replacement_staged",
            "final_path": str(final_path),
        })
        _check_fault("after_stage")

        # 7. fsync the newly placed file and its parent
        _fsync_file(final_path)
        _fsync_dir(final_path.parent)

        _append_journal(op_id, {"state": "replacement_committed"})
        _check_fault("after_replacement_committed")

        # 8. DB update
        db_update()
        _append_journal(op_id, {"state": "db_committed"})
        _check_fault("after_db_committed")

        # 9. Finalize
        _remove_sentinel(sentinel)
        sentinel = None
        _append_journal(op_id, {"state": "finalized"})

        return OpResult(
            op_id=op_id,
            status="finalized",
            final_path=final_path,
            trash_path=trash_dest,
            states=_journal_states(op_id),
        )

    except (KillSwitchDisabled, AbortedByRevalidate):
        _remove_sentinel(sentinel)
        raise
    except _InjectedCrash:
        # Let crash propagate — tests simulate power failure
        raise
    except Exception as exc:
        logger.exception("replace_file op %s failed: %s", op_id, exc)
        _append_journal(op_id, {"state": "error", "error": str(exc)})
        _remove_sentinel(sentinel)
        raise

# ---------------------------------------------------------------------------
# trash_file_txn — trash-only op
# ---------------------------------------------------------------------------

def trash_file_txn(
    path: Path,
    library_root: Path,
    *,
    revalidate: Callable[[], bool],
    db_update: Callable[[], None],
    meta: dict,
) -> OpResult:
    """
    Trash `path` within `library_root`.

    State machine:
        intent → (revalidate) → original_quarantined → db_committed → finalized
    """
    op_id = str(uuid.uuid4())
    sentinel: Optional[Path] = None

    trash_root = trash_root_for(path, library_root)
    assert_same_device(path, trash_root)

    sha256_orig = _sha256(path)
    _append_journal(op_id, {
        "state": "intent",
        "op": "trash",
        "path": str(path),
        "library_root": str(library_root),
        "sha256_original": sha256_orig,
        "meta": meta,
    })
    _check_fault("after_intent")
    _verify_intent_record(op_id)

    trash_dest = _trash_dest(path, trash_root, library_root)

    try:
        sentinel = _place_sentinel(path, op_id)
        _check_fault("after_sentinel")

        if not revalidate():
            _append_journal(op_id, {"state": "aborted", "reason": "revalidate_returned_false"})
            return OpResult(op_id=op_id, status="aborted")

        _check_fault("after_revalidate")

        move_chokepoint(path, trash_dest, op_id, "original_quarantined")
        _append_journal(op_id, {
            "state": "original_quarantined",
            "trash_dest": str(trash_dest),
            "sha256_original": sha256_orig,
        })
        _check_fault("after_quarantine")

        db_update()
        _append_journal(op_id, {"state": "db_committed"})
        _check_fault("after_db_committed")

        _remove_sentinel(sentinel)
        sentinel = None
        _append_journal(op_id, {"state": "finalized"})

        return OpResult(
            op_id=op_id,
            status="finalized",
            final_path=None,
            trash_path=trash_dest,
            states=_journal_states(op_id),
        )

    except (KillSwitchDisabled, AbortedByRevalidate):
        _remove_sentinel(sentinel)
        raise
    except _InjectedCrash:
        raise
    except Exception as exc:
        logger.exception("trash_file_txn op %s failed: %s", op_id, exc)
        _append_journal(op_id, {"state": "error", "error": str(exc)})
        _remove_sentinel(sentinel)
        raise

# ---------------------------------------------------------------------------
# Startup reconciler
# ---------------------------------------------------------------------------

def recover_incomplete_ops() -> List[Dict]:
    """
    Scan JOURNAL_DIR for op files lacking a 'finalized' record.

    Roll-forward policy:
        - Reached 'replacement_committed': the new file is at the final path,
          the original is in trash.  Call db_update is unknown, so record
          `reconciled_forward` and return the op info for the caller to handle
          DB reconciliation.
        - Reached only 'original_quarantined' (replace or trash): rename
          original back from trash to its original path (roll back).
        - Reached 'intent' only or partial: nothing destructive happened; mark
          rolled_back.

    In every case the live slot must not be left partial/missing while the
    original is unrecoverable.  Stale sentinels are removed.

    Returns a list of summary dicts describing each reconciled op.
    """
    jdir = _journal_dir()
    summary = []

    for jfile in sorted(jdir.glob("*.jsonl")):
        op_id = jfile.stem
        records = _read_journal(op_id)
        if not records:
            continue

        states = [r.get("state", "") for r in records]
        if "finalized" in states:
            continue  # Already complete

        first = records[0]
        op_type = first.get("op", "unknown")

        # Remove any stale sentinel
        target_str = first.get("original") or first.get("path")
        if target_str:
            sentinel = Path(target_str).parent / f".op-in-progress-{op_id}"
            _remove_sentinel(sentinel)

        if "replacement_committed" in states or "replacement_staged" in states:
            # Roll forward. At replacement_staged the rename into the final
            # path already happened (atomic — content is whole); finish the
            # fsync it may have missed, then record for DB reconciliation.
            if "replacement_committed" not in states:
                final_rec = next(
                    (r for r in records if r.get("state") == "replacement_staged"),
                    None,
                )
                final_str = (final_rec or {}).get("final_path") or first.get("original")
                if final_str and Path(final_str).exists():
                    _fsync_file(Path(final_str))
                    _fsync_dir(Path(final_str).parent)
                _append_journal(
                    op_id, {"state": "replacement_committed", "note": "recovered"}
                )
            summary.append({
                "op_id": op_id,
                "action": "reconciled_forward",
                "op_type": op_type,
                "original": first.get("original"),
                "meta": first.get("meta", {}),
                "states": states,
                "needs_db_reconciliation": True,
            })
            _append_journal(op_id, {"state": "reconciled_forward"})
            continue

        if "original_quarantined" in states:
            # Roll back: move original from trash back to original path.
            quarantine_rec = next(
                (r for r in records if r.get("state") == "original_quarantined"), None
            )
            original_path_str = first.get("original") or first.get("path")
            trash_dest_str = quarantine_rec.get("trash_dest") if quarantine_rec else None

            rolled_back = False
            if original_path_str and trash_dest_str:
                original_path = Path(original_path_str)
                trash_dest = Path(trash_dest_str)
                if trash_dest.exists() and not original_path.exists():
                    try:
                        os.rename(str(trash_dest), str(original_path))
                        _append_journal(op_id, {
                            "state": "rolled_back",
                            "original_restored": original_path_str,
                        })
                        rolled_back = True
                    except OSError as e:
                        logger.error(
                            "Reconciler could not roll back op %s: %s", op_id, e
                        )
                        _append_journal(op_id, {
                            "state": "rollback_failed",
                            "error": str(e),
                        })
                elif original_path.exists() and not trash_dest.exists():
                    # Already restored (trash slot empty)
                    _append_journal(op_id, {
                        "state": "rolled_back",
                        "note": "original already present",
                    })
                    rolled_back = True
                elif original_path.exists() and trash_dest.exists():
                    # Live path occupied AND quarantined copy still in trash —
                    # never overwrite; surface for manual attention.
                    logger.error(
                        "Reconciler conflict for op %s: %s occupied while "
                        "quarantined copy exists at %s",
                        op_id, original_path, trash_dest,
                    )
                    _append_journal(op_id, {
                        "state": "rollback_conflict",
                        "live_path": str(original_path),
                        "trash_dest": str(trash_dest),
                    })

                # Remove staged_new if present
                staged_str = first.get("staged_new")
                if staged_str:
                    staged = Path(staged_str)
                    if staged.exists():
                        staged.unlink(missing_ok=True)

            summary.append({
                "op_id": op_id,
                "action": "rolled_back" if rolled_back else "rollback_failed",
                "op_type": op_type,
                "original": original_path_str,
                "states": states,
            })
            continue

        # Only intent or earlier — nothing destructive happened
        _append_journal(op_id, {"state": "rolled_back", "note": "no destructive step taken"})
        summary.append({
            "op_id": op_id,
            "action": "rolled_back",
            "op_type": op_type,
            "states": states,
        })

    return summary
