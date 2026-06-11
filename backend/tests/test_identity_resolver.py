"""
Table-driven tests for backend/identity_resolver.py.

Covers every scenario from the U4 plan test table:
  - T1 happy / multi-mapping / duration-veto
  - T2 happy / no-corroboration / missing AudD
  - T4 happy
  - Divergence: T2 alone → review/divergent; + unique ISRC → confirmed/T1
  - Ambiguity: 0.93/0.91 different ids; same id twice; sub-floor rival 0.52/0.49
  - Dead-band boundaries: 3000/3001/4000/4001 ms
  - Short 30 s track
  - Conflict
  - Deferred: budget exhausted / audd not attempted / mirror unavailable
  - Error: file_missing / fpcalc_failed
  - NULL duration never confirms
  - Veto-precedes-tier ordering assertion
  - Evidence trace asserted in every scenario
"""

import pytest
from identity_resolver import (
    resolve,
    RESOLVER_VERSION,
    CONFIRM_MS,
    DEADBAND_MS,
    SHORT_TRACK_MS,
    AMBIGUITY_GAP,
    ARTIST_SIM,
    TITLE_SIM,
    T4_MIN_SCORE,
    DIVERGENCE_SIM,
)


# ---------------------------------------------------------------------------
# Evidence builder helpers
# ---------------------------------------------------------------------------

def _cand(
    recording_id="rid-A",
    score=0.90,
    below_floor=False,
    artist="Beck",
    title="Loser",
    length_ms=200_000,
    isrcs=None,
    release_id="rel-1",
    album="Mellow Gold",
    date="1994",
    track_no=1,
):
    """Build a single AcoustID candidate dict."""
    return {
        "recording_id": recording_id,
        "score": score,
        "below_floor": below_floor,
        "artist": artist,
        "title": title,
        "length_ms": length_ms,
        "isrcs": isrcs or [],
        "release_id": release_id,
        "album": album,
        "date": date,
        "track_no": track_no,
    }


def _audd(
    artist="Beck",
    title="Loser",
    album="Mellow Gold",
    isrc="USRC11400001",
    audd_score=0.95,
    duration_ms=200_000,
):
    """Build an AudD result dict."""
    return {
        "artist": artist,
        "title": title,
        "album": album,
        "isrc": isrc,
        "audd_score": audd_score,
        "duration_ms": duration_ms,
    }


def _evidence(
    file_exists=True,
    duration_ms=200_000,
    existing_artist="Beck",
    existing_title="Loser",
    acoustid_candidates=None,
    audd=None,
    audd_attempted=True,
    audd_budget_exhausted=False,
    mirror_available=True,
    isrc_recording_map=None,
    fingerprint_ok=True,
):
    """Build a complete evidence dict with sane defaults."""
    return {
        "file_exists": file_exists,
        "duration_ms": duration_ms,
        "existing_artist": existing_artist,
        "existing_title": existing_title,
        "acoustid_candidates": acoustid_candidates if acoustid_candidates is not None else [],
        "audd": audd,
        "audd_attempted": audd_attempted,
        "audd_budget_exhausted": audd_budget_exhausted,
        "mirror_available": mirror_available,
        "isrc_recording_map": isrc_recording_map or {},
        "fingerprint_ok": fingerprint_ok,
    }


# ---------------------------------------------------------------------------
# Assertion helpers
# ---------------------------------------------------------------------------

def _assert_evidence_trace(v):
    """Every verdict must carry a well-formed evidence trace."""
    ev = v["evidence"]
    assert isinstance(ev["vetoes_fired"], list), "vetoes_fired must be list"
    assert isinstance(ev["tier_trace"], list), "tier_trace must be list"
    assert isinstance(ev["comparisons"], dict), "comparisons must be dict"
    assert isinstance(ev["candidates"], list), "candidates must be list"
    assert v["resolver_version"] == RESOLVER_VERSION


# ---------------------------------------------------------------------------
# U4 T1 scenarios
# ---------------------------------------------------------------------------

