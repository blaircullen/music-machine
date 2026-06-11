"""
job_locks.py — Lease-based job locks and per-recording action locks (U9).

All state is DB-resident (local SQLite via database.get_db) — no NFS lockfiles.

Tables
------
job_locks(
    job_name TEXT PRIMARY KEY,
    owner_id TEXT,          -- "host:pid:uuid"
    epoch INTEGER,
    lease_expires_at TEXT,  -- ISO-8601 UTC
    last_heartbeat TEXT     -- ISO-8601 UTC
)

recording_locks(
    mb_recording_id TEXT PRIMARY KEY,
    owner_id TEXT,
    acquired_at TEXT        -- ISO-8601 UTC
)

Job lock protocol
-----------------
acquire_job() uses BEGIN IMMEDIATE compare-and-set:
  - Row absent → insert with epoch=1.
  - Row present, NOT expired → return None (held by another owner).
  - Row present, expired AND stale (last_heartbeat > 2× lease AND pid dead or
    remote host) → reclaim with epoch+1.

Epoch fencing: assert_lease() verifies owner_id+epoch are still current before
every fenced write.  heartbeat() renews only if owner+epoch still match.

Owner ID format "host:pid:uuid" — kill 0 check only when host matches the
current host; otherwise treat an expired lease as reclaimable.
"""

from __future__ import annotations

import os
import platform
import signal
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import database


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class LeaseLost(Exception):
    """Owner/epoch mismatch — lease was taken by another owner."""


class RecordingLockHeld(Exception):
    """Per-recording lock is held by another owner."""


# ---------------------------------------------------------------------------
# Lease dataclass
# ---------------------------------------------------------------------------

@dataclass
class Lease:
    job_name: str
    owner_id: str
    epoch: int
    lease_seconds: int


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _expire_iso(lease_seconds: int) -> str:
    from datetime import timedelta
    return (datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)).isoformat()


def _is_expired(lease_expires_at: str) -> bool:
    try:
        exp = datetime.fromisoformat(lease_expires_at)
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) > exp
    except (ValueError, TypeError):
        return True


def _is_stale(last_heartbeat: str, lease_seconds: int) -> bool:
    """True if last heartbeat is older than 2× the lease period."""
    try:
        hb = datetime.fromisoformat(last_heartbeat)
        if hb.tzinfo is None:
            hb = hb.replace(tzinfo=timezone.utc)
        from datetime import timedelta
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=lease_seconds * 2)
        return hb < cutoff
    except (ValueError, TypeError):
        return True


def _pid_dead(owner_id: str) -> bool:
    """
    Return True if the process identified in owner_id is definitely dead.
    Only performs a live check when owner_id's host matches the current host.
    Format: "host:pid:uuid"
    """
    try:
        parts = owner_id.split(":", 2)
        if len(parts) < 2:
            return False  # Unparseable — assume alive
        host, pid_str = parts[0], parts[1]
        if host != platform.node():
            # Remote host: we can't check — treat as reclaimable if expired+stale
            return True
        pid = int(pid_str)
        if pid == os.getpid():
            return False  # That's us
        try:
            os.kill(pid, 0)
            return False  # Process alive
        except ProcessLookupError:
            return True   # PID gone
        except PermissionError:
            return False  # Exists but not ours to signal
    except (ValueError, AttributeError):
        return False


def _make_owner_id() -> str:
    return f"{platform.node()}:{os.getpid()}:{uuid.uuid4()}"


# ---------------------------------------------------------------------------
# ensure_tables — idempotent, called from database.py init_db too
# ---------------------------------------------------------------------------

def ensure_tables(db) -> None:
    """Create job_locks and recording_locks tables if they don't exist."""
    db.execute("""
        CREATE TABLE IF NOT EXISTS job_locks (
            job_name        TEXT PRIMARY KEY,
            owner_id        TEXT,
            epoch           INTEGER,
            lease_expires_at TEXT,
            last_heartbeat  TEXT
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS recording_locks (
            mb_recording_id TEXT PRIMARY KEY,
            owner_id        TEXT,
            acquired_at     TEXT
        )
    """)


# ---------------------------------------------------------------------------
# Job lock API
# ---------------------------------------------------------------------------

