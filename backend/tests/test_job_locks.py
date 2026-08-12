"""
Unit U9 — job_locks tests.

Covers:
1. Acquire → hold → second acquire returns None (active lock not reclaimable).
2. Expired + stale + pid dead → reclaimed with epoch+1.
3. assert_lease after force_release → LeaseLost.
4. heartbeat after epoch bump → returns False.
5. release by owner removes row.
6. Recording lock: acquire / second blocked / release / re-acquire.
"""

import os
import platform
from datetime import datetime, timedelta, timezone

import pytest

import database
import job_locks
from job_locks import (
    Lease,
    LeaseLost,
    acquire_job,
    assert_lease,
    force_release,
    heartbeat,
    release,
    acquire_recording,
    release_recording,
)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _set_job_lock_raw(job_name, owner_id, epoch, lease_expires_at, last_heartbeat):
    """Directly insert/replace a job_locks row for test setup."""
    with database.get_db() as db:
        db.execute(
            """
            INSERT OR REPLACE INTO job_locks
                (job_name, owner_id, epoch, lease_expires_at, last_heartbeat)
            VALUES (?, ?, ?, ?, ?)
            """,
            (job_name, owner_id, epoch, lease_expires_at, last_heartbeat),
        )


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _past(seconds: int) -> str:
    return _iso(datetime.now(timezone.utc) - timedelta(seconds=seconds))


def _future(seconds: int) -> str:
    return _iso(datetime.now(timezone.utc) + timedelta(seconds=seconds))


# ---------------------------------------------------------------------------
# Test 1: Second acquire while lock actively held → None
# ---------------------------------------------------------------------------

def test_second_acquire_while_held_returns_none(init_test_db):
    job = "test_job_held"
    force_release(job)  # clean state

    lease = acquire_job(job, owner_id="host1:111:aaa", lease_seconds=120)
    assert lease is not None
    assert lease.job_name == job
    assert lease.epoch == 1

    # Different owner tries to acquire
    second = acquire_job(job, owner_id="host1:222:bbb", lease_seconds=120)
    assert second is None, "Active lock must not be handed to a second owner"

    release(lease)


# ---------------------------------------------------------------------------
# Test 2: Expired + stale + pid dead → reclaimed with epoch+1
# ---------------------------------------------------------------------------

def test_expired_stale_dead_pid_reclaimed(init_test_db):
    job = "test_job_stale"
    force_release(job)

    # Insert a row that is expired, stale (heartbeat 500s ago), and pid=99999999 (dead)
    dead_owner = f"{platform.node()}:99999999:dead-uuid"
    _set_job_lock_raw(
        job,
        owner_id=dead_owner,
        epoch=3,
        lease_expires_at=_past(300),    # expired 5 min ago
        last_heartbeat=_past(500),       # last hb 8+ min ago (> 2×120)
    )

    new_owner = f"{platform.node()}:{os.getpid()}:new-uuid"
    lease = acquire_job(job, owner_id=new_owner, lease_seconds=120)

    assert lease is not None, "Expired+stale+dead lock should be reclaimable"
    assert lease.epoch == 4, f"Expected epoch 4 (old 3+1), got {lease.epoch}"
    assert lease.owner_id == new_owner

    release(lease)


# ---------------------------------------------------------------------------
# Test 3: assert_lease after force_release → LeaseLost
# ---------------------------------------------------------------------------

def test_assert_lease_after_force_release_raises(init_test_db):
    job = "test_job_assert"
    force_release(job)

    lease = acquire_job(job, lease_seconds=120)
    assert lease is not None

    force_release(job)

    with pytest.raises(LeaseLost):
        assert_lease(lease)


# ---------------------------------------------------------------------------
# Test 4: heartbeat after epoch bump → False
# ---------------------------------------------------------------------------

def test_heartbeat_after_epoch_bump_returns_false(init_test_db):
    job = "test_job_hb_epoch"
    force_release(job)

    lease = acquire_job(job, owner_id=f"{platform.node()}:{os.getpid()}:epoch-test",
                        lease_seconds=120)
    assert lease is not None

    # Simulate another process reclaiming the lock (bump epoch)
    with database.get_db() as db:
        db.execute(
            "UPDATE job_locks SET epoch = epoch + 1 WHERE job_name = ?",
            (job,),
        )

    result = heartbeat(lease)
    assert result is False, "heartbeat must return False after epoch mismatch"

    force_release(job)


# ---------------------------------------------------------------------------
# Test 5: release by owner removes row
# ---------------------------------------------------------------------------

def test_release_removes_row(init_test_db):
    job = "test_job_release"
    force_release(job)

    lease = acquire_job(job, lease_seconds=60)
    assert lease is not None

    release(lease)

    # Row should be gone
    with database.get_db() as db:
        row = db.execute(
            "SELECT 1 FROM job_locks WHERE job_name = ?", (job,)
        ).fetchone()
    assert row is None, "Row should be deleted after release"


# ---------------------------------------------------------------------------
# Test 6a: Recording lock acquire
# ---------------------------------------------------------------------------

def test_recording_lock_acquire_and_release(init_test_db):
    rec_id = "mb-rec-00000001"
    owner = "proc:1:test"

    # Clean state
    release_recording(rec_id, owner)

    ok = acquire_recording(rec_id, owner)
    assert ok is True

    # Second acquire by different owner → False
    ok2 = acquire_recording(rec_id, "proc:2:other")
    assert ok2 is False

    # Release by original owner
    release_recording(rec_id, owner)

    # Now a new owner can acquire
    ok3 = acquire_recording(rec_id, "proc:3:new")
    assert ok3 is True

    release_recording(rec_id, "proc:3:new")


# ---------------------------------------------------------------------------
# Test 6b: Recording lock — release by non-owner doesn't unlock
# ---------------------------------------------------------------------------

def test_recording_lock_release_by_wrong_owner_noop(init_test_db):
    rec_id = "mb-rec-00000002"
    owner = "proc:10:real"
    other = "proc:11:impostor"

    release_recording(rec_id, owner)
    release_recording(rec_id, other)

    ok = acquire_recording(rec_id, owner)
    assert ok is True

    # Impostor tries to release
    release_recording(rec_id, other)

    # Lock should still be held
    ok2 = acquire_recording(rec_id, "proc:12:another")
    assert ok2 is False, "Lock should still be held after wrong-owner release"

    release_recording(rec_id, owner)