class TestT1HappyPath:
    """T1: ISRC unique-recording + Δdur ≤ 3000 ms → confirmed."""

    def test_t1_happy_delta_1500ms(self):
        isrc = "USRC11400001"
        cand = _cand(recording_id="rid-A", isrcs=[isrc], length_ms=200_000)
        ev = _evidence(
            duration_ms=201_500,           # Δ = 1500 ms → confirm
            acoustid_candidates=[cand],
            audd=_audd(isrc=isrc),
            isrc_recording_map={isrc: ["rid-A"]},
        )
        v = resolve(ev)
        assert v["state"] == "confirmed"
        assert v["tier"] == "T1"
        assert v["mb_recording_id"] == "rid-A"
        assert v["isrc"] == isrc
        assert v["divergent"] is False
        _assert_evidence_trace(v)

    def test_t1_evidence_trace_contains_t1_tier(self):
        isrc = "USRC11400001"
        cand = _cand(recording_id="rid-A", isrcs=[isrc], length_ms=200_000)
        ev = _evidence(
            duration_ms=200_000,
            acoustid_candidates=[cand],
            audd=_audd(isrc=isrc),
            isrc_recording_map={isrc: ["rid-A"]},
        )
        v = resolve(ev)
        tiers = [t["tier"] for t in v["evidence"]["tier_trace"]]
        assert "T1" in tiers

    def test_t1_isrc_multi_mapping_two_recordings_review(self):
        """ISRC maps to 2 recordings → review (T1 blocked, KTD 3)."""
        isrc = "USRC11400001"
        cand = _cand(recording_id="rid-A", isrcs=[isrc], length_ms=200_000)
        ev = _evidence(
            duration_ms=200_000,
            acoustid_candidates=[cand],
            audd=_audd(isrc=isrc),
            isrc_recording_map={isrc: ["rid-A", "rid-B"]},  # two recordings
        )
        v = resolve(ev)
        assert v["state"] == "review"
        assert v["tier"] != "T1" or v["state"] != "confirmed"
        _assert_evidence_trace(v)
        # isrc_multi_mapping veto should be in vetoes_fired
        assert "isrc_multi_mapping" in v["evidence"]["vetoes_fired"]

    def test_t1_duration_delta_6000ms_disqualified(self):
        """Δ 6000 ms > 4000 ms → T1 candidate disqualified, falls through."""
        isrc = "USRC11400001"
        cand = _cand(recording_id="rid-A", isrcs=[isrc], length_ms=200_000)
        ev = _evidence(
            duration_ms=206_000,           # Δ = 6000 ms → veto on candidate
            acoustid_candidates=[cand],
            audd=_audd(isrc=isrc),
            isrc_recording_map={isrc: ["rid-A"]},
        )
        v = resolve(ev)
        # T1 should NOT confirm because duration is a hard veto
        assert v["state"] != "confirmed" or v["tier"] != "T1"
        _assert_evidence_trace(v)


# ---------------------------------------------------------------------------
# U4 T1 dead-band boundary tests
# ---------------------------------------------------------------------------

class TestT1DeadBandBoundaries:
    """Boundary tests: 3000/3001/4000/4001 ms on T1 path."""

    def _t1_ev(self, delta_ms):
        isrc = "USRC11400001"
        ref = 200_000
        cand = _cand(recording_id="rid-A", isrcs=[isrc], length_ms=ref)
        return _evidence(
            duration_ms=ref + delta_ms,
            acoustid_candidates=[cand],
            audd=_audd(isrc=isrc),
            isrc_recording_map={isrc: ["rid-A"]},
        )

    def test_t1_delta_3000_confirms(self):
        v = resolve(self._t1_ev(3000))
        assert v["state"] == "confirmed"
        assert v["tier"] == "T1"
        _assert_evidence_trace(v)

    def test_t1_delta_3001_deadband_review(self):
        v = resolve(self._t1_ev(3001))
        assert v["state"] == "review"
        assert "dead_band" in v["evidence"]["vetoes_fired"]
        _assert_evidence_trace(v)

    def test_t1_delta_4000_deadband_review(self):
        v = resolve(self._t1_ev(4000))
        assert v["state"] == "review"
        assert "dead_band" in v["evidence"]["vetoes_fired"]
        _assert_evidence_trace(v)

    def test_t1_delta_4001_disqualified_falls_through(self):
        """Δ 4001 ms → hard veto, T1 candidate disqualified."""
        v = resolve(self._t1_ev(4001))
        # Not confirmed via T1; could fall to T2/T4/review/unknown
        assert not (v["state"] == "confirmed" and v["tier"] == "T1")
        _assert_evidence_trace(v)


# ---------------------------------------------------------------------------
# U4 T2 scenarios
# ---------------------------------------------------------------------------

