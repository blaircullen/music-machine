"""
Unit U2 — SENSE extensions.

Tests for:
  - mb_local.get_recording_metadata: isrcs list, backward-compat isrc, length_ms
  - mb_local.recordings_for_isrc
  - tagger.lookup_acoustid: ACOUSTID_RETAIN_SCORE, below_floor flag, dedup
  - audd_client._parse_audd_result: duration_ms from spotify block
  - audd_client.identify_track: no _record_usage on transport exception
  - audio_probe.decoded_duration_ms: happy path, bad returncode, missing binary
"""

import json
import subprocess
from unittest.mock import MagicMock, call, patch

import pytest

# ---------------------------------------------------------------------------
# mb_local tests
# ---------------------------------------------------------------------------


def _make_mock_conn(rows_by_query):
    """
    Build a psycopg2-style mock connection whose cursor returns rows based on
    which query fragment is executed.  rows_by_query is a list of
    (fragment, rows) pairs checked in order.
    """
    results = list(rows_by_query)

    class MockCursor:
        def __init__(self):
            self._rows = []

        def execute(self, sql, params=()):
            self._rows = []
            for fragment, rows in results:
                if fragment in sql:
                    self._rows = rows
                    break

        def fetchone(self):
            return self._rows[0] if self._rows else None

        def fetchall(self):
            return list(self._rows)

    class MockConn:
        def cursor(self):
            return MockCursor()

    return MockConn()


def _patch_pool(conn):
    """Context manager: patch mb_local._get_pool to return a fake pool."""
    mock_pool = MagicMock()
    mock_pool.getconn.return_value = conn
    mock_pool.putconn = MagicMock()
    return patch("mb_local._get_pool", return_value=mock_pool)


class TestGetRecordingMetadataIsrcs:
    """get_recording_metadata returns isrcs list + backward-compat isrc key."""

    def _build_conn(self, isrc_rows):
        return _make_mock_conn([
            # recording basic info
            ("FROM musicbrainz.recording r", [(42, "Test Title", 183100)]),
            # artist credits
            ("FROM musicbrainz.artist_credit_name", [("Test Artist", "")]),
            # release / track info — empty for simplicity
            ("FROM musicbrainz.track t", []),
            # ISRCs
            ("FROM musicbrainz.isrc", isrc_rows),
            # label — empty
            ("FROM musicbrainz.release_label", []),
            # composer — empty
            ("FROM musicbrainz.l_recording_work", []),
            # genre tags — empty
            ("FROM musicbrainz.recording_tag", []),
        ])

    def test_three_isrcs(self):
        import mb_local
        isrc_rows = [("USABC1234567",), ("USABC1234568",), ("USABC1234569",)]
        conn = self._build_conn(isrc_rows)
        with _patch_pool(conn):
            result = mb_local.get_recording_metadata("fake-mbid-0000")
        assert result is not None
        assert result["isrcs"] == ["USABC1234567", "USABC1234568", "USABC1234569"]
        # backward-compat: isrc is first
        assert result["isrc"] == "USABC1234567"

    def test_zero_isrcs(self):
        import mb_local
        conn = self._build_conn([])
        with _patch_pool(conn):
            result = mb_local.get_recording_metadata("fake-mbid-0001")
        assert result is not None
        assert result["isrcs"] == []
        assert result["isrc"] is None

    def test_length_ms_no_truncation(self):
        """length_ms must equal the raw MB value (no division, no truncation)."""
        import mb_local
        conn = self._build_conn([])
        with _patch_pool(conn):
            result = mb_local.get_recording_metadata("fake-mbid-0002")
        assert result is not None
        assert result["length_ms"] == 183100

    def test_length_ms_none_when_null(self):
        """When recording.length is NULL, length_ms must be None."""
        import mb_local
        # Override the recording row to have None for length
        conn = _make_mock_conn([
            ("FROM musicbrainz.recording r", [(42, "Test Title", None)]),
            ("FROM musicbrainz.artist_credit_name", [("Test Artist", "")]),
            ("FROM musicbrainz.track t", []),
            ("FROM musicbrainz.isrc", []),
            ("FROM musicbrainz.release_label", []),
            ("FROM musicbrainz.l_recording_work", []),
            ("FROM musicbrainz.recording_tag", []),
        ])
        with _patch_pool(conn):
            result = mb_local.get_recording_metadata("fake-mbid-0003")
        assert result is not None
        assert result["length_ms"] is None

    def test_pool_unavailable_returns_none(self):
        import mb_local
        with patch("mb_local._get_pool", return_value=None):
            result = mb_local.get_recording_metadata("fake-mbid-0004")
        assert result is None