def acquire_job(
    job_name: str,
    owner_id: Optional[str] = None,
    lease_seconds: int = 120,
) -> Optional[Lease]:
    """
    Attempt to acquire the named job lock.

    Returns a Lease on success, None if lock is held by another active owner.

    Uses BEGIN IMMEDIATE for compare-and-set atomicity.  Reclaims expired+stale
    locks where the original process is dead (or on a different host).
    """
    if owner_id is None:
        owner_id = _make_owner_id()

    with database.get_db() as db:
        # BEGIN IMMEDIATE: no other writer can sneak in between SELECT and INSERT/UPDATE
        db.execute("BEGIN IMMEDIATE")

        row = db.execute(
            "SELECT owner_id, epoch, lease_expires_at, last_heartbeat "
            "FROM job_locks WHERE job_name = ?",
            (job_name,),
        ).fetchone()

        if row is None:
            # Lock is free — acquire with epoch 1
            epoch = 1
            db.execute(
                """
                INSERT INTO job_locks (job_name, owner_id, epoch, lease_expires_at, last_heartbeat)
                VALUES (?, ?, ?, ?, ?)
                """,
                (job_name, owner_id, epoch, _expire_iso(lease_seconds), _now_iso()),
            )
            return Lease(job_name=job_name, owner_id=owner_id, epoch=epoch,
                         lease_seconds=lease_seconds)

        # Row exists — check if reclaimable
        curr_owner, curr_epoch, expires_at, last_hb = (
            row["owner_id"], row["epoch"], row["lease_expires_at"], row["last_heartbeat"]
        )

        if curr_owner == owner_id:
            # We already hold this lock (re-entry or after restart with same owner_id)
            epoch = curr_epoch
            db.execute(
                """
                UPDATE job_locks
                SET lease_expires_at = ?, last_heartbeat = ?
                WHERE job_name = ?
                """,
                (_expire_iso(lease_seconds), _now_iso(), job_name),
            )
            return Lease(job_name=job_name, owner_id=owner_id, epoch=epoch,
                         lease_seconds=lease_seconds)

        expired = _is_expired(expires_at)
        stale = _is_stale(last_hb, lease_seconds)
        dead = _pid_dead(curr_owner)

        if expired and stale and dead:
            # Reclaimable — bump epoch
            epoch = curr_epoch + 1
            db.execute(
                """
                UPDATE job_locks
                SET owner_id = ?, epoch = ?, lease_expires_at = ?, last_heartbeat = ?
                WHERE job_name = ?
                """,
                (owner_id, epoch, _expire_iso(lease_seconds), _now_iso(), job_name),
            )
            return Lease(job_name=job_name, owner_id=owner_id, epoch=epoch,
                         lease_seconds=lease_seconds)

        # Lock is actively held
        return None


def heartbeat(lease: Lease) -> bool:
    """
    Renew the lease expiry.  Returns False if the lease was taken by another
    owner/epoch (caller should stop the job).
    """
    with database.get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT owner_id, epoch FROM job_locks WHERE job_name = ?",
            (lease.job_name,),
        ).fetchone()

        if row is None or row["owner_id"] != lease.owner_id or row["epoch"] != lease.epoch:
            return False

        db.execute(
            """
            UPDATE job_locks
            SET lease_expires_at = ?, last_heartbeat = ?
            WHERE job_name = ? AND owner_id = ? AND epoch = ?
            """,
            (
                _expire_iso(lease.lease_seconds),
                _now_iso(),
                lease.job_name,
                lease.owner_id,
                lease.epoch,
            ),
        )
        return True


def assert_lease(lease: Lease) -> None:
    """
    Verify the lease is still current.  Raises LeaseLost if not.
    Call before every fenced write to enforce epoch fencing.
    """
    with database.get_db() as db:
        row = db.execute(
            "SELECT owner_id, epoch FROM job_locks WHERE job_name = ?",
            (lease.job_name,),
        ).fetchone()

    if row is None or row["owner_id"] != lease.owner_id or row["epoch"] != lease.epoch:
        raise LeaseLost(
            f"Lease lost for job '{lease.job_name}': "
            f"expected owner={lease.owner_id} epoch={lease.epoch}, "
            f"got owner={row['owner_id'] if row else None} epoch={row['epoch'] if row else None}"
        )


def release(lease: Lease) -> None:
    """Release the lock only if we still own it (owner+epoch match)."""
    with database.get_db() as db:
        db.execute(
            "DELETE FROM job_locks WHERE job_name = ? AND owner_id = ? AND epoch = ?",
            (lease.job_name, lease.owner_id, lease.epoch),
        )


def force_release(job_name: str) -> None:
    """Unconditionally delete the lock row — for CLI/admin use."""
    with database.get_db() as db:
        db.execute("DELETE FROM job_locks WHERE job_name = ?", (job_name,))


# ---------------------------------------------------------------------------
# Per-recording action locks
# ---------------------------------------------------------------------------

def acquire_recording(mb_recording_id: str, owner_id: str) -> bool:
    """
    Acquire a per-recording lock.  Returns True on success, False if already held.
    No lease expiry — holder must explicitly release.
    """
    try:
        with database.get_db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT owner_id FROM recording_locks WHERE mb_recording_id = ?",
                (mb_recording_id,),
            ).fetchone()

            if row is not None:
                # Already held
                return False

            db.execute(
                """
                INSERT INTO recording_locks (mb_recording_id, owner_id, acquired_at)
                VALUES (?, ?, ?)
                """,
                (mb_recording_id, owner_id, _now_iso()),
            )
            return True
    except Exception:
        return False


def release_recording(mb_recording_id: str, owner_id: str) -> None:
    """Release the per-recording lock only if we hold it."""
    with database.get_db() as db:
        db.execute(
            "DELETE FROM recording_locks WHERE mb_recording_id = ? AND owner_id = ?",
            (mb_recording_id, owner_id),
        )