class TestT2HappyPath:
    """T2: AcoustID ≈ AudD + decoded-duration corroboration → confirmed."""

    def test_t2_happy_with_duration_corroboration(self):
        """No ISRC overlap → T1 skipped; T2 confirms with matching artists/titles + dur."""
        cand = _cand(recording_id="rid-A", isrcs=[], length_ms=200_000,
                     artist="Beck", title="Loser")
        ev = _evidence(
            duration_ms=200_000,           # Δ = 0 → confirm
            acoustid_candidates=[cand],
            audd=_audd(artist="Beck", title="Loser", isrc=""),
            isrc_recording_map={},
        )
        v = resolve(ev)
        assert v["state"] == "confirmed"
        assert v["tier"] == "T2"
        _assert_evidence_trace(v)

    def test_t2_no_duration_corroboration_review(self):
        """T2 without duration corroboration (delta > 4000 ms) → not confirmed."""
        cand = _cand(recording_id="rid-A", isrcs=[], length_ms=200_000,
                     artist="Beck", title="Loser")
        ev = _evidence(
            duration_ms=210_000,           # Δ = 10000 ms → hard veto
            acoustid_candidates=[cand],
            audd=_audd(artist="Beck", title="Loser", isrc=""),
            isrc_recording_map={},
        )
        v = resolve(ev)
        # T2 cannot confirm; hard dur veto disqualifies candidate
        assert not (v["state"] == "confirmed" and v["tier"] == "T2")
        _assert_evidence_trace(v)

    def test_t2_null_duration_no_confirm(self):
        """NULL decoded duration → T2 cannot confirm (KTD 5)."""
        cand = _cand(recording_id="rid-A", isrcs=[], length_ms=200_000,
                     artist="Beck", title="Loser")
        ev = _evidence(
            duration_ms=None,              # NULL duration
            acoustid_candidates=[cand],
            audd=_audd(artist="Beck", title="Loser", isrc=""),
            isrc_recording_map={},
        )
        v = resolve(ev)
        assert v["state"] != "confirmed"
        _assert_evidence_trace(v)
        assert v["evidence"]["comparisons"].get("duration_unavailable") is True

    def test_t2_audd_missing_falls_to_t4(self):
        """AudD missing → T2 not attempted; may fall to T4."""
        cand = _cand(recording_id="rid-A", score=0.85, isrcs=[],
                     length_ms=200_000, artist="Beck", title="Loser")
        ev = _evidence(
            duration_ms=200_000,
            existing_artist="Beck",
            existing_title="Loser",
            acoustid_candidates=[cand],
            audd=None,
            audd_attempted=True,
            isrc_recording_map={},
        )
        v = resolve(ev)
        # T2 not attempted; T4 should confirm
        t2_entries = [t for t in v["evidence"]["tier_trace"] if t["tier"] == "T2"]
        if t2_entries:
            assert not t2_entries[0].get("attempted", True) or t2_entries[0].get("result") == "audd_missing"
        _assert_evidence_trace(v)

    def test_t2_artist_sim_below_threshold_no_confirm(self):
        """Artist sim < ARTIST_SIM → T2 fails."""
        cand = _cand(recording_id="rid-A", isrcs=[], length_ms=200_000,
                     artist="Beck", title="Loser")
        ev = _evidence(
            duration_ms=200_000,
            acoustid_candidates=[cand],
            audd=_audd(artist="Completely Different Artist", title="Loser", isrc=""),
            isrc_recording_map={},
        )
        v = resolve(ev)
        assert not (v["state"] == "confirmed" and v["tier"] == "T2")
        _assert_evidence_trace(v)

    def test_t2_title_sim_below_threshold_no_confirm(self):
        """Title sim < TITLE_SIM → T2 fails."""
        cand = _cand(recording_id="rid-A", isrcs=[], length_ms=200_000,
                     artist="Beck", title="Loser")
        ev = _evidence(
            duration_ms=200_000,
            acoustid_candidates=[cand],
            audd=_audd(artist="Beck", title="Completely Different Title", isrc=""),
            isrc_recording_map={},
        )
        v = resolve(ev)
        assert not (v["state"] == "confirmed" and v["tier"] == "T2")
        _assert_evidence_trace(v)


# ---------------------------------------------------------------------------
# U4 T2 dead-band boundaries
# ---------------------------------------------------------------------------

class TestT2DeadBandBoundaries:
    """Dead-band boundaries on T2 path."""

    def _t2_ev(self, delta_ms):
        ref = 200_000
        cand = _cand(recording_id="rid-A", isrcs=[], length_ms=ref,
                     artist="Beck", title="Loser")
        return _evidence(
            duration_ms=ref + delta_ms,
            acoustid_candidates=[cand],
            audd=_audd(artist="Beck", title="Loser", isrc=""),
            isrc_recording_map={},
        )

    def test_t2_delta_3000_confirms(self):
        v = resolve(self._t2_ev(3000))
        assert v["state"] == "confirmed"
        assert v["tier"] == "T2"
        _assert_evidence_trace(v)

    def test_t2_delta_3001_deadband_review(self):
        v = resolve(self._t2_ev(3001))
        assert v["state"] == "review"
        assert "dead_band" in v["evidence"]["vetoes_fired"]
        _assert_evidence_trace(v)

    def test_t2_delta_4000_deadband_review(self):
        v = resolve(self._t2_ev(4000))
        assert v["state"] == "review"
        assert "dead_band" in v["evidence"]["vetoes_fired"]
        _assert_evidence_trace(v)

    def test_t2_delta_4001_disqualified(self):
        v = resolve(self._t2_ev(4001))
        assert not (v["state"] == "confirmed" and v["tier"] == "T2")
        _assert_evidence_trace(v)


# ---------------------------------------------------------------------------
# U4 T4 scenarios
# ---------------------------------------------------------------------------

