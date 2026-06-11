"""
Unit U9 — restore_service tests.

Covers:
1. Round-trip restore: trash a file, restore_op → original path restored.
2. Occupied destination → .restored conflict path + needs_manual_merge=True.
3. Hash mismatch → status='hash_mismatch', no file moved.
4. Double restore → idempotent no-op (status='already_restored').
5. Missing journal → status='aborted'.
"""

import os
import pytest
from pathlib import Path

import database
import file_txn
import restore_service
from file_txn import trash_file_txn, replace_file
from restore_service import restore_op, RestoreResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _set_setting(key, value):
    with database.get_db() as db:
        db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, value),
        )


def _make_file(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def kill_switch_on(init_test_db):
    _set_setting("identity_act_enabled", "true")
    yield
    _set_setting("identity_act_enabled", "false")


@pytest.fixture()
def library(tmp_path):
    lib = tmp_path / "library"
    lib.mkdir()
    return lib


@pytest.fixture(autouse=True)
def journal_dir(tmp_path, monkeypatch):
    jdir = tmp_path / "journals"
    jdir.mkdir()
    monkeypatch.setenv("FILE_TXN_JOURNAL_DIR", str(jdir))
    yield jdir
    file_txn._fault_point = None


# ---------------------------------------------------------------------------
# Test 1: Round-trip restore
# ---------------------------------------------------------------------------

def test_restore_round_trip(library, tmp_path):
    target = library / "artist" / "song.flac"
    content = b"ORIGINAL_AUDIO" * 50
    _make_file(target, content)

    result = trash_file_txn(
        target,
        library,
        revalidate=lambda: True,
        db_update=lambda: None,
        meta={"track_id": 1},
    )
    assert result.status == "finalized"
    assert not target.exists()

    restore_result = restore_op(result.op_id)

    assert restore_result.status == "restored"
    assert restore_result.restored_to == target
    assert restore_result.needs_manual_merge is False
    assert target.exists()
    assert target.read_bytes() == content


# ---------------------------------------------------------------------------
# Test 2: Occupied destination → conflict path
# ---------------------------------------------------------------------------

def test_restore_occupied_destination(library, tmp_path):
    target = library / "artist" / "occupied.flac"
    orig_content = b"ORIGINAL_OCCUPIED" * 30
    _make_file(target, orig_content)

    result = trash_file_txn(
        target,
        library,
        revalidate=lambda: True,
        db_update=lambda: None,
        meta={},
    )
    assert result.status == "finalized"

    # Recreate a NEW file at the original path (simulating a replacement was placed there)
    newer_content = b"NEWER_FILE_HERE" * 30
    _make_file(target, newer_content)

    restore_result = restore_op(result.op_id)

    assert restore_result.status == "restored"
    assert restore_result.needs_manual_merge is True
    # Conflict path must differ from original
    assert restore_result.restored_to != target
    assert restore_result.restored_to is not None
    assert restore_result.restored_to.exists()
    # The restored file has the original content
    assert restore_result.restored_to.read_bytes() == orig_content
    # The newer file at original path is untouched
    assert target.read_bytes() == newer_content


# ---------------------------------------------------------------------------
# Test 3: Hash mismatch → abort, no file moved
# ---------------------------------------------------------------------------

def test_restore_hash_mismatch(library, tmp_path):
    target = library / "artist" / "hash_check.flac"
    content = b"REAL_AUDIO" * 40
    _make_file(target, content)

    result = trash_file_txn(
        target,
        library,
        revalidate=lambda: True,
        db_update=lambda: None,
        meta={},
    )
    assert result.status == "finalized"

    # Corrupt the trashed file
    result.trash_path.write_bytes(b"CORRUPTED_DATA" * 40)

    restore_result = restore_op(result.op_id)

    assert restore_result.status == "hash_mismatch"
    # Trashed file still at trash location (not moved)
    assert result.trash_path.exists()
    # Original path still absent
    assert not target.exists()


# ---------------------------------------------------------------------------
# Test 4: Double restore → idempotent no-op
# ---------------------------------------------------------------------------

def test_restore_idempotent(library, tmp_path):
    target = library / "artist" / "idempotent.flac"
    content = b"IDEMPOTENT_AUDIO" * 25
    _make_file(target, content)

    result = trash_file_txn(
        target,
        library,
        revalidate=lambda: True,
        db_update=lambda: None,
        meta={},
    )
    assert result.status == "finalized"

    r1 = restore_op(result.op_id)
    assert r1.status == "restored"
    assert target.exists()

    r2 = restore_op(result.op_id)
    assert r2.status == "already_restored"
    # File still there, content unchanged
    assert target.read_bytes() == content


# ---------------------------------------------------------------------------
# Test 5: Missing journal → aborted
# ---------------------------------------------------------------------------

def test_restore_missing_journal():
    result = restore_op("nonexistent-op-id-12345")
    assert result.status == "aborted"
    assert "No journal" in result.detail or "journal" in result.detail.lower()
