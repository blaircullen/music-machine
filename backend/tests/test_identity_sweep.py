"""
Tests for backend/identity_sweep.py (U5).

All I/O is mocked — no real DB, no real files, no real network.

Scenarios covered:
  1. happy_path         — confirmed track written, counter incremented
  2. audd_escalation    — non-confirmed phase1 triggers AudD → confirmed
  3. mirror_unavailable_at_start — hard-stop before any work
  4. mirror_lost_mid_sweep — deferred + stop after mirror drops
  5. lease_not_acquired  — another owner holds lock → sweep exits gracefully
  6. dry_run             — verdicts computed but nothing written to DB
  7. stop_signal         — _sweep_stop fires before batch → no writes
  8. budget_exhausted    — no AudD call, verdict is deferred
"""

from __future__ import annotations

import json
import sys
import os
import threading
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

# Ensure backend/ is on sys.path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ---------------------------------------------------------------------------
# Minimal in-memory DB fixture (no real SQLite file)
# ---------------------------------------------------------------------------

class _FakeRow(dict):
    """dict that also supports positional access (like sqlite3.Row)."""
    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


def _make_track_row(track_id=1, file_path="/music/test.flac",
                     artist="Beck", title="Loser"):
    return _FakeRow(id=track_id, file_path=file_path, artist=artist, title=title)


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows
        self.rowcount = len(rows) if rows else 0

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class _FakeDB:
    """Minimal DB fake that records INSERT OR REPLACE calls."""
    def __init__(self):
        self.written: list[dict] = []
        self._track_rows: list = []
        self._total_active: int = 0

    def set_tracks(self, rows, total_active=None):
        self._track_rows = rows
        self._total_active = total_active if total_active is not None else len(rows)

    def execute(self, sql: str, params=()) -> _FakeCursor:
        sql_stripped = sql.strip()
        if "INSERT OR REPLACE INTO track_identity" in sql_stripped:
            cols = [
                "track_id", "state", "mb_recording_id", "mb_release_id",
                "isrc", "artist", "title", "album", "date", "track_no",
                "tier", "evidence", "divergent", "decided_at", "resolver_version",
            ]
            self.written.append(dict(zip(cols, params)))
            return _FakeCursor([])
        if "SELECT COUNT(*) FROM tracks WHERE status" in sql_stripped:
            return _FakeCursor([_FakeRow(**{"COUNT(*)": self._total_active})])
        if ("SELECT COUNT(*) FROM tracks" in sql_stripped and
                "LEFT JOIN track_identity" in sql_stripped):
            return _FakeCursor([_FakeRow(**{"COUNT(*)": len(self._track_rows)})])
        if ("SELECT t.id" in sql_stripped and
                "LEFT JOIN track_identity" in sql_stripped):
            # Honor the keyset cursor (params = (version, last_id, limit))
            last_id = params[1] if len(params) >= 3 else 0
            return _FakeCursor([r for r in self._track_rows if r["id"] > last_id])
        if "SELECT fingerprint, duration FROM tracks" in sql_stripped:
            return _FakeCursor([_FakeRow(fingerprint=None, duration=None)])
        if "UPDATE tracks SET fingerprint" in sql_stripped:
            return _FakeCursor([])
        return _FakeCursor([])

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


# Context manager factory for get_db
def _make_get_db(fake_db: _FakeDB):
    from contextlib import contextmanager

    @contextmanager
    def _get_db():
        yield fake_db

    return _get_db


# ---------------------------------------------------------------------------
# Evidence + verdict helpers (matching test_identity_resolver.py shapes)
# ---------------------------------------------------------------------------

def _mock_confirmed_verdict():
    return {
        "state": "confirmed",
        "tier": "T4",
        "mb_recording_id": "rid-A",
        "mb_release_id": "rel-1",
        "isrc": None,
        "artist": "Beck",
        "title": "Loser",
        "album": "Mellow Gold",
        "date": "1994",
        "track_no": 1,
        "divergent": False,
        "evidence": {"vetoes_fired": [], "tier_trace": ["T4"], "comparisons": {}, "candidates": []},
        "resolver_version": "1",
    }