class TestT4HappyPath:
    """T4: AcoustID ≈ existing tags, score ≥ 0.7, dur confirm → confirmed."""

    def test_t4_happy_no_api_cost(self):
        """T4 path: no AudD needed — tags match AcoustID, score high, dur ok."""
        cand = _cand(recording_id="rid-A", score=0.85, isrcs=[],
                     length_ms=200_000, artist="Beck", title="Loser")
        ev = _evidence(
            duration_ms=200_000,
            existing_artist="Beck",
            existing_title="Loser",
            acoustid_candidates=[cand],
            audd=None,
            audd_attempted=True,
            isrc_recording_map={},
        )
        v = resolve(ev)
        assert v["state"] == "confirmed"
        assert v["tier"] == "T4"
        _assert_evidence_trace(v)

    def test_t4_score_below_minimum_no_confirm(self):
        """AcoustID score < T4_MIN_SCORE → T4 fails."""
        cand = _cand(recording_id="rid-A", score=T4_MIN_SCORE - 0.01, isrcs=[],
                     length_ms=200_000, artist="Beck", title="Loser")
        ev = _evidence(
            duration_ms=200_000,
            existing_artist="Beck",
            existing_title="Loser",
            acoustid_candidates=[cand],
            audd=None,
            audd_attempted=True,
            isrc_recording_map={},
        )
        v = resolve(ev)
        assert not (v["state"] == "confirmed" and v["tier"] == "T4")
        _assert_evidence_trace(v)

    def test_t4_no_existing_tags_not_attempted(self):
        """T4 requires existing artist+title tags; absent → T4 not attempted."""
        cand = _cand(recording_id="rid-A", score=0.90, isrcs=[],
                     length_ms=200_000, artist="Beck", title="Loser")
        ev = _evidence(
            duration_ms=200_000,
            existing_artist=None,
            existing_title=None,
            acoustid_candidates=[cand],
            audd=None,
            audd_attempted=True,
            isrc_recording_map={},
        )
        v = resolve(ev)
        assert not (v["state"] == "confirmed" and v["tier"] == "T4")
        _assert_evidence_trace(v)

    def test_t4_null_duration_no_confirm(self):
        """NULL duration → T4 cannot confirm."""
        cand = _cand(recording_id="rid-A", score=0.90, isrcs=[],
                     length_ms=200_000, artist="Beck", title="Loser")
        ev = _evidence(
            duration_ms=None,
            existing_artist="Beck",
            existing_title="Loser",
            acoustid_candidates=[cand],
            audd=None,
            audd_attempted=True,
            isrc_recording_map={},
        )
        v = resolve(ev)
        assert v["state"] != "confirmed"
        _assert_evidence_trace(v)


# ---------------------------------------------------------------------------
# U4 Divergence scenarios
# ---------------------------------------------------------------------------

class TestDivergence:
    """
    Divergence: winner artist vs existing_artist similar() < DIVERGENCE_SIM.
    T1: confirms anyway (divergent=True).
    T2: downgrades to review/divergent.
    T4: impossible by construction (requires existing tags to match).
    """

    def test_divergence_t2_alone_review_divergent(self):
        """
        Tags say "Beck", AcoustID+AudD both say "Ella Henderson" →
        T2 with divergence → review + divergent=True.
        (The "Ella Henderson" scenario from the plan.)
        """
        cand = _cand(recording_id="rid-ella", isrcs=[], length_ms=220_000,
                     artist="Ella Henderson", title="Ghost")
        ev = _evidence(
            duration_ms=220_000,
            existing_artist="Beck",       # differs from Ella Henderson
            existing_title="Ghost",
            acoustid_candidates=[cand],
            audd=_audd(artist="Ella Henderson", title="Ghost", isrc=""),
            isrc_recording_map={},
        )
        v = resolve(ev)
        assert v["state"] == "review"
        assert v["divergent"] is True
        assert "divergent_artist" in v["evidence"].get("reason", "")
        _assert_evidence_trace(v)
        # tier_trace should show a divergence_downgrade entry for T2
        downgrades = [
            t for t in v["evidence"]["tier_trace"]
            if t.get("divergence_downgrade")
        ]
        assert downgrades, "Expected a T2 divergence_downgrade tier_trace entry"

    def test_divergence_t1_confirms_anyway(self):
        """
        Tags say "Beck", but T1 (unique ISRC) proves Ella Henderson →
        confirmed, divergent=True.  T1 overrides divergence.
        """
        isrc = "GBELH1400001"
        cand = _cand(recording_id="rid-ella", isrcs=[isrc], length_ms=220_000,
                     artist="Ella Henderson", title="Ghost")
        ev = _evidence(
            duration_ms=220_000,
            existing_artist="Beck",
            existing_title="Ghost",
            acoustid_candidates=[cand],
            audd=_audd(artist="Ella Henderson", title="Ghost", isrc=isrc),
            isrc_recording_map={isrc: ["rid-ella"]},
        )
        v = resolve(ev)
        assert v["state"] == "confirmed"
        assert v["tier"] == "T1"
        assert v["divergent"] is True
        _assert_evidence_trace(v)

    def test_divergence_t4_impossible_by_construction(self):
        """
        T4 requires artist tag to match AcoustID (sim ≥ ARTIST_SIM).
        If existing_artist is "Beck" but winner is "Ella Henderson",
        T4 artist_sim check fails — T4 cannot fire for a divergent identity.
        """
        cand = _cand(recording_id="rid-ella", score=0.90, isrcs=[],
                     length_ms=220_000, artist="Ella Henderson", title="Ghost")
        ev = _evidence(
            duration_ms=220_000,
            existing_artist="Beck",        # divergent from candidate
            existing_title="Ghost",
            acoustid_candidates=[cand],
            audd=None,
            audd_attempted=True,
            isrc_recording_map={},
        )
        v = resolve(ev)
        assert not (v["state"] == "confirmed" and v["tier"] == "T4")
        _assert_evidence_trace(v)


