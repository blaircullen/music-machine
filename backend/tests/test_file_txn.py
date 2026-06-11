"""
Unit U9 — file_txn tests.

Covers:
1. Happy-path replace: 6 states journaled in order; new file at final path;
   original in trash; db_update called once; sentinel gone.
2. Happy-path trash: 4 states journaled; file in trash; original gone.
3. Crash-injection at every fault point (parametrize) → recover_incomplete_ops()
   → invariant: original accessible (either at original path or trash, never lost).
4. Cross-device refusal via monkeypatched os.stat.
5. Kill switch false → KillSwitchDisabled, no rename, audit record in journal.
6. revalidate() False → clean abort, original untouched.
7. Concurrent ops on different files → both finalize.
8. Trash collision suffix.
"""

import os
import pytest
from pathlib import Path

import database
import file_txn
from file_txn import (
    replace_file,
    trash_file_txn,
    recover_incomplete_ops,
    CrossDeviceError,
    KillSwitchDisabled,
    _InjectedCrash,
    OpResult,
    assert_same_device,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _set_setting(key, value):
    with database.get_db() as db:
        db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, value),
        )


def _make_file(path: Path, content: bytes = b"FLAC_HEADER" * 100) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _journal_states_for(op_id: str):
    return [r.get("state") for r in file_txn._read_journal(op_id)]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def kill_switch_on(init_test_db):
    """Ensure kill switch is ON by default for file_txn tests."""
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
    """Point FILE_TXN_JOURNAL_DIR at a temp dir for each test."""
    jdir = tmp_path / "journals"
    jdir.mkdir()
    monkeypatch.setenv("FILE_TXN_JOURNAL_DIR", str(jdir))
    yield jdir
    # Reset fault point after each test
    file_txn._fault_point = None


# ---------------------------------------------------------------------------
# Test 1: Happy-path replace
# ---------------------------------------------------------------------------

def test_replace_file_happy_path(library, tmp_path):
    original = library / "artist" / "album" / "track.flac"
    staged = tmp_path / "staged.flac"
    _make_file(original, b"OLD_CONTENT" * 50)
    _make_file(staged, b"NEW_CONTENT" * 50)

    db_calls = []

    result = replace_file(
        original,
        staged,
        library,
        revalidate=lambda: True,
        db_update=lambda: db_calls.append(1),
        meta={"track_id": 42},
    )

    assert result.status == "finalized"
    assert result.final_path == original
    assert result.trash_path is not None

    # New content at original path
    assert original.read_bytes() == b"NEW_CONTENT" * 50
    # Original in trash
    assert result.trash_path.exists()
    assert result.trash_path.read_bytes() == b"OLD_CONTENT" * 50
    # db_update called exactly once
    assert db_calls == [1]
    # Sentinel gone
    sentinel = original.parent / f".op-in-progress-{result.op_id}"
    assert not sentinel.exists()

    # States journaled in correct order
    expected_states = [
        "intent",
        "original_quarantined",
        "replacement_staged",
        "replacement_committed",
        "db_committed",
        "finalized",
    ]
    states = _journal_states_for(result.op_id)
    assert states == expected_states, f"Got states: {states}"


# ---------------------------------------------------------------------------
# Test 2: Happy-path trash
# ---------------------------------------------------------------------------

def test_trash_file_happy_path(library, tmp_path):
    target = library / "artist" / "album" / "trash_me.flac"
    _make_file(target, b"TRASH_CONTENT" * 30)

    db_calls = []

    result = trash_file_txn(
        target,
        library,
        revalidate=lambda: True,
        db_update=lambda: db_calls.append(1),
        meta={"reason": "test"},
    )

    assert result.status == "finalized"
    assert result.trash_path is not None
    assert result.trash_path.exists()
    assert not target.exists()
    assert db_calls == [1]

    expected_states = ["intent", "original_quarantined", "db_committed", "finalized"]
    states = _journal_states_for(result.op_id)
    assert states == expected_states, f"Got states: {states}"


# ---------------------------------------------------------------------------
# Test 3: Crash injection — invariant: original never lost
# ---------------------------------------------------------------------------

REPLACE_FAULT_POINTS = [
    "after_intent",
    "after_sentinel",
    "after_revalidate",
    "after_quarantine",
    "after_stage",
    "after_replacement_committed",
    "after_db_committed",
]

