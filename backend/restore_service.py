"""
restore_service.py — Non-destructive idempotent restore from file_txn journal (U9).

Protocol
--------
1. Read the op's journal from FILE_TXN_JOURNAL_DIR.
2. Verify the op type is trash or replace (replace restores the original from trash).
3. Re-hash the trashed file against the sha256 recorded in the journal.
   Hash mismatch → abort with RestoreResult(status='hash_mismatch').
4. If the restore destination is occupied:
   - Produce a conflict path: <name>.restored.<op_id8>.<ext>
   - Flag the result as needs_manual_merge=True.
5. Move the trashed file back to its original location (or conflict path).
6. Idempotent: if the trashed file is already gone AND the original path exists,
   treat as already-restored and return success.
7. Never overwrites an existing file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import file_txn


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class RestoreResult:
    op_id: str
    status: str           # restored | already_restored | hash_mismatch | aborted | error
    restored_to: Optional[Path] = None
    needs_manual_merge: bool = False
    detail: str = ""


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class RestoreError(Exception):
    """Fatal error during restore."""


# ---------------------------------------------------------------------------
# Core restore function
# ---------------------------------------------------------------------------

def restore_op(op_id: str) -> RestoreResult:
    """
    Restore the original file trashed by the given op.

    Returns RestoreResult — never raises except on truly unexpected errors.
    """
    records = file_txn._read_journal(op_id)
    if not records:
        return RestoreResult(
            op_id=op_id,
            status="aborted",
            detail=f"No journal found for op {op_id}",
        )

    first = records[0]
    op_type = first.get("op", "")
    if op_type not in ("trash", "replace"):
        return RestoreResult(
            op_id=op_id,
            status="aborted",
            detail=f"Op type '{op_type}' is not restorable",
        )

    # Find the quarantine record
    quarantine_rec = next(
        (r for r in records if r.get("state") == "original_quarantined"), None
    )
    if quarantine_rec is None:
        return RestoreResult(
            op_id=op_id,
            status="aborted",
            detail="Op never reached original_quarantined state — nothing to restore",
        )

    trash_dest_str = quarantine_rec.get("trash_dest")
    original_str = first.get("original") or first.get("path")
    journaled_sha256 = quarantine_rec.get("sha256_original") or first.get("sha256_original")

    if not trash_dest_str or not original_str:
        return RestoreResult(
            op_id=op_id,
            status="aborted",
            detail="Journal missing trash_dest or original path",
        )

    trash_dest = Path(trash_dest_str)
    original_path = Path(original_str)

    # ---------- Idempotency check ----------
    # If trash file is gone and original exists, treat as already restored
    if not trash_dest.exists() and original_path.exists():
        return RestoreResult(
            op_id=op_id,
            status="already_restored",
            restored_to=original_path,
            detail="Trashed file already absent and original path exists",
        )

    # If trash file is gone and original is also gone — unrecoverable
    if not trash_dest.exists():
        return RestoreResult(
            op_id=op_id,
            status="aborted",
            detail=f"Trashed file not found at {trash_dest}",
        )

    # ---------- Hash verification ----------
    if journaled_sha256:
        actual_sha256 = file_txn._sha256(trash_dest)
        if actual_sha256 != journaled_sha256:
            return RestoreResult(
                op_id=op_id,
                status="hash_mismatch",
                detail=(
                    f"Trashed file hash {actual_sha256} does not match "
                    f"journaled hash {journaled_sha256}"
                ),
            )

    # ---------- Determine restore destination ----------
    needs_manual_merge = False
    restore_dest = original_path

    if original_path.exists():
        # Occupied — produce conflict path, never overwrite
        stem = original_path.stem
        suffix = original_path.suffix
        op_id8 = op_id[:8]
        restore_dest = original_path.parent / f"{stem}.restored.{op_id8}{suffix}"
        # Ensure uniqueness (shouldn't collide but be safe)
        counter = 1
        while restore_dest.exists():
            restore_dest = original_path.parent / f"{stem}.restored.{op_id8}.{counter}{suffix}"
            counter += 1
        needs_manual_merge = True

    # ---------- Perform restore ----------
    try:
        restore_dest.parent.mkdir(parents=True, exist_ok=True)
        os.rename(str(trash_dest), str(restore_dest))
    except OSError as e:
        return RestoreResult(
            op_id=op_id,
            status="error",
            detail=f"os.rename failed: {e}",
        )

    # Append a record to the journal so repeated calls are idempotent
    file_txn._append_journal(op_id, {
        "state": "restored",
        "restored_to": str(restore_dest),
        "needs_manual_merge": needs_manual_merge,
    })

    return RestoreResult(
        op_id=op_id,
        status="restored",
        restored_to=restore_dest,
        needs_manual_merge=needs_manual_merge,
        detail="",
    )