# ---------------------------------------------------------------------------
# U4 Ambiguity veto
# ---------------------------------------------------------------------------

class TestAmbiguityVeto:
    """
    Ambiguity veto fires when top-2 recording_ids differ AND score gap ≤ 0.05,
    evaluated on ALL ≥0.3 retained candidates (KTD 6).
    """

    def test_ambiguity_0_93_0_91_different_ids_review(self):
        """Top-2 different recording ids, gap 0.02 ≤ 0.05 → review."""
        c1 = _cand(recording_id="rid-A", score=0.93)
        c2 = _cand(recording_id="rid-B", score=0.91)
        ev = _evidence(
            duration_ms=200_000,
            acoustid_candidates=[c1, c2],
            audd=_audd(),
            isrc_recording_map={},
        )
        v = resolve(ev)
        assert v["state"] == "review"
        assert "ambiguous" in v["evidence"]["vetoes_fired"]
        _assert_evidence_trace(v)

    def test_ambiguity_same_recording_id_twice_no_veto(self):
        """Two candidates with the same recording_id — strengthens, no veto."""
        c1 = _cand(recording_id="rid-A", score=0.93, isrcs=["USRC11400001"])
        c2 = _cand(recording_id="rid-A", score=0.91, isrcs=["USRC11400001"])
        isrc = "USRC11400001"
        ev = _evidence(
            duration_ms=200_000,
            acoustid_candidates=[c1, c2],
            audd=_audd(isrc=isrc),
            isrc_recording_map={isrc: ["rid-A"]},
        )
        v = resolve(ev)
        assert "ambiguous" not in v["evidence"]["vetoes_fired"]
        _assert_evidence_trace(v)

    def test_ambiguity_sub_floor_rival_veto_fires(self):
        """
        Top 0.52 (participant), rival 0.49 (below_floor but retained ≥0.3).
        Ambiguity veto runs on ALL ≥0.3 candidates including below_floor.
        Gap = 0.03 ≤ 0.05 with different ids → veto fires.
        """
        c1 = _cand(recording_id="rid-A", score=0.52, below_floor=False)
        c2 = _cand(recording_id="rid-B", score=0.49, below_floor=True)
        ev = _evidence(
            duration_ms=200_000,
            acoustid_candidates=[c1, c2],
            audd=_audd(),
            isrc_recording_map={},
        )
        v = resolve(ev)
        assert v["state"] == "review"
        assert "ambiguous" in v["evidence"]["vetoes_fired"]
        _assert_evidence_trace(v)

    def test_ambiguity_same_artist_different_recording_veto_fires(self):
        """
        Same artist (both Beck), but different recording ids (live vs studio).
        Ambiguity is recording-id keyed — artist match doesn't exempt it.
        """
        c1 = _cand(recording_id="rid-studio", score=0.88, artist="Beck", title="Loser")
        c2 = _cand(recording_id="rid-live", score=0.85, artist="Beck", title="Loser (Live)")
        ev = _evidence(
            duration_ms=200_000,
            acoustid_candidates=[c1, c2],
            audd=_audd(artist="Beck", title="Loser"),
            isrc_recording_map={},
        )
        v = resolve(ev)
        assert v["state"] == "review"
        assert "ambiguous" in v["evidence"]["vetoes_fired"]
        _assert_evidence_trace(v)

    def test_ambiguity_gap_above_threshold_no_veto(self):
        """Gap > AMBIGUITY_GAP (0.05) → no ambiguity veto."""
        c1 = _cand(recording_id="rid-A", score=0.93)
        c2 = _cand(recording_id="rid-B", score=0.80)  # gap = 0.13
        isrc = "USRC11400001"
        c1["isrcs"] = [isrc]
        ev = _evidence(
            duration_ms=200_000,
            acoustid_candidates=[c1, c2],
            audd=_audd(isrc=isrc),
            isrc_recording_map={isrc: ["rid-A"]},
        )
        v = resolve(ev)
        assert "ambiguous" not in v["evidence"]["vetoes_fired"]
        _assert_evidence_trace(v)


# ---------------------------------------------------------------------------
# U4 Veto-ordering assertion
# ---------------------------------------------------------------------------

class TestVetoOrdering:
    """
    Vetoes must fire BEFORE tier assignment.
    An input that would otherwise T1-confirm but has an ambiguity condition
    must resolve to review (veto wins), not confirmed.
    """

    def test_ambiguity_blocks_t1_confirm(self):
        """
        Unique ISRC + dur ok → would be T1-confirmed.
        But top-2 different recording ids with gap ≤ 0.05 → review wins.
        """
        isrc = "USRC11400001"
        c1 = _cand(recording_id="rid-A", score=0.93, isrcs=[isrc], length_ms=200_000)
        c2 = _cand(recording_id="rid-B", score=0.91, isrcs=[])
        ev = _evidence(
            duration_ms=200_000,
            acoustid_candidates=[c1, c2],
            audd=_audd(isrc=isrc),
            isrc_recording_map={isrc: ["rid-A"]},
        )
        v = resolve(ev)
        # Ambiguity veto fires → review, even though T1 would have confirmed
        assert v["state"] == "review"
        assert "ambiguous" in v["evidence"]["vetoes_fired"]
        _assert_evidence_trace(v)
        # Confirm T1 tier was evaluated (trace present) but overridden
        t1_entries = [t for t in v["evidence"]["tier_trace"] if t["tier"] == "T1"]
        assert t1_entries, "T1 tier trace must be present even when veto overrides"