TRASH_FAULT_POINTS = [
    "after_intent",
    "after_sentinel",
    "after_revalidate",
    "after_quarantine",
    "after_db_committed",
]


@pytest.mark.parametrize("fault_point", REPLACE_FAULT_POINTS)
def test_replace_crash_then_recover(library, tmp_path, fault_point):
    original = library / "crash_test" / "track.flac"
    staged = tmp_path / "staged_crash.flac"
    _make_file(original, b"ORIG" * 100)
    _make_file(staged, b"NEW" * 100)

    orig_content = original.read_bytes()

    # Inject crash
    file_txn._fault_point = fault_point
    with pytest.raises(_InjectedCrash):
        replace_file(
            original,
            staged,
            library,
            revalidate=lambda: True,
            db_update=lambda: None,
            meta={},
        )
    file_txn._fault_point = None

    # Run reconciler
    summary = recover_incomplete_ops()

    # Invariant (plan U9): the live slot is never partial — it holds either
    # the complete original (roll-back) or the complete replacement
    # (roll-forward) — and in the roll-forward case the original must be
    # fully recoverable from trash.
    new_content = b"NEW" * 100
    assert original.exists(), (
        f"After crash at {fault_point}: live slot empty. Summary: {summary}"
    )
    content_found = original.read_bytes()
    assert content_found in (orig_content, new_content), (
        f"Live slot partial/corrupt after crash at {fault_point}"
    )

    if content_found == new_content:
        # Roll-forward: original must be intact in trash, and the journal
        # must direct the caller to reconcile the DB.
        trash_root = library / ".m2-trash"
        trash_files = list(trash_root.rglob("*.flac")) if trash_root.exists() else []
        assert trash_files, (
            f"Roll-forward at {fault_point} but original not in trash. "
            f"Summary: {summary}"
        )
        assert trash_files[0].read_bytes() == orig_content, (
            f"Original content lost after crash at {fault_point}"
        )
        assert any(
            s.get("action") == "reconciled_forward" and s.get("needs_db_reconciliation")
            for s in summary
        ), f"Roll-forward at {fault_point} missing db-reconciliation flag"


@pytest.mark.parametrize("fault_point", TRASH_FAULT_POINTS)
def test_trash_crash_then_recover(library, tmp_path, fault_point):
    target = library / "crash_trash" / "song.flac"
    _make_file(target, b"SONG" * 80)
    orig_content = target.read_bytes()

    file_txn._fault_point = fault_point
    with pytest.raises(_InjectedCrash):
        trash_file_txn(
            target,
            library,
            revalidate=lambda: True,
            db_update=lambda: None,
            meta={},
        )
    file_txn._fault_point = None

    recover_incomplete_ops()

    # Invariant
    if target.exists():
        content_found = target.read_bytes()
    else:
        trash_root = library / ".m2-trash"
        trash_files = list(trash_root.rglob("*.flac")) if trash_root.exists() else []
        assert trash_files, (
            f"After trash crash at {fault_point}: file gone and not in trash"
        )
        content_found = trash_files[0].read_bytes()

    assert content_found == orig_content


# ---------------------------------------------------------------------------
# Test 4: Cross-device refusal
# ---------------------------------------------------------------------------

def test_cross_device_refused(library, tmp_path, monkeypatch):
    original = library / "track.flac"
    staged = tmp_path / "staged.flac"
    _make_file(original)
    _make_file(staged)

    real_stat = os.stat

    def fake_stat(path, *args, **kwargs):
        result = real_stat(path, *args, **kwargs)
        # Make staged file appear on a different device
        if str(path) == str(staged):
            class FakeStat:
                st_dev = result.st_dev + 9999
                def __getattr__(self, name):
                    return getattr(result, name)
            return FakeStat()
        return result

    monkeypatch.setattr(os, "stat", fake_stat)

    with pytest.raises(CrossDeviceError):
        file_txn.replace_file(
            original,
            staged,
            library,
            revalidate=lambda: True,
            db_update=lambda: None,
            meta={},
        )


# ---------------------------------------------------------------------------
# Test 5: Kill switch false → KillSwitchDisabled, audit record, no rename
# ---------------------------------------------------------------------------