def _mock_review_verdict():
    return {
        "state": "review",
        "tier": "T2",
        "mb_recording_id": "rid-A",
        "mb_release_id": None,
        "isrc": None,
        "artist": "Beck",
        "title": "Loser",
        "album": "",
        "date": "",
        "track_no": None,
        "divergent": True,
        "evidence": {"vetoes_fired": [], "tier_trace": ["T2"], "comparisons": {}, "candidates": []},
        "resolver_version": "1",
    }


def _mock_deferred_verdict():
    return {
        "state": "deferred",
        "tier": None,
        "mb_recording_id": None,
        "mb_release_id": None,
        "isrc": None,
        "artist": None,
        "title": None,
        "album": None,
        "date": None,
        "track_no": None,
        "divergent": False,
        "evidence": {"vetoes_fired": [], "tier_trace": [], "comparisons": {}, "candidates": []},
        "resolver_version": "1",
    }


# ---------------------------------------------------------------------------
# Shared mock surface for all tests
# ---------------------------------------------------------------------------

def _base_patches():
    """Return a dict of patch targets → mock values for common dependencies."""
    return {
        "identity_sweep.mb_local": MagicMock(is_available=MagicMock(return_value=True),
                                              get_recording_metadata=MagicMock(return_value=None),
                                              recordings_for_isrc=MagicMock(return_value=[])),
        "identity_sweep.os.path.isfile": MagicMock(return_value=True),
        "identity_sweep._get_or_compute_fingerprint": MagicMock(return_value=("fp123", 200.0)),
        "identity_sweep._rate_limited_acoustid": MagicMock(return_value=[]),
        "identity_sweep.decoded_duration_ms": MagicMock(return_value=200_000),
    }


# ---------------------------------------------------------------------------
# Scenario 1 — happy path: one track, confirmed verdict written
# ---------------------------------------------------------------------------

class TestHappyPath:
    def test_confirmed_written_and_counter(self):
        fake_db = _FakeDB()
        track = _make_track_row()
        fake_db.set_tracks([track], total_active=1)

        import identity_sweep as sw
        # Reset module state
        sw._sweep_stop.clear()
        sw.sweep_status.update({k: 0 for k in ("confirmed", "review", "unknown",
                                                  "conflict", "deferred", "error",
                                                  "audd_escalated", "processed", "total")})

        confirmed = _mock_confirmed_verdict()
        deferred_for_budget = _mock_deferred_verdict()

        patches = _base_patches()
        patches["identity_sweep.resolve"] = MagicMock(return_value=confirmed)
        patches["identity_sweep.database.get_db"] = _make_get_db(fake_db)
        patches["identity_sweep.job_locks.acquire_job"] = MagicMock(
            return_value=MagicMock(job_name="identity_sweep", owner_id="x", epoch=1,
                                   lease_seconds=300))
        patches["identity_sweep.job_locks.heartbeat"] = MagicMock(return_value=True)
        patches["identity_sweep.job_locks.assert_lease"] = MagicMock()
        patches["identity_sweep.job_locks.release"] = MagicMock()
        patches["identity_sweep.check_budget"] = MagicMock(return_value=False)
        patches["identity_sweep.identify_track"] = MagicMock(return_value=None)

        with _apply_patches(patches):
            # Public entry point — regression coverage for the run-lock vs
            # status-lock deadlock (run_sweep must not hold _sweep_lock).
            sw.run_sweep(dry_run=False)

        assert len(fake_db.written) == 1
        row = fake_db.written[0]
        assert row["track_id"] == 1
        assert row["state"] == "confirmed"
        assert row["tier"] == "T4"
        assert sw.sweep_status["confirmed"] == 1
        assert sw.sweep_status["processed"] == 1
        assert sw.sweep_status["stopped_reason"] == "complete"


# ---------------------------------------------------------------------------
# Scenario 2 — AudD escalation: phase1=review, phase2=confirmed after AudD
# ---------------------------------------------------------------------------