# ---------------------------------------------------------------------------
# U4 Short track veto
# ---------------------------------------------------------------------------

class TestShortTrackVeto:
    """File duration < 45 s → review(short)."""

    def test_short_30s_review(self):
        cand = _cand(recording_id="rid-A", isrcs=["USRC11400001"], length_ms=30_000)
        ev = _evidence(
            duration_ms=30_000,            # 30 s < 45 s
            acoustid_candidates=[cand],
            audd=_audd(isrc="USRC11400001"),
            isrc_recording_map={"USRC11400001": ["rid-A"]},
        )
        v = resolve(ev)
        assert v["state"] == "review"
        assert "short" in v["evidence"]["vetoes_fired"]
        _assert_evidence_trace(v)

    def test_45s_not_short(self):
        """Exactly SHORT_TRACK_MS is NOT short (boundary not included)."""
        cand = _cand(recording_id="rid-A", score=0.90, isrcs=[], length_ms=SHORT_TRACK_MS)
        ev = _evidence(
            duration_ms=SHORT_TRACK_MS,
            existing_artist="Beck",
            existing_title="Loser",
            acoustid_candidates=[cand],
            audd=None,
            audd_attempted=True,
            isrc_recording_map={},
        )
        v = resolve(ev)
        assert "short" not in v["evidence"]["vetoes_fired"]
        _assert_evidence_trace(v)


# ---------------------------------------------------------------------------
# U4 Conflict veto
# ---------------------------------------------------------------------------

class TestConflictVeto:
    """AudD vs AcoustID both artist AND title sim < 0.3 → conflict."""

    def test_hard_contradiction_conflict(self):
        """AudD says 'Drake, God's Plan'; AcoustID says 'Beck, Loser' → conflict."""
        cand = _cand(recording_id="rid-A", artist="Beck", title="Loser")
        ev = _evidence(
            duration_ms=200_000,
            acoustid_candidates=[cand],
            audd=_audd(artist="Drake", title="God's Plan", isrc=""),
            isrc_recording_map={},
        )
        v = resolve(ev)
        assert v["state"] == "conflict"
        assert "conflict" in v["evidence"]["vetoes_fired"]
        _assert_evidence_trace(v)

    def test_title_match_artist_mismatch_not_conflict(self):
        """
        Only artist differs (title similar) → NOT conflict
        (both fields must contradict per plan).
        """
        cand = _cand(recording_id="rid-A", artist="Beck", title="Loser")
        ev = _evidence(
            duration_ms=200_000,
            acoustid_candidates=[cand],
            audd=_audd(artist="Completely Different", title="Loser", isrc=""),
            isrc_recording_map={},
        )
        v = resolve(ev)
        assert v["state"] != "conflict"
        _assert_evidence_trace(v)

    def test_no_audd_no_conflict(self):
        """No AudD → conflict veto cannot fire."""
        cand = _cand(recording_id="rid-A")
        ev = _evidence(
            duration_ms=200_000,
            acoustid_candidates=[cand],
            audd=None,
            audd_attempted=True,
            isrc_recording_map={},
        )
        v = resolve(ev)
        assert "conflict" not in v["evidence"]["vetoes_fired"]
        _assert_evidence_trace(v)


# ---------------------------------------------------------------------------
# U4 Deferred scenarios
# ---------------------------------------------------------------------------

class TestDeferred:
    """Sensing incomplete → deferred."""

    def test_audd_budget_exhausted_deferred(self):
        """AudD budget exhausted → deferred."""
        cand = _cand(recording_id="rid-A", score=0.55)
        ev = _evidence(
            duration_ms=200_000,
            acoustid_candidates=[cand],
            audd=None,
            audd_attempted=True,
            audd_budget_exhausted=True,
            mirror_available=True,
            isrc_recording_map={},
        )
        v = resolve(ev)
        assert v["state"] == "deferred"
        _assert_evidence_trace(v)

    def test_audd_not_attempted_deferred(self):
        """AudD not attempted, budget not exhausted → deferred (can still try)."""
        cand = _cand(recording_id="rid-A", score=0.55)
        ev = _evidence(
            duration_ms=200_000,
            acoustid_candidates=[cand],
            audd=None,
            audd_attempted=False,
            audd_budget_exhausted=False,
            mirror_available=True,
            isrc_recording_map={},
        )
        v = resolve(ev)
        assert v["state"] == "deferred"
        _assert_evidence_trace(v)

    def test_mirror_unavailable_deferred(self):
        """Mirror down → deferred."""
        ev = _evidence(
            duration_ms=200_000,
            acoustid_candidates=[],
            audd=None,
            audd_attempted=True,
            audd_budget_exhausted=False,
            mirror_available=False,
            isrc_recording_map={},
        )
        v = resolve(ev)
        assert v["state"] == "deferred"
        assert "mirror_unavailable" in v["evidence"].get("reason", "")
        _assert_evidence_trace(v)