class TestRecordingsForIsrc:
    """recordings_for_isrc returns list of MBIDs or [] on failure."""

    def test_returns_mbids(self):
        import mb_local
        mock_pool = MagicMock()
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = [
            ("aaaabbbb-0000-1111-2222-333333333333",),
            ("ccccdddd-0000-1111-2222-333333333333",),
        ]
        mock_conn.cursor.return_value = mock_cur
        mock_pool.getconn.return_value = mock_conn

        with patch("mb_local._get_pool", return_value=mock_pool):
            result = mb_local.recordings_for_isrc("USABC1234567")

        assert result == [
            "aaaabbbb-0000-1111-2222-333333333333",
            "ccccdddd-0000-1111-2222-333333333333",
        ]

    def test_mirror_unavailable_returns_empty(self):
        import mb_local
        with patch("mb_local._get_pool", return_value=None):
            result = mb_local.recordings_for_isrc("USABC1234567")
        assert result == []

    def test_query_exception_returns_empty(self):
        import mb_local
        mock_pool = MagicMock()
        mock_pool.getconn.side_effect = Exception("connection refused")

        with patch("mb_local._get_pool", return_value=mock_pool):
            result = mb_local.recordings_for_isrc("USABC1234567")
        assert result == []

    def test_no_mapping_returns_empty(self):
        import mb_local
        mock_pool = MagicMock()
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = []
        mock_conn.cursor.return_value = mock_cur
        mock_pool.getconn.return_value = mock_conn

        with patch("mb_local._get_pool", return_value=mock_pool):
            result = mb_local.recordings_for_isrc("USXXX9999999")
        assert result == []


# ---------------------------------------------------------------------------
# tagger.lookup_acoustid tests
# ---------------------------------------------------------------------------

_FAKE_ACOUSTID_RESPONSE = {
    "status": "ok",
    "results": [
        # score 0.9 — above floor, two recordings (dedup test on rec-B)
        {
            "score": 0.9,
            "recordings": [
                {"id": "rec-A"},
                {"id": "rec-B"},
            ],
        },
        # score 0.52 — above floor
        {"score": 0.52, "recordings": [{"id": "rec-C"}]},
        # score 0.49 — below floor but above retain
        {"score": 0.49, "recordings": [{"id": "rec-D"}]},
        # score 0.31 — below floor but above retain
        {"score": 0.31, "recordings": [{"id": "rec-B"}]},  # dup of rec-B (0.9 wins)
        # score 0.2 — below retain threshold, must be dropped
        {"score": 0.20, "recordings": [{"id": "rec-E"}]},
    ],
}


def _fake_urlopen(req, timeout=None):
    """Fake urllib.request.urlopen that returns _FAKE_ACOUSTID_RESPONSE."""
    resp = MagicMock()
    resp.read.return_value = json.dumps(_FAKE_ACOUSTID_RESPONSE).encode()
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


class TestLookupAcoustid:

    def _call(self, monkeypatch):
        import tagger
        import urllib.request
        monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
        return tagger.lookup_acoustid("fp-data", 180.0)

    def test_drops_below_retain(self, monkeypatch):
        """rec-E (0.2) must not appear in results."""
        results = self._call(monkeypatch)
        ids = [r["recording_id"] for r in results]
        assert "rec-E" not in ids

    def test_retains_four_candidates(self, monkeypatch):
        """Scores 0.9, 0.52, 0.49, 0.31 are retained (rec-B deduped)."""
        results = self._call(monkeypatch)
        ids = [r["recording_id"] for r in results]
        # rec-A, rec-B, rec-C, rec-D — 4 unique IDs
        assert set(ids) == {"rec-A", "rec-B", "rec-C", "rec-D"}

    def test_below_floor_annotation(self, monkeypatch):
        """rec-D (0.49) and rec-D-level entries have below_floor=True."""
        results = self._call(monkeypatch)
        by_id = {r["recording_id"]: r for r in results}
        assert by_id["rec-A"]["below_floor"] is False
        assert by_id["rec-B"]["below_floor"] is False  # 0.9 wins dedup
        assert by_id["rec-C"]["below_floor"] is False
        assert by_id["rec-D"]["below_floor"] is True

    def test_descending_order(self, monkeypatch):
        """Results must be sorted by score descending."""
        results = self._call(monkeypatch)
        scores = [r["score"] for r in results]
        assert scores == sorted(scores, reverse=True)

    def test_dedup_keeps_highest_score(self, monkeypatch):
        """rec-B appears at 0.9 and 0.31; only the 0.9 entry survives."""
        results = self._call(monkeypatch)
        rec_b = [r for r in results if r["recording_id"] == "rec-B"]
        assert len(rec_b) == 1
        assert rec_b[0]["score"] == 0.9


# ---------------------------------------------------------------------------
# audd_client tests
# ---------------------------------------------------------------------------