class TestAuddEscalation:
    def test_escalation_calls_identify_track(self):
        fake_db = _FakeDB()
        track = _make_track_row()
        fake_db.set_tracks([track], total_active=1)

        import identity_sweep as sw
        sw._sweep_stop.clear()

        audd_result = {"artist": "Beck", "title": "Loser", "isrc": "USRC11400001",
                       "audd_score": 0.95, "duration_ms": 200_000}
        review_verdict = _mock_review_verdict()
        confirmed_verdict = _mock_confirmed_verdict()

        resolve_mock = MagicMock(side_effect=[review_verdict, confirmed_verdict])
        identify_mock = MagicMock(return_value=audd_result)

        patches = _base_patches()
        patches["identity_sweep.resolve"] = resolve_mock
        patches["identity_sweep.database.get_db"] = _make_get_db(fake_db)
        patches["identity_sweep.job_locks.acquire_job"] = MagicMock(
            return_value=MagicMock(job_name="identity_sweep", owner_id="x", epoch=1,
                                   lease_seconds=300))
        patches["identity_sweep.job_locks.heartbeat"] = MagicMock(return_value=True)
        patches["identity_sweep.job_locks.assert_lease"] = MagicMock()
        patches["identity_sweep.job_locks.release"] = MagicMock()
        patches["identity_sweep.check_budget"] = MagicMock(return_value=True)
        patches["identity_sweep.identify_track"] = identify_mock

        with _apply_patches(patches):
            sw._run_sweep_inner(dry_run=False)

        identify_mock.assert_called_once()
        assert resolve_mock.call_count == 2
        assert len(fake_db.written) == 1
        assert fake_db.written[0]["state"] == "confirmed"
        assert sw.sweep_status["audd_escalated"] == 1


# ---------------------------------------------------------------------------
# Scenario 3 — mirror unavailable at start: hard-stop, nothing written
# ---------------------------------------------------------------------------

class TestMirrorUnavailableAtStart:
    def test_hard_stop_before_any_work(self):
        fake_db = _FakeDB()
        fake_db.set_tracks([_make_track_row()], total_active=1)

        import identity_sweep as sw
        sw._sweep_stop.clear()

        patches = _base_patches()
        patches["identity_sweep.mb_local"].is_available.return_value = False
        patches["identity_sweep.database.get_db"] = _make_get_db(fake_db)
        patches["identity_sweep.job_locks.acquire_job"] = MagicMock(
            return_value=MagicMock(job_name="identity_sweep", owner_id="x", epoch=1,
                                   lease_seconds=300))

        with _apply_patches(patches):
            sw._run_sweep_inner(dry_run=False)

        assert len(fake_db.written) == 0
        assert sw.sweep_status["stopped_reason"] == "mirror_unavailable"
        assert not sw.sweep_status["running"]


# ---------------------------------------------------------------------------
# Scenario 4 — mirror lost mid-sweep: deferred + stop
# ---------------------------------------------------------------------------

class TestMirrorLostMidSweep:
    def test_deferred_written_and_sweep_stops(self):
        fake_db = _FakeDB()
        track = _make_track_row()
        fake_db.set_tracks([track], total_active=1)

        import identity_sweep as sw
        sw._sweep_stop.clear()

        # Mirror available at start, gone on first mid-sweep check
        mb_mock = MagicMock()
        mb_mock.is_available.side_effect = [True, False]  # start=OK, mid-sweep=down
        mb_mock.get_recording_metadata.return_value = None
        mb_mock.recordings_for_isrc.return_value = []

        deferred = _mock_deferred_verdict()

        patches = _base_patches()
        patches["identity_sweep.mb_local"] = mb_mock
        patches["identity_sweep.resolve"] = MagicMock(return_value=deferred)
        patches["identity_sweep.database.get_db"] = _make_get_db(fake_db)
        patches["identity_sweep.job_locks.acquire_job"] = MagicMock(
            return_value=MagicMock(job_name="identity_sweep", owner_id="x", epoch=1,
                                   lease_seconds=300))
        patches["identity_sweep.job_locks.heartbeat"] = MagicMock(return_value=True)
        patches["identity_sweep.job_locks.assert_lease"] = MagicMock()
        patches["identity_sweep.job_locks.release"] = MagicMock()
        patches["identity_sweep.check_budget"] = MagicMock(return_value=False)
        patches["identity_sweep.identify_track"] = MagicMock(return_value=None)

        with _apply_patches(patches):
            sw._run_sweep_inner(dry_run=False)

        # One deferred row should be written for the track that hit mirror loss
        assert len(fake_db.written) == 1
        assert fake_db.written[0]["state"] == "deferred"
        assert sw.sweep_status["stopped_reason"] == "mirror_unavailable"