# ---------------------------------------------------------------------------
# U4 Error scenarios
# ---------------------------------------------------------------------------

class TestError:
    """Mechanical failure → error."""

    def test_file_missing_error(self):
        ev = _evidence(file_exists=False)
        v = resolve(ev)
        assert v["state"] == "error"
        assert "file_missing" in v["evidence"]["vetoes_fired"]
        assert v["evidence"].get("reason") == "file_missing"
        _assert_evidence_trace(v)

    def test_fpcalc_failed_error(self):
        ev = _evidence(fingerprint_ok=False)
        v = resolve(ev)
        assert v["state"] == "error"
        assert "fpcalc_failed" in v["evidence"]["vetoes_fired"]
        assert v["evidence"].get("reason") == "fpcalc_failed"
        _assert_evidence_trace(v)

    def test_file_missing_takes_priority_over_fpcalc(self):
        """file_exists=False + fingerprint_ok=False → file_missing wins."""
        ev = _evidence(file_exists=False, fingerprint_ok=False)
        v = resolve(ev)
        assert v["state"] == "error"
        assert "file_missing" in v["evidence"]["vetoes_fired"]


# ---------------------------------------------------------------------------
# U4 Unknown fallthrough
# ---------------------------------------------------------------------------

class TestUnknown:
    """All sensing done, no tier fires → unknown."""

    def test_all_sensing_done_no_tier_unknown(self):
        """AudD attempted, budget not exhausted, no candidates → unknown."""
        ev = _evidence(
            duration_ms=200_000,
            acoustid_candidates=[],
            audd=None,
            audd_attempted=True,
            audd_budget_exhausted=False,
            mirror_available=True,
            isrc_recording_map={},
        )
        v = resolve(ev)
        assert v["state"] == "unknown"
        _assert_evidence_trace(v)

    def test_null_duration_with_all_sensing_done(self):
        """
        NULL duration + AudD attempted + no tiers fire → unknown.
        Specifically, NULL duration must never silently confirm anything.
        """
        cand = _cand(recording_id="rid-A", score=0.90, isrcs=[], length_ms=200_000,
                     artist="Beck", title="Loser")
        ev = _evidence(
            duration_ms=None,
            existing_artist="Beck",
            existing_title="Loser",
            acoustid_candidates=[cand],
            audd=_audd(artist="Beck", title="Loser", isrc=""),
            audd_attempted=True,
            isrc_recording_map={},
        )
        v = resolve(ev)
        assert v["state"] != "confirmed", "NULL duration must never yield confirmed"
        _assert_evidence_trace(v)


# ---------------------------------------------------------------------------
# U4 RESOLVER_VERSION constant
# ---------------------------------------------------------------------------

class TestResolverVersion:
    def test_resolver_version_present_in_every_verdict(self):
        cases = [
            _evidence(file_exists=False),
            _evidence(fingerprint_ok=False),
            _evidence(acoustid_candidates=[], audd=None, audd_attempted=True,
                      audd_budget_exhausted=False, mirror_available=True,
                      isrc_recording_map={}),
        ]
        for ev in cases:
            v = resolve(ev)
            assert v["resolver_version"] == RESOLVER_VERSION

    def test_resolver_version_is_string(self):
        assert isinstance(RESOLVER_VERSION, str)


# ---------------------------------------------------------------------------
# U4 Evidence trace completeness
# ---------------------------------------------------------------------------

