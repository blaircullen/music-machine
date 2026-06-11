"""
Identity Resolver — pure evidence-to-verdict engine.

No I/O, no DB imports.  Caller gathers all evidence and passes it as a
plain dict; resolve() returns a verdict dict suitable for insertion into
track_identity.

STATE TRANSITION MAP
====================

    [any state]  ──(re-resolve at higher resolver_version)──►  [new state]
    [deferred]   ──(mirror recovers / budget replenished)──────►  [re-resolve]
    [error]      ──(file restored / fpcalc fixed)───────────────►  [re-resolve]
    [review]     ──(human approves, Phase 4 UI)────────────────►  confirmed
    [conflict]   ──(human resolves, Phase 4 UI)────────────────►  confirmed | review
    [confirmed]  ──(higher-tier evidence + bumped version)──────►  confirmed
    [confirmed]  ──(human override, Phase 4 UI)────────────────►  confirmed (superseded)

Automatic (no human required):
    mechanical failure  ──►  error
    sensing incomplete  ──►  deferred
    tier passes cleanly ──►  confirmed
    everything else     ──►  review | unknown | conflict

Automatic promotion from deferred/error occurs at next sweep run when
the blocking condition clears; the sweep (U5) handles re-queuing.

T3 (album-lock tier) is intentionally reserved for Phase 2.  The gap
in numbering T1/T2/-/T4 is documented here and in KTD 2 of the plan;
it is not a typo.

Order of evaluation (test-asserted):
  1. Mechanical checks   → error
  2. Duration availability note
  3. Vetoes (on ALL ≥0.3 candidates, strictly before tier assignment):
       short → review
       ambiguity → review
       conflict → conflict
  4. Tiers T1 → T2 → T4 (participants = score ≥ 0.5 / not below_floor)
  5. Divergence escalation (after a tier nominates a winner)
  6. Deferred / unknown fallthrough
"""

from __future__ import annotations

import logging

from normalize import similar

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Version
# ---------------------------------------------------------------------------

RESOLVER_VERSION = "1"

# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

CONFIRM_MS      = 3000   # |file_dur - ref_dur| ≤ this → confirm
DEADBAND_MS     = 4000   # CONFIRM_MS < |Δ| ≤ this   → dead-band (review)
# |Δ| > DEADBAND_MS → hard veto (candidate disqualified)

AMBIGUITY_GAP   = 0.05   # top-2 score gap ≤ this with different recording ids
SHORT_TRACK_MS  = 45_000 # duration below this → review(short)

ARTIST_SIM      = 0.85   # similarity threshold for artist fields
TITLE_SIM       = 0.80   # similarity threshold for title fields
T4_MIN_SCORE    = 0.70   # minimum AcoustID score for T4
DIVERGENCE_SIM  = 0.50   # winner vs existing_artist below this → divergent


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _dur_ok(file_ms, ref_ms):
    """
    Compare file decoded duration against a reference duration.

    Returns one of:
        "confirm"     — |Δ| ≤ CONFIRM_MS
        "deadband"    — CONFIRM_MS < |Δ| ≤ DEADBAND_MS
        "veto"        — |Δ| > DEADBAND_MS
        "unavailable" — either value is None / falsy
    """
    if not file_ms or not ref_ms:
        return "unavailable"
    delta = abs(file_ms - ref_ms)
    if delta <= CONFIRM_MS:
        return "confirm"
    if delta <= DEADBAND_MS:
        return "deadband"
    return "veto"


def _best_candidate(candidates):
    """Return the single highest-score non-below_floor candidate, or None."""
    participants = [c for c in candidates if not c.get("below_floor", True)]
    if not participants:
        return None
    return max(participants, key=lambda c: c.get("score", 0.0))