# ---------------------------------------------------------------------------
# Scenario 5 — lease not acquired: sweep exits gracefully
# ---------------------------------------------------------------------------

class TestLeaseNotAcquired:
    def test_exits_gracefully_when_lock_held(self):
        fake_db = _FakeDB()
        fake_db.set_tracks([_make_track_row()], total_active=1)

        import identity_sweep as sw
        sw._sweep_stop.clear()

        patches = _base_patches()
        patches["identity_sweep.database.get_db"] = _make_get_db(fake_db)
        patches["identity_sweep.job_locks.acquire_job"] = MagicMock(return_value=None)

        with _apply_patches(patches):
            sw._run_sweep_inner(dry_run=False)

        assert len(fake_db.written) == 0
        assert sw.sweep_status["stopped_reason"] == "lease_lost"
        assert not sw.sweep_status["running"]


# ---------------------------------------------------------------------------
# Scenario 6 — dry_run: verdicts computed but DB not written
# ---------------------------------------------------------------------------

class TestDryRun:
    def test_no_writes_in_dry_run(self):
        fake_db = _FakeDB()
        track = _make_track_row()
        fake_db.set_tracks([track], total_active=1)

        import identity_sweep as sw
        sw._sweep_stop.clear()

        confirmed = _mock_confirmed_verdict()
        patches = _base_patches()
        patches["identity_sweep.resolve"] = MagicMock(return_value=confirmed)
        patches["identity_sweep.database.get_db"] = _make_get_db(fake_db)
        patches["identity_sweep.job_locks.acquire_job"] = MagicMock(
            return_value=MagicMock(job_name="identity_sweep", owner_id="x", epoch=1,
                                   lease_seconds=300))
        patches["identity_sweep.job_locks.heartbeat"] = MagicMock(return_value=True)
        patches["identity_sweep.job_locks.assert_lease"] = MagicMock()
        patches["identity_sweep.job_locks.release"] = MagicMock()
        patches["identity_sweep.check_budget"] = MagicMock(return_value=False)
        patches["identity_sweep.identify_track"] = MagicMock(return_value=None)

        with _apply_patches(patches):
            sw._run_sweep_inner(dry_run=True)

        # The fake_db tracks only INSERT OR REPLACE calls, which are skipped in dry_run
        assert len(fake_db.written) == 0
        assert sw.sweep_status["processed"] == 1


# ---------------------------------------------------------------------------
# Scenario 7 — stop signal: _sweep_stop fires, no writes
# ---------------------------------------------------------------------------

class TestStopSignal:
    def test_stop_before_batch_exits_cleanly(self):
        fake_db = _FakeDB()
        track = _make_track_row()
        fake_db.set_tracks([track], total_active=1)

        import identity_sweep as sw
        sw._sweep_stop.clear()

        confirmed = _mock_confirmed_verdict()

        original_execute = fake_db.execute

        def execute_and_stop(sql, params=()):
            result = original_execute(sql, params)
            # Signal stop when the sweep fetches the batch
            if "SELECT t.id" in sql and "LEFT JOIN track_identity" in sql:
                sw._sweep_stop.set()
            return result

        fake_db.execute = execute_and_stop

        patches = _base_patches()
        patches["identity_sweep.resolve"] = MagicMock(return_value=confirmed)
        patches["identity_sweep.database.get_db"] = _make_get_db(fake_db)
        patches["identity_sweep.job_locks.acquire_job"] = MagicMock(
            return_value=MagicMock(job_name="identity_sweep", owner_id="x", epoch=1,
                                   lease_seconds=300))
        patches["identity_sweep.job_locks.heartbeat"] = MagicMock(return_value=True)
        patches["identity_sweep.job_locks.assert_lease"] = MagicMock()
        patches["identity_sweep.job_locks.release"] = MagicMock()
        patches["identity_sweep.check_budget"] = MagicMock(return_value=False)
        patches["identity_sweep.identify_track"] = MagicMock(return_value=None)

        with _apply_patches(patches):
            sw._run_sweep_inner(dry_run=False)

        assert sw.sweep_status["stopped_reason"] == "stopped"
        assert not sw.sweep_status["running"]