class TestEvidenceTrace:
    """Evidence trace must be complete on every path, not just happy paths."""

    def test_evidence_trace_on_confirmed(self):
        isrc = "USRC11400001"
        cand = _cand(recording_id="rid-A", isrcs=[isrc], length_ms=200_000)
        ev = _evidence(
            duration_ms=200_000,
            acoustid_candidates=[cand],
            audd=_audd(isrc=isrc),
            isrc_recording_map={isrc: ["rid-A"]},
        )
        v = resolve(ev)
        assert v["state"] == "confirmed"
        ev_trace = v["evidence"]
        assert "candidates" in ev_trace
        assert "audd_present" in ev_trace
        assert "audd_attempted" in ev_trace
        assert "file_ms" in ev_trace
        assert "vetoes_fired" in ev_trace
        assert "tier_trace" in ev_trace
        assert "comparisons" in ev_trace

    def test_evidence_trace_on_error(self):
        v = resolve(_evidence(file_exists=False))
        _assert_evidence_trace(v)

    def test_evidence_trace_on_conflict(self):
        cand = _cand(recording_id="rid-A", artist="Beck", title="Loser")
        ev = _evidence(
            acoustid_candidates=[cand],
            audd=_audd(artist="Drake", title="God's Plan", isrc=""),
        )
        v = resolve(ev)
        assert v["state"] == "conflict"
        _assert_evidence_trace(v)

    def test_evidence_trace_on_review(self):
        # Ambiguity → review
        c1 = _cand(recording_id="rid-A", score=0.93)
        c2 = _cand(recording_id="rid-B", score=0.91)
        ev = _evidence(
            acoustid_candidates=[c1, c2],
            audd=_audd(),
        )
        v = resolve(ev)
        assert v["state"] == "review"
        _assert_evidence_trace(v)

    def test_comparisons_populated_on_conflict_check(self):
        """conflict_check comparisons populated when AudD + candidate both present."""
        cand = _cand(recording_id="rid-A", artist="Beck", title="Loser")
        ev = _evidence(
            acoustid_candidates=[cand],
            audd=_audd(artist="Drake", title="God's Plan", isrc=""),
        )
        v = resolve(ev)
        assert "conflict_check" in v["evidence"]["comparisons"]
        cc = v["evidence"]["comparisons"]["conflict_check"]
        assert "artist_sim" in cc
        assert "title_sim" in cc

    def test_ambiguity_top2_in_comparisons(self):
        """ambiguity_top2 comparisons populated when 2+ candidates."""
        c1 = _cand(recording_id="rid-A", score=0.93)
        c2 = _cand(recording_id="rid-B", score=0.91)
        ev = _evidence(acoustid_candidates=[c1, c2], audd=_audd())
        v = resolve(ev)
        assert "ambiguity_top2" in v["evidence"]["comparisons"]

    def test_t1_dur_comparison_present_on_t1_path(self):
        isrc = "USRC11400001"
        cand = _cand(recording_id="rid-A", isrcs=[isrc], length_ms=200_000)
        ev = _evidence(
            duration_ms=200_000,
            acoustid_candidates=[cand],
            audd=_audd(isrc=isrc),
            isrc_recording_map={isrc: ["rid-A"]},
        )
        v = resolve(ev)
        assert "t1_dur" in v["evidence"]["comparisons"]

    def test_t2_dur_comparison_present_on_t2_path(self):
        cand = _cand(recording_id="rid-A", isrcs=[], length_ms=200_000,
                     artist="Beck", title="Loser")
        ev = _evidence(
            duration_ms=200_000,
            acoustid_candidates=[cand],
            audd=_audd(artist="Beck", title="Loser", isrc=""),
            isrc_recording_map={},
        )
        v = resolve(ev)
        assert v["tier"] == "T2"
        assert "t2_dur" in v["evidence"]["comparisons"]


# ---------------------------------------------------------------------------
# Misc edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_empty_candidates_and_no_audd_unknown(self):
        ev = _evidence(
            acoustid_candidates=[],
            audd=None,
            audd_attempted=True,
            audd_budget_exhausted=False,
            mirror_available=True,
            isrc_recording_map={},
        )
        v = resolve(ev)
        assert v["state"] == "unknown"

    def test_all_fields_none_no_crash(self):
        """Minimal evidence dict with missing keys should not crash."""
        v = resolve({"file_exists": True, "fingerprint_ok": True})
        assert v["state"] in {"unknown", "deferred", "review", "confirmed", "conflict", "error"}

    def test_single_candidate_no_ambiguity(self):
        """Only one candidate → no ambiguity veto."""
        cand = _cand(recording_id="rid-A", score=0.90, isrcs=[], length_ms=200_000,
                     artist="Beck", title="Loser")
        ev = _evidence(
            duration_ms=200_000,
            existing_artist="Beck",
            existing_title="Loser",
            acoustid_candidates=[cand],
            audd=None,
            audd_attempted=True,
            isrc_recording_map={},
        )
        v = resolve(ev)
        assert "ambiguous" not in v["evidence"]["vetoes_fired"]

    def test_t1_isrc_in_map_but_recording_id_mismatch_no_confirm(self):
        """ISRC maps to one recording but it's a different one → T1 mismatch, not confirmed."""
        isrc = "USRC11400001"
        cand = _cand(recording_id="rid-A", isrcs=[isrc], length_ms=200_000)
        ev = _evidence(
            duration_ms=200_000,
            acoustid_candidates=[cand],
            audd=_audd(isrc=isrc),
            isrc_recording_map={isrc: ["rid-DIFFERENT"]},  # unique but mismatched
        )
        v = resolve(ev)
        assert not (v["state"] == "confirmed" and v["tier"] == "T1")
        _assert_evidence_trace(v)

    def test_below_floor_only_candidates_no_t1_t2_t4(self):
        """All candidates below_floor → no participant for T2/T4; T1 also skipped."""
        cand = _cand(recording_id="rid-A", score=0.40, below_floor=True,
                     isrcs=["USRC11400001"])
        ev = _evidence(
            duration_ms=200_000,
            existing_artist="Beck",
            existing_title="Loser",
            acoustid_candidates=[cand],
            audd=_audd(isrc="USRC11400001"),
            isrc_recording_map={"USRC11400001": ["rid-A"]},
        )
        v = resolve(ev)
        # No participant → T1 requires non-below_floor; T2/T4 need best candidate
        assert v["state"] != "confirmed"
        _assert_evidence_trace(v)