def test_kill_switch_blocks_replace(library, tmp_path):
    _set_setting("identity_act_enabled", "false")

    original = library / "ks_test" / "track.flac"
    staged = tmp_path / "staged_ks.flac"
    _make_file(original, b"ORIG_KS" * 20)
    _make_file(staged, b"NEW_KS" * 20)

    with pytest.raises(KillSwitchDisabled):
        replace_file(
            original,
            staged,
            library,
            revalidate=lambda: True,
            db_update=lambda: None,
            meta={},
        )

    # Original untouched
    assert original.exists()
    assert original.read_bytes() == b"ORIG_KS" * 20

    # Journal contains kill_switch_blocked audit record
    jdir = Path(os.environ["FILE_TXN_JOURNAL_DIR"])
    journals = list(jdir.glob("*.jsonl"))
    assert journals, "No journal file written"
    found_audit = False
    for j in journals:
        records = file_txn._read_journal(j.stem)
        if any(r.get("state") == "kill_switch_blocked" for r in records):
            found_audit = True
            break
    assert found_audit, "kill_switch_blocked audit record not found in journal"


# ---------------------------------------------------------------------------
# Test 6: revalidate() False → clean abort, original untouched
# ---------------------------------------------------------------------------

def test_revalidate_false_aborts_cleanly(library, tmp_path):
    original = library / "rv_test" / "track.flac"
    staged = tmp_path / "staged_rv.flac"
    _make_file(original, b"ORIG_RV" * 20)
    _make_file(staged, b"NEW_RV" * 20)

    result = replace_file(
        original,
        staged,
        library,
        revalidate=lambda: False,
        db_update=lambda: None,
        meta={},
    )

    assert result.status == "aborted"
    # Original untouched
    assert original.exists()
    assert original.read_bytes() == b"ORIG_RV" * 20


# ---------------------------------------------------------------------------
# Test 7: Concurrent ops on different files — both finalize
# ---------------------------------------------------------------------------

def test_concurrent_ops_different_files(library, tmp_path):
    orig_a = library / "a" / "track_a.flac"
    staged_a = tmp_path / "staged_a.flac"
    orig_b = library / "b" / "track_b.flac"
    staged_b = tmp_path / "staged_b.flac"

    _make_file(orig_a, b"ORIG_A" * 30)
    _make_file(staged_a, b"NEW_A" * 30)
    _make_file(orig_b, b"ORIG_B" * 30)
    _make_file(staged_b, b"NEW_B" * 30)

    result_a = replace_file(
        orig_a, staged_a, library,
        revalidate=lambda: True,
        db_update=lambda: None,
        meta={"which": "a"},
    )
    result_b = replace_file(
        orig_b, staged_b, library,
        revalidate=lambda: True,
        db_update=lambda: None,
        meta={"which": "b"},
    )

    assert result_a.status == "finalized"
    assert result_b.status == "finalized"
    assert orig_a.read_bytes() == b"NEW_A" * 30
    assert orig_b.read_bytes() == b"NEW_B" * 30


# ---------------------------------------------------------------------------
# Test 8: Trash collision suffix
# ---------------------------------------------------------------------------

def test_trash_collision_suffix(library, tmp_path):
    """Two trash ops on the same relative path produce non-colliding trash destinations."""
    target1 = library / "dup_dir" / "song.flac"
    _make_file(target1, b"SONG_1" * 20)

    result1 = trash_file_txn(
        target1, library,
        revalidate=lambda: True,
        db_update=lambda: None,
        meta={},
    )
    assert result1.status == "finalized"

    # Recreate the file at the same path
    target2 = library / "dup_dir" / "song.flac"
    _make_file(target2, b"SONG_2" * 20)

    result2 = trash_file_txn(
        target2, library,
        revalidate=lambda: True,
        db_update=lambda: None,
        meta={},
    )
    assert result2.status == "finalized"

    # Both trash files must exist and be different
    assert result1.trash_path != result2.trash_path
    assert result1.trash_path.exists()
    assert result2.trash_path.exists()
    assert result1.trash_path.read_bytes() == b"SONG_1" * 20
    assert result2.trash_path.read_bytes() == b"SONG_2" * 20