class TestParseAuddResult:
    """_parse_audd_result extracts duration_ms from spotify block."""

    def _result_with_spotify(self, duration_ms=215000):
        return {
            "artist": "Test Artist",
            "title": "Test Title",
            "album": "",
            "spotify": {
                "id": "spotify123",
                "duration_ms": duration_ms,
                "external_ids": {"isrc": "USABC1234567"},
                "album": {"name": "Test Album", "images": []},
            },
        }

    def _result_without_spotify(self):
        return {
            "artist": "Test Artist",
            "title": "Test Title",
            "album": "",
        }

    def test_duration_ms_from_spotify(self):
        from audd_client import _parse_audd_result
        result = _parse_audd_result(self._result_with_spotify(215000))
        assert result["duration_ms"] == 215000

    def test_duration_ms_none_without_spotify(self):
        from audd_client import _parse_audd_result
        result = _parse_audd_result(self._result_without_spotify())
        assert result["duration_ms"] is None

    def test_duration_ms_key_always_present(self):
        """Key must exist regardless of spotify presence."""
        from audd_client import _parse_audd_result
        result = _parse_audd_result(self._result_without_spotify())
        assert "duration_ms" in result


class TestIdentifyTrackUsageBilling:
    """Transport exceptions must NOT call _record_usage()."""

    def test_transport_exception_no_billing(self, monkeypatch, tmp_path):
        """OSError from urlopen → _record_usage must not be called."""
        import audd_client

        # Patch _get_api_key and check_budget so we get past the guards
        monkeypatch.setattr(audd_client, "_get_api_key", lambda: "test-key")
        monkeypatch.setattr(audd_client, "check_budget", lambda: True)

        # Patch _extract_sample to return a fake path (avoids ffmpeg dependency)
        fake_sample = tmp_path / "sample.mp3"
        fake_sample.write_bytes(b"\xff\xfb" * 100)
        monkeypatch.setattr(audd_client, "_extract_sample", lambda path: str(fake_sample))

        # Make urlopen raise a transport error
        import urllib.request as urllib_req

        def _raise_oserror(*args, **kwargs):
            raise OSError("Network unreachable")

        monkeypatch.setattr(urllib_req, "urlopen", _raise_oserror)

        record_usage_calls = []
        monkeypatch.setattr(audd_client, "_record_usage", lambda: record_usage_calls.append(1))

        result = audd_client.identify_track(str(fake_sample))

        assert result is None
        assert record_usage_calls == [], "Transport exception must not trigger _record_usage()"

    def test_api_response_bills(self, monkeypatch, tmp_path):
        """Successful API response (even no-match) must call _record_usage()."""
        import audd_client
        import urllib.request as urllib_req

        monkeypatch.setattr(audd_client, "_get_api_key", lambda: "test-key")
        monkeypatch.setattr(audd_client, "check_budget", lambda: True)

        fake_sample = tmp_path / "sample.mp3"
        fake_sample.write_bytes(b"\xff\xfb" * 100)
        monkeypatch.setattr(audd_client, "_extract_sample", lambda path: str(fake_sample))

        # API returns a "no result" success response
        def _fake_urlopen(req, timeout=None):
            resp = MagicMock()
            resp.read.return_value = json.dumps({"status": "success", "result": None}).encode()
            resp.__enter__ = lambda s: s
            resp.__exit__ = MagicMock(return_value=False)
            return resp

        monkeypatch.setattr(urllib_req, "urlopen", _fake_urlopen)

        record_usage_calls = []
        monkeypatch.setattr(audd_client, "_record_usage", lambda: record_usage_calls.append(1))

        result = audd_client.identify_track(str(fake_sample))

        assert result is None  # No match
        assert len(record_usage_calls) == 1, "API response must trigger _record_usage() once"


# ---------------------------------------------------------------------------
# audio_probe tests
# ---------------------------------------------------------------------------


class TestDecodedDurationMs:

    def _fake_ffprobe_output(self):
        return json.dumps({"format": {"duration": "183.245"}})

    def test_happy_path(self, monkeypatch):
        """Valid ffprobe output: 183.245 s → 183245 ms."""
        import audio_probe

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = self._fake_ffprobe_output()

        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: mock_result)
        assert audio_probe.decoded_duration_ms("/fake/track.flac") == 183245

    def test_nonzero_returncode_returns_none(self, monkeypatch):
        """ffprobe non-zero exit → None."""
        import audio_probe

        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""

        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: mock_result)
        assert audio_probe.decoded_duration_ms("/fake/track.flac") is None

    def test_file_not_found_returns_none(self, monkeypatch):
        """FileNotFoundError (ffprobe not on PATH) → None."""
        import audio_probe

        def _raise(*args, **kwargs):
            raise FileNotFoundError("ffprobe not found")

        monkeypatch.setattr(subprocess, "run", _raise)
        assert audio_probe.decoded_duration_ms("/fake/track.flac") is None

    def test_invalid_json_returns_none(self, monkeypatch):
        """Malformed ffprobe output → None (no crash)."""
        import audio_probe

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "not json"

        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: mock_result)
        assert audio_probe.decoded_duration_ms("/fake/track.flac") is None

    def test_ms_truncation(self, monkeypatch):
        """int() truncates fractional ms (183245.7 → 183245)."""
        import audio_probe

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"format": {"duration": "183.2457"}})

        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: mock_result)
        # 183.2457 * 1000 = 183245.7 → int = 183245
        assert audio_probe.decoded_duration_ms("/fake/track.flac") == 183245