# ---------------------------------------------------------------------------
# Scenario 8 — budget exhausted: no AudD call, verdict is deferred
# ---------------------------------------------------------------------------

class TestBudgetExhausted:
    def test_no_audd_call_when_budget_exhausted(self):
        fake_db = _FakeDB()
        track = _make_track_row()
        fake_db.set_tracks([track], total_active=1)

        import identity_sweep as sw
        sw._sweep_stop.clear()

        review_verdict = _mock_review_verdict()
        deferred_verdict = _mock_deferred_verdict()

        # Phase1 returns review (not confirmed); budget exhausted → phase2 returns deferred
        resolve_mock = MagicMock(side_effect=[review_verdict, deferred_verdict])
        identify_mock = MagicMock(return_value=None)

        patches = _base_patches()
        patches["identity_sweep.resolve"] = resolve_mock
        patches["identity_sweep.database.get_db"] = _make_get_db(fake_db)
        patches["identity_sweep.job_locks.acquire_job"] = MagicMock(
            return_value=MagicMock(job_name="identity_sweep", owner_id="x", epoch=1,
                                   lease_seconds=300))
        patches["identity_sweep.job_locks.heartbeat"] = MagicMock(return_value=True)
        patches["identity_sweep.job_locks.assert_lease"] = MagicMock()
        patches["identity_sweep.job_locks.release"] = MagicMock()
        patches["identity_sweep.check_budget"] = MagicMock(return_value=False)
        patches["identity_sweep.identify_track"] = identify_mock

        with _apply_patches(patches):
            sw._run_sweep_inner(dry_run=False)

        # AudD should NOT be called when budget is exhausted
        identify_mock.assert_not_called()
        assert sw.sweep_status["audd_escalated"] == 0
        assert len(fake_db.written) == 1
        assert fake_db.written[0]["state"] == "deferred"


# ---------------------------------------------------------------------------
# Route smoke tests
# ---------------------------------------------------------------------------

class TestRoutes:
    def test_sweep_status_endpoint(self):
        from fastapi.testclient import TestClient
        from main import app
        client = TestClient(app)

        resp = client.get("/api/identity/sweep/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "running" in data
        assert "processed" in data

    def test_report_endpoint(self):
        from fastapi.testclient import TestClient
        from main import app
        client = TestClient(app)

        resp = client.get("/api/identity/report")
        assert resp.status_code == 200
        data = resp.json()
        assert "resolver_version" in data
        assert "by_state" in data
        assert "divergent" in data

    def test_start_stop_returns_ok(self):
        from fastapi.testclient import TestClient
        from main import app
        import identity_sweep as sw

        client = TestClient(app)

        # Ensure sweep is not running before test
        sw.sweep_status["running"] = False

        resp = client.post("/api/identity/sweep/stop")
        assert resp.status_code == 200
        data = resp.json()
        # Not running → ok=False is expected
        assert "ok" in data

    def test_list_endpoints_return_paginated_shape(self):
        from fastapi.testclient import TestClient
        from main import app
        client = TestClient(app)

        for path in ("/api/identity/tracks/confirmed",
                     "/api/identity/tracks/review",
                     "/api/identity/tracks/conflict",
                     "/api/identity/tracks/unknown",
                     "/api/identity/tracks/deferred",
                     "/api/identity/tracks/error",
                     "/api/identity/tracks/divergent"):
            resp = client.get(path)
            assert resp.status_code == 200, f"{path} returned {resp.status_code}"
            data = resp.json()
            assert "items" in data
            assert "total" in data
            assert isinstance(data["items"], list)


# ---------------------------------------------------------------------------
# Utility: context manager to apply multiple patches at once
# ---------------------------------------------------------------------------

from contextlib import contextmanager
from unittest.mock import patch as _patch


@contextmanager
def _apply_patches(patches: dict):
    """Apply a dict of {target: mock_value} as nested context managers."""
    with _nested_patches(list(patches.items())):
        yield


@contextmanager
def _nested_patches(items):
    if not items:
        yield
        return
    target, mock_val = items[0]
    with _patch(target, mock_val):
        with _nested_patches(items[1:]):
            yield