def _top2_participant_ids(candidates):
    """
    Return the top-2 recording_ids (by score) among ALL retained candidates
    (≥0.3), regardless of participation floor.  Used for ambiguity veto.
    """
    sorted_c = sorted(candidates, key=lambda c: c.get("score", 0.0), reverse=True)
    result = []
    for c in sorted_c:
        result.append((c.get("recording_id"), c.get("score", 0.0)))
        if len(result) == 2:
            break
    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def resolve(evidence: dict) -> dict:
    """
    Pure evidence → verdict function.

    Parameters
    ----------
    evidence : dict with keys:
        file_exists          : bool
        duration_ms          : int | None  — decoded audio duration
        existing_artist      : str | None  — tag as-found on disk
        existing_title       : str | None
        acoustid_candidates  : list of dicts, each:
            {recording_id, score, below_floor, artist, title,
             length_ms, isrcs: list, release_id, album, date, track_no}
        audd                 : dict | None:
            {artist, title, album, isrc, audd_score, duration_ms}
        audd_attempted       : bool
        audd_budget_exhausted: bool
        mirror_available     : bool
        isrc_recording_map   : dict  {isrc: [recording_id, ...]}
        fingerprint_ok       : bool  — fpcalc succeeded

    Returns
    -------
    dict with keys:
        state, tier, mb_recording_id, mb_release_id, isrc,
        artist, title, album, date, track_no,
        divergent, evidence (full trace dict), resolver_version
    """

    candidates  = evidence.get("acoustid_candidates") or []
    audd        = evidence.get("audd")
    file_ms     = evidence.get("duration_ms")
    exist_art   = evidence.get("existing_artist") or ""
    exist_ttl   = evidence.get("existing_title") or ""
    isrc_map    = evidence.get("isrc_recording_map") or {}

    # Trace containers populated throughout evaluation
    vetoes_fired = []
    tier_trace   = []
    comparisons  = {}

    # ------------------------------------------------------------------
    # Helper: build final verdict
    # ------------------------------------------------------------------
    def _verdict(state, tier=None, winner=None, divergent=False, extra_reason=None):
        w = winner or {}
        ev = {
            "candidates": candidates,
            "audd_present": audd is not None,
            "audd_attempted": evidence.get("audd_attempted", False),
            "audd_budget_exhausted": evidence.get("audd_budget_exhausted", False),
            "mirror_available": evidence.get("mirror_available", True),
            "file_ms": file_ms,
            "vetoes_fired": vetoes_fired,
            "tier_trace": tier_trace,
            "comparisons": comparisons,
        }
        if extra_reason:
            ev["reason"] = extra_reason
        return {
            "state":            state,
            "tier":             tier,
            "mb_recording_id":  w.get("recording_id"),
            "mb_release_id":    w.get("release_id"),
            "isrc":             w.get("_winning_isrc"),
            "artist":           w.get("artist"),
            "title":            w.get("title"),
            "album":            w.get("album"),
            "date":             w.get("date"),
            "track_no":         w.get("track_no"),
            "divergent":        divergent,
            "evidence":         ev,
            "resolver_version": RESOLVER_VERSION,
        }

    # ==================================================================
    # 1. MECHANICAL CHECKS  →  error
    # ==================================================================
    if not evidence.get("file_exists", True):
        vetoes_fired.append("file_missing")
        return _verdict("error", extra_reason="file_missing")

    if not evidence.get("fingerprint_ok", True):
        vetoes_fired.append("fpcalc_failed")
        return _verdict("error", extra_reason="fpcalc_failed")

    # ==================================================================
    # 2. DURATION AVAILABILITY NOTE
    # ==================================================================
    duration_available = file_ms is not None
    if not duration_available:
        # Duration-dependent tiers cannot confirm; note it but continue
        # so ambiguity / conflict vetoes can still fire.
        comparisons["duration_unavailable"] = True

    # ==================================================================
    # 3. VETOES  (evaluated on ALL retained candidates ≥ 0.3)
    # ==================================================================

    # --- 3a. SHORT track ---
    if duration_available and file_ms < SHORT_TRACK_MS:
        vetoes_fired.append("short")
        # Still evaluate remaining vetoes for completeness, but remember
        # we already have a review reason.

    # --- 3b. AMBIGUITY veto ---
    # Top-2 by score across all retained candidates (≥0.3); veto fires when
    # recording_ids differ AND score gap ≤ AMBIGUITY_GAP.
    # Same recording_id appearing twice strengthens rather than vetoes.
    ambiguous = False
    if len(candidates) >= 2:
        top2 = _top2_participant_ids(candidates)
        if len(top2) == 2:
            rid1, sc1 = top2[0]
            rid2, sc2 = top2[1]
            gap = sc1 - sc2
            comparisons["ambiguity_top2"] = {
                "rid1": rid1, "score1": sc1,
                "rid2": rid2, "score2": sc2,
                "gap": gap,
                "different_ids": rid1 != rid2,
            }
            if rid1 != rid2 and gap <= AMBIGUITY_GAP:
                ambiguous = True
                vetoes_fired.append("ambiguous")

    # --- 3c. CONFLICT veto ---
    # AudD present + best acoustid candidate present + BOTH artist AND title
    # hard-contradict (both < 0.3 similarity).  Conservative — both fields
    # must contradict; a title match with artist mismatch is not a conflict.
    conflicted = False
    best = _best_candidate(candidates)
    if audd and best:
        art_sim = similar(audd.get("artist", ""), best.get("artist", ""))
        ttl_sim = similar(audd.get("title",  ""), best.get("title",  ""))
        comparisons["conflict_check"] = {
            "audd_artist": audd.get("artist"),
            "acoustid_artist": best.get("artist"),
            "artist_sim": art_sim,
            "audd_title": audd.get("title"),
            "acoustid_title": best.get("title"),
            "title_sim": ttl_sim,
        }
        if art_sim < 0.3 and ttl_sim < 0.3:
            conflicted = True
            vetoes_fired.append("conflict")

    # Conflict short-circuits everything — return immediately
    if conflicted:
        return _verdict("conflict", extra_reason="conflict")

    # Short or ambiguous vetoes collected — remember for later; we still try
    # tiers (a veto means the result is review, not that tiers are skipped
    # entirely — the veto wins if a tier would have confirmed).
    has_review_veto = bool(vetoes_fired)  # at this point only short/ambiguous

    # ==================================================================
    # 4. TIERS  (participants = not below_floor, i.e. score ≥ 0.5)
    # ==================================================================

    # ---- T1: ISRC unique-recording proof ----
    # Requires:
    #   audd.isrc non-empty
    #   audd.isrc ∈ candidate.isrcs  (for some participant candidate)
    #   isrc_recording_map[isrc] has exactly ONE recording_id
    #   that recording_id == candidate.recording_id
    #   dur_ok(file_ms, candidate.length_ms) ∈ {"confirm","deadband"}
    t1_info: dict = {"attempted": False}
    winner = None

    if audd and audd.get("isrc"):
        t1_info["attempted"] = True
        audd_isrc = audd["isrc"]
        t1_info["audd_isrc"] = audd_isrc

        # Find a participant candidate that lists this ISRC
        t1_candidate = None
        for c in candidates:
            if c.get("below_floor"):
                continue
            if audd_isrc in (c.get("isrcs") or []):
                t1_candidate = c
                break

        if t1_candidate is None:
            t1_info["result"] = "isrc_not_in_any_candidate"
        else:
            t1_info["candidate_recording_id"] = t1_candidate.get("recording_id")
            # Uniqueness check
            mapped_ids = isrc_map.get(audd_isrc, [])
            t1_info["isrc_mapped_ids"] = mapped_ids
            if len(mapped_ids) != 1:
                t1_info["result"] = "isrc_multi_mapping"
                # Only flag multi-mapping if T1 would otherwise have fired
                if duration_available:
                    dur_result = _dur_ok(file_ms, t1_candidate.get("length_ms"))
                    if dur_result in ("confirm", "deadband"):
                        vetoes_fired.append("isrc_multi_mapping")
                        has_review_veto = True
            elif mapped_ids[0] != t1_candidate.get("recording_id"):
                t1_info["result"] = "isrc_recording_id_mismatch"
            else:
                # Duration gate
                if not duration_available:
                    t1_info["result"] = "duration_unavailable"
                else:
                    dur_result = _dur_ok(file_ms, t1_candidate.get("length_ms"))
                    t1_info["dur_result"] = dur_result
                    comparisons["t1_dur"] = {
                        "file_ms": file_ms,
                        "ref_ms": t1_candidate.get("length_ms"),
                        "delta": abs(file_ms - (t1_candidate.get("length_ms") or 0)),
                        "result": dur_result,
                    }
                    if dur_result == "confirm":
                        t1_info["result"] = "pass"
                        winner = dict(t1_candidate)
                        winner["_winning_isrc"] = audd_isrc
                    elif dur_result == "deadband":
                        t1_info["result"] = "deadband"
                        vetoes_fired.append("dead_band")
                        has_review_veto = True
                    else:  # veto
                        t1_info["result"] = "dur_veto"

    tier_trace.append({"tier": "T1", **t1_info})

    if winner:
        # T1 confirmed — check divergence
        divergent = False
        if exist_art:
            div_sim = similar(winner.get("artist", ""), exist_art)
            comparisons["t1_divergence"] = {
                "winner_artist": winner.get("artist"),
                "existing_artist": exist_art,
                "sim": div_sim,
            }
            if div_sim < DIVERGENCE_SIM:
                divergent = True
        # T1 confirms even when divergent (per plan KTD: T1 still confirms)
        if has_review_veto:
            # A veto (short/ambiguous/dead_band) fired — review wins
            return _verdict("review", tier="T1", winner=winner, divergent=divergent)
        return _verdict("confirmed", tier="T1", winner=winner, divergent=divergent)

    # ---- T2: cross-recognizer corroboration ----
    # Requires:
    #   audd present
    #   similar(candidate.artist, audd.artist) ≥ ARTIST_SIM
    #   similar(candidate.title,  audd.title ) ≥ TITLE_SIM
    #   dur_ok(file_ms, candidate.length_ms) == "confirm"
    t2_info: dict = {"attempted": False}

    if audd and best:
        t2_info["attempted"] = True
        art_sim_t2 = similar(best.get("artist", ""), audd.get("artist", ""))
        ttl_sim_t2 = similar(best.get("title",  ""), audd.get("title",  ""))
        comparisons["t2_sim"] = {
            "acoustid_artist": best.get("artist"),
            "audd_artist": audd.get("artist"),
            "artist_sim": art_sim_t2,
            "acoustid_title": best.get("title"),
            "audd_title": audd.get("title"),
            "title_sim": ttl_sim_t2,
        }
        if art_sim_t2 < ARTIST_SIM:
            t2_info["result"] = f"artist_sim_fail ({art_sim_t2:.3f} < {ARTIST_SIM})"
        elif ttl_sim_t2 < TITLE_SIM:
            t2_info["result"] = f"title_sim_fail ({ttl_sim_t2:.3f} < {TITLE_SIM})"
        else:
            # Duration corroboration required
            if not duration_available:
                t2_info["result"] = "duration_unavailable"
            else:
                dur_result_t2 = _dur_ok(file_ms, best.get("length_ms"))
                comparisons["t2_dur"] = {
                    "file_ms": file_ms,
                    "ref_ms": best.get("length_ms"),
                    "delta": abs(file_ms - (best.get("length_ms") or 0)),
                    "result": dur_result_t2,
                }
                t2_info["dur_result"] = dur_result_t2
                if dur_result_t2 == "confirm":
                    t2_info["result"] = "pass"
                    winner = dict(best)
                    winner["_winning_isrc"] = None
                elif dur_result_t2 == "deadband":
                    t2_info["result"] = "deadband"
                    vetoes_fired.append("dead_band")
                    has_review_veto = True
                else:  # veto
                    t2_info["result"] = "dur_veto"
    elif not audd:
        t2_info["attempted"] = False
        t2_info["result"] = "audd_missing"
    else:
        t2_info["attempted"] = True
        t2_info["result"] = "no_participant_candidate"

    tier_trace.append({"tier": "T2", **t2_info})

    if winner:
        # T2 confirmed — check divergence
        divergent = False
        if exist_art:
            div_sim = similar(winner.get("artist", ""), exist_art)
            comparisons["t2_divergence"] = {
                "winner_artist": winner.get("artist"),
                "existing_artist": exist_art,
                "sim": div_sim,
            }
            if div_sim < DIVERGENCE_SIM:
                divergent = True
                # T2-only confirm with divergence → downgrade to review
                tier_trace.append({"tier": "T2", "divergence_downgrade": True})
                return _verdict("review", tier="T2", winner=winner, divergent=True,
                                extra_reason="divergent_artist")
        if has_review_veto:
            return _verdict("review", tier="T2", winner=winner, divergent=divergent)
        return _verdict("confirmed", tier="T2", winner=winner, divergent=divergent)

    # ---- T4: tag corroboration ----
    # Requires:
    #   similar(candidate.artist, existing_artist) ≥ ARTIST_SIM
    #   similar(candidate.title,  existing_title ) ≥ TITLE_SIM
    #   candidate.score ≥ T4_MIN_SCORE
    #   dur_ok(file_ms, candidate.length_ms) == "confirm"
    # Note: T4 by construction cannot be divergent (it matches existing tags).
    t4_info: dict = {"attempted": False}

    if best and exist_art and exist_ttl:
        t4_info["attempted"] = True
        art_sim_t4 = similar(best.get("artist", ""), exist_art)
        ttl_sim_t4 = similar(best.get("title",  ""), exist_ttl)
        score_t4   = best.get("score", 0.0)
        comparisons["t4_sim"] = {
            "acoustid_artist": best.get("artist"),
            "existing_artist": exist_art,
            "artist_sim": art_sim_t4,
            "acoustid_title": best.get("title"),
            "existing_title": exist_ttl,
            "title_sim": ttl_sim_t4,
            "score": score_t4,
        }
        if art_sim_t4 < ARTIST_SIM:
            t4_info["result"] = f"artist_sim_fail ({art_sim_t4:.3f} < {ARTIST_SIM})"
        elif ttl_sim_t4 < TITLE_SIM:
            t4_info["result"] = f"title_sim_fail ({ttl_sim_t4:.3f} < {TITLE_SIM})"
        elif score_t4 < T4_MIN_SCORE:
            t4_info["result"] = f"score_fail ({score_t4:.3f} < {T4_MIN_SCORE})"
        else:
            if not duration_available:
                t4_info["result"] = "duration_unavailable"
            else:
                dur_result_t4 = _dur_ok(file_ms, best.get("length_ms"))
                comparisons["t4_dur"] = {
                    "file_ms": file_ms,
                    "ref_ms": best.get("length_ms"),
                    "delta": abs(file_ms - (best.get("length_ms") or 0)),
                    "result": dur_result_t4,
                }
                t4_info["dur_result"] = dur_result_t4
                if dur_result_t4 == "confirm":
                    t4_info["result"] = "pass"
                    winner = dict(best)
                    winner["_winning_isrc"] = None
                elif dur_result_t4 == "deadband":
                    t4_info["result"] = "deadband"
                    vetoes_fired.append("dead_band")
                    has_review_veto = True
                else:
                    t4_info["result"] = "dur_veto"
    elif not (exist_art and exist_ttl):
        t4_info["result"] = "no_existing_tags"
    else:
        t4_info["result"] = "no_participant_candidate"

    tier_trace.append({"tier": "T4", **t4_info})

    if winner:
        if has_review_veto:
            return _verdict("review", tier="T4", winner=winner)
        return _verdict("confirmed", tier="T4", winner=winner)

    # ==================================================================
    # 5. FALLTHROUGH
    # ==================================================================

    # If any veto fired (short / ambiguous) we land here without a winner
    if has_review_veto:
        return _verdict("review")

    # Mirror unavailable → deferred (can't complete sensing)
    if not evidence.get("mirror_available", True):
        return _verdict("deferred", extra_reason="mirror_unavailable")

    # AudD not attempted yet AND budget not exhausted AND mirror available →
    # deferred (sensing incomplete, can try AudD)
    if not evidence.get("audd_attempted", False) and not evidence.get("audd_budget_exhausted", False):
        return _verdict("deferred", extra_reason="audd_not_attempted")

    # AudD budget exhausted → deferred
    if evidence.get("audd_budget_exhausted", False):
        return _verdict("deferred", extra_reason="audd_budget_exhausted")

    # All sensing done, no tier fired → unknown
    return _verdict("unknown")
