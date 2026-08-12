"""
Golden + differential tests for all four normalizers.

PURPOSE
-------
1. Golden: for each corpus entry, assert that each LEGACY normalizer produces
   exactly the hardcoded expected output.  If anyone touches dedup.py,
   upgrade_service.py, or lidarr_client.py these tests will catch it.

2. Differential: assert the full frozen divergence table — every combination
   of corpus entry × normalizer × canonical output.  Any drift in ANY
   normalizer is immediately visible.

LEGACY NORMALIZERS (byte-for-byte unchanged this phase)
    dedup.normalize_text          (backend/dedup.py)
    upgrade_service._normalize_text  (backend/upgrade_service.py)
    lidarr_client._normalize      (backend/lidarr_client.py)

CANONICAL
    normalize.normalize           (backend/normalize.py)

HOW THE EXPECTED VALUES WERE GENERATED
---------------------------------------
All expected values were derived by manually tracing each function's code
against the corpus entry.  The key transformation differences are:

    dedup
        NFKD + regex removes combining marks (é → e+combining → "e")
        Strips leading "the ", "a ", "an "
        Keeps hyphens; removes other punctuation by deletion (not space)
        Does NOT strip feat/remaster suffixes

    upgrade_service._normalize_text  ("upgrade" column)
        Identical to dedup EXCEPT: does NOT strip leading articles
        Both use NFKD + same regex, so diacritics handled the same way

    lidarr_client._normalize  ("lidarr" column)
        .lower() only — no NFKD, so diacritics survive (é stays é)
        Strips feat/featuring/ft and everything after (bare word boundary)
        Strips ALL string.punctuation including hyphens (deletion, not space)
        Does NOT strip leading articles or remaster suffixes

    canonical normalize.normalize
        casefold + NFD + Mn-category drop (diacritics → bare ASCII)
        Strips feat in trailing parens AND as bare separator
        Strips remaster/deluxe/edition/anniversary/single-version/radio-edit/
            expanded/bonus-track suffixes in parens, brackets, or after " - "
        Strips ONLY leading "The " (not "a ", "an ")
        Replaces non-alphanumeric non-space with SPACE (not deletion) then
            collapses whitespace — so "AC/DC" → "ac dc" (two tokens), while
            legacy normalizers delete "/" → "acdc" (one token)

NOTABLE DIVERGENCES (summary; detail in the DIVERGENCE_TABLE below)
--------------------
D1  Remaster suffixes   canonical strips, all three legacy keep
D2  feat suffix         canonical + lidarr strip; dedup + upgrade keep
D3  AC/DC → ac dc vs acdc   canonical maps "/" to space; legacy delete it
D4  "The " prefix       canonical + dedup strip; upgrade + lidarr keep
D5  Diacritics (é, ö)   canonical + dedup + upgrade strip; lidarr keeps
D6  Leading "a "        dedup strips; canonical + upgrade + lidarr keep
D7  Track numbers       all keep (canonical: "01 - song", lidarr: "01 song")
D8  Unicode dash        canonical maps to space; legacy may vary
"""

from __future__ import annotations

import sys
import types

# Stub acoustid so dedup/scanner imports don't blow up without the C extension.
_acoustid_stub = types.ModuleType("acoustid")
_acoustid_stub.fingerprint_file = lambda path: (0, None)
sys.modules.setdefault("acoustid", _acoustid_stub)

from dedup import normalize_text as dedup_norm
from upgrade_service import _normalize_text as upgrade_norm
from lidarr_client import _normalize as lidarr_norm
from normalize import normalize as canonical_norm


# ---------------------------------------------------------------------------
# Corpus — 40+ real-world artist and track title strings
# ---------------------------------------------------------------------------

# Each entry: (label, raw_string)
# Labels are used as pytest IDs and referenced in DIVERGENCE_TABLE.

CORPUS: list[tuple[str, str]] = [
    # Artists — simple
    ("artist_beatles",          "The Beatles"),
    ("artist_beatles_bare",     "Beatles"),
    ("artist_rolling_stones",   "The Rolling Stones"),
    ("artist_beyonce_diacritic","Beyoncé"),
    ("artist_bjork",            "Björk"),
    ("artist_motorhead",        "Motörhead"),
    ("artist_sigur_ros",        "Sigur Rós"),
    ("artist_acdc_slash",       "AC/DC"),
    ("artist_simon_garfunkel",  "Simon & Garfunkel"),
    ("artist_led_zeppelin",     "Led Zeppelin"),
    # Artists — leading articles
    ("artist_the_xx",           "The XX"),
    ("artist_a_tribe",          "A Tribe Called Quest"),
    ("artist_an_horse",         "An Horse"),
    # Track titles — feat variants
    ("feat_bare_dot",           "A feat. B"),
    ("feat_bare_featuring",     "Track featuring Rihanna"),
    ("feat_bare_ft",            "Song ft. Drake"),
    ("feat_paren_dot",          "Track (feat. Drake)"),
    ("feat_paren_featuring",    "Track (featuring Beyoncé)"),
    ("feat_paren_with",         "Track (with Drake)"),
    ("feat_multi",              "One Dance feat. Wizkid & Kyla"),
    # Track titles — remaster/edition suffixes
    ("remaster_bare_paren",     "Song (Remastered)"),
    ("remaster_year_after",     "Song (Remastered 2019)"),
    ("remaster_year_before",    "Song (2019 Remaster)"),
    ("remaster_after_dash",     "Song - 2011 Remaster"),
    ("remaster_after_dash2",    "Song - Remastered"),
    ("deluxe_edition",          "Album (Deluxe Edition)"),
    ("deluxe_bare",             "Album (Deluxe)"),
    ("anniversary_edition",     "Song (Anniversary Edition)"),
    ("single_version",          "Song (Single Version)"),
    ("radio_edit",              "Song (Radio Edit)"),
    ("expanded_edition",        "Song (Expanded Edition)"),
    ("bonus_track",             "Song (Bonus Track)"),
    ("bracket_remaster",        "Song [Remastered 2019]"),
    # Track titles — Live / Acoustic / Demo (must NOT be stripped by canonical)
    ("live_paren",              "Cumbersome (Live)"),
    ("acoustic_paren",          "Blackbird (Acoustic)"),
    ("demo_paren",              "Yesterday (Demo)"),
    ("live_bare",               "Stairway to Heaven - Live"),
    # Track titles — track number prefix
    ("track_number_prefix",     "01 - Song"),
    ("track_number_prefix2",    "05 - Bohemian Rhapsody"),
    # Track titles — unicode / punctuation
    ("unicode_dash_title",      "Don’t Stop Me Now"),    # U+2019 RIGHT SINGLE QUOTATION MARK
    ("em_dash_title",           "Song — Live Version"),  # U+2014 EM DASH
    ("full_title_normal",       "The Dark Side of the Moon"),
    ("full_title_a_prefix",     "A Hard Day's Night"),
    ("title_punctuation_heavy", "Don't Stop Me Now!"),
    ("title_colon",             "Hello: The Collection"),
    ("title_period",            "Mr. Brightside"),
    ("title_amp_title",         "Young & Beautiful"),
    ("combined_feat_remaster",  "Song (feat. Drake) (Remastered 2019)"),
]


# ---------------------------------------------------------------------------
# Frozen expected outputs (generated by tracing each function)
# ---------------------------------------------------------------------------

# Format: EXPECTED[label] = {"dedup": str, "upgrade": str, "lidarr": str, "canonical": str}
#
# Key:
#   dedup    = dedup.normalize_text
#   upgrade  = upgrade_service._normalize_text
#   lidarr   = lidarr_client._normalize
#   canonical = normalize.normalize

DIVERGENCE_TABLE: dict[str, dict[str, str]] = {
    # --- Artists — simple ---
    "artist_beatles": {
        "dedup":     "beatles",           # strips "the "
        "upgrade":   "the beatles",       # no article strip
        "lidarr":    "the beatles",       # no article strip, no diacritics issue
        "canonical": "beatles",           # strips "the "
    },
    "artist_beatles_bare": {
        "dedup":     "beatles",
        "upgrade":   "beatles",
        "lidarr":    "beatles",
        "canonical": "beatles",
    },
    "artist_rolling_stones": {
        "dedup":     "rolling stones",
        "upgrade":   "the rolling stones",
        "lidarr":    "the rolling stones",
        "canonical": "rolling stones",
    },
    # D5: lidarr keeps é, others strip it
    "artist_beyonce_diacritic": {
        "dedup":     "beyonce",           # NFKD + regex strips combining mark
        "upgrade":   "beyonce",           # same
        "lidarr":    "beyoncé",      # .lower() only — é survives
        "canonical": "beyonce",           # casefold + NFD + Mn drop
    },
    "artist_bjork": {
        "dedup":     "bjork",
        "upgrade":   "bjork",
        "lidarr":    "björk",       # ö survives in lidarr
        "canonical": "bjork",
    },
    "artist_motorhead": {
        "dedup":     "motorhead",    # NFKD: ö → o + U+0308 (Mn), regex drops the mark
        "upgrade":   "motorhead",
        "lidarr":    "motörhead",   # no NFKD
        "canonical": "motorhead",
    },
    "artist_sigur_ros": {
        "dedup":     "sigur ros",
        "upgrade":   "sigur ros",
        "lidarr":    "sigur rós",
        "canonical": "sigur ros",
    },
    # D3: "/" → deleted in legacy (→ one token "acdc"), space in canonical (→ two tokens "ac dc")
    "artist_acdc_slash": {
        "dedup":     "acdc",             # regex deletes "/"
        "upgrade":   "acdc",
        "lidarr":    "acdc",             # string.punctuation includes "/"
        "canonical": "ac dc",            # "/" → space
    },
    "artist_simon_garfunkel": {
        "dedup":     "simon garfunkel",  # regex deletes "&"
        "upgrade":   "simon garfunkel",
        "lidarr":    "simon  garfunkel", # & deleted, two spaces → collapse → "simon garfunkel"
        # Actually lidarr does: translate deletes &, then re.sub collapses
        "canonical": "simon garfunkel",  # & → space → collapse
    },
    "artist_led_zeppelin": {
        "dedup":     "led zeppelin",
        "upgrade":   "led zeppelin",
        "lidarr":    "led zeppelin",
        "canonical": "led zeppelin",
    },
    # --- Leading articles ---
    "artist_the_xx": {
        "dedup":     "xx",
        "upgrade":   "the xx",
        "lidarr":    "the xx",
        "canonical": "xx",
    },
    # D6: dedup strips "a "; canonical does not
    "artist_a_tribe": {
        "dedup":     "tribe called quest",   # strips "a "
        "upgrade":   "a tribe called quest", # no strip
        "lidarr":    "a tribe called quest",
        "canonical": "a tribe called quest", # only "the " is stripped
    },
    "artist_an_horse": {
        "dedup":     "horse",           # strips "an "
        "upgrade":   "an horse",
        "lidarr":    "an horse",
        "canonical": "an horse",        # "an " NOT stripped canonically
    },
    # --- feat variants ---
    # D2 + D6: feat kept in dedup/upgrade; dedup also strips "a " prefix
    "feat_bare_dot": {
        "dedup":     "feat b",          # strips "a " → "feat. b" → removes "." → "feat b"
        "upgrade":   "a feat b",        # keeps "a", removes "." → "a feat b"
        "lidarr":    "a",               # strips "feat. b" via regex
        "canonical": "a",               # feat separator strip
    },
    "feat_bare_featuring": {
        "dedup":     "track featuring rihanna",   # no feat strip in dedup
        "upgrade":   "track featuring rihanna",
        "lidarr":    "track",                     # strips "featuring rihanna", split/join collapses
        "canonical": "track",
    },
    "feat_bare_ft": {
        "dedup":     "song ft drake",   # removes "."
        "upgrade":   "song ft drake",
        "lidarr":    "song",            # strips "ft. drake"
        "canonical": "song",
    },
    "feat_paren_dot": {
        # dedup: "(", ".", ")" removed; "feat" kept as word
        "dedup":     "track feat drake",
        "upgrade":   "track feat drake",
        # lidarr: "track (" then feat regex matches "feat. drake)" → wait, what does lidarr do?
        # lidarr step: re.sub(r"\b(feat|featuring|ft)\.?\b.*$", "", value)
        # In "track (feat. drake)", after .lower(): "track (feat. drake)"
        # The pattern searches for \b(feat)\.?\b — "feat" is preceded by "(" which is non-word
        # so \b matches before "f". Pattern matches "feat. drake)". Replaces with "".
        # → "track (" → translate removes "(" → "track " → collapse → "track"
        "lidarr":    "track",
        "canonical": "track",
    },
    "feat_paren_featuring": {
        "dedup":     "track featuring beyonce",   # parens removed, beyonce via NFKD
        "upgrade":   "track featuring beyonce",
        "lidarr":    "track",
        "canonical": "track",
    },
    "feat_paren_with": {
        "dedup":     "track with drake",   # "(", ")" removed; "with" kept
        "upgrade":   "track with drake",
        "lidarr":    "track with drake",   # "with" not in lidarr's feat regex
        "canonical": "track",              # "(with Drake)" stripped by _FEAT_TRAILING_PAREN
    },
    "feat_multi": {
        "dedup":     "one dance feat wizkid kyla",  # removes ".", "&"
        "upgrade":   "one dance feat wizkid kyla",
        "lidarr":    "one dance",                   # strips "feat. Wizkid & Kyla"
        "canonical": "one dance",
    },
    # --- Remaster/edition suffixes (D1: only canonical strips) ---
    "remaster_bare_paren": {
        "dedup":     "song remastered",
        "upgrade":   "song remastered",
        "lidarr":    "song remastered",
        "canonical": "song",
    },
    "remaster_year_after": {
        "dedup":     "song remastered 2019",
        "upgrade":   "song remastered 2019",
        "lidarr":    "song remastered 2019",
        "canonical": "song",
    },
    "remaster_year_before": {
        "dedup":     "song 2019 remaster",
        "upgrade":   "song 2019 remaster",
        "lidarr":    "song 2019 remaster",
        "canonical": "song",
    },
    "remaster_after_dash": {
        "dedup":     "song - 2011 remaster",  # keeps hyphen
        "upgrade":   "song - 2011 remaster",
        "lidarr":    "song  2011 remaster",   # "-" deleted → double space → collapse → single
        "canonical": "song",
    },
    "remaster_after_dash2": {
        "dedup":     "song - remastered",
        "upgrade":   "song - remastered",
        "lidarr":    "song  remastered",
        "canonical": "song",
    },
    "deluxe_edition": {
        "dedup":     "album deluxe edition",
        "upgrade":   "album deluxe edition",
        "lidarr":    "album deluxe edition",
        "canonical": "album",
    },
    "deluxe_bare": {
        "dedup":     "album deluxe",
        "upgrade":   "album deluxe",
        "lidarr":    "album deluxe",
        "canonical": "album",
    },
    "anniversary_edition": {
        "dedup":     "song anniversary edition",
        "upgrade":   "song anniversary edition",
        "lidarr":    "song anniversary edition",
        "canonical": "song",
    },
    "single_version": {
        "dedup":     "song single version",
        "upgrade":   "song single version",
        "lidarr":    "song single version",
        "canonical": "song",
    },
    "radio_edit": {
        "dedup":     "song radio edit",
        "upgrade":   "song radio edit",
        "lidarr":    "song radio edit",
        "canonical": "song",
    },
    "expanded_edition": {
        "dedup":     "song expanded edition",
        "upgrade":   "song expanded edition",
        "lidarr":    "song expanded edition",
        "canonical": "song",
    },
    "bonus_track": {
        "dedup":     "song bonus track",
        "upgrade":   "song bonus track",
        "lidarr":    "song bonus track",
        "canonical": "song",
    },
    "bracket_remaster": {
        "dedup":     "song remastered 2019",
        "upgrade":   "song remastered 2019",
        "lidarr":    "song remastered 2019",
        "canonical": "song",
    },
    # --- Live / Acoustic / Demo (must stay distinct in canonical) ---
    "live_paren": {
        "dedup":     "cumbersome live",
        "upgrade":   "cumbersome live",
        "lidarr":    "cumbersome live",
        "canonical": "cumbersome live",   # Live NOT stripped — same as legacy
    },
    "acoustic_paren": {
        "dedup":     "blackbird acoustic",
        "upgrade":   "blackbird acoustic",
        "lidarr":    "blackbird acoustic",
        "canonical": "blackbird acoustic",
    },
    "demo_paren": {
        "dedup":     "yesterday demo",
        "upgrade":   "yesterday demo",
        "lidarr":    "yesterday demo",
        "canonical": "yesterday demo",
    },
    "live_bare": {
        # D7: dedup keeps "-"; lidarr deletes "-"; canonical maps "-" to space
        "dedup":     "stairway to heaven - live",
        "upgrade":   "stairway to heaven - live",
        "lidarr":    "stairway to heaven  live",  # "-" deleted, double space...
        # After lidarr collapse: "stairway to heaven live"
        "canonical": "stairway to heaven live",
    },
    # --- Track number prefix ---
    "track_number_prefix": {
        "dedup":     "01 - song",       # keeps " - "
        "upgrade":   "01 - song",
        "lidarr":    "01  song",        # "-" deleted → collapse → "01 song"
        "canonical": "01 song",         # "-" → space → collapse (no remaster match)
    },
    "track_number_prefix2": {
        "dedup":     "05 - bohemian rhapsody",
        "upgrade":   "05 - bohemian rhapsody",
        "lidarr":    "05  bohemian rhapsody",
        "canonical": "05 bohemian rhapsody",
    },
    # --- Unicode / punctuation ---
    # U+2019 RIGHT SINGLE QUOTATION MARK — not in string.punctuation for lidarr
    # dedup/upgrade: NFKD of U+2019 → U+0027 (apostrophe), then [^\w\s\-] removes it
    # lidarr: string.punctuation includes U+0027 (ASCII apostrophe) but U+2019 is
    #   NOT in string.punctuation (it's non-ASCII) → survives in lidarr output
    "unicode_dash_title": {
        "dedup":     "dont stop me now",   # NFKD(U+2019)→U+0027 → removed by regex
        "upgrade":   "dont stop me now",
        "lidarr":    "don’t stop me now",  # U+2019 not in string.punctuation
        "canonical": "dont stop me now",   # casefold + NFD → combining dropped; U+2019 → space
    },
    # U+2014 EM DASH — not in string.punctuation
    # dedup/upgrade: NFKD(U+2014) → U+2014 (no decomposition); [^\w\s\-] removes it
    # lidarr: U+2014 not in string.punctuation → survives
    # canonical: [^\w\s] matches U+2014 → replaced with space
    "em_dash_title": {
        "dedup":     "song live version",  # em dash removed (not hyphen, not \w)
        "upgrade":   "song live version",
        "lidarr":    "song — live version",  # em dash not in string.punctuation
        "canonical": "song live version",   # em dash → space → collapse
    },
    "full_title_normal": {
        "dedup":     "dark side of the moon",  # strips "the "
        "upgrade":   "the dark side of the moon",
        "lidarr":    "the dark side of the moon",
        "canonical": "dark side of the moon",
    },
    # D6: dedup strips "a "; canonical keeps it
    "full_title_a_prefix": {
        "dedup":     "hard days night",    # strips "a ", removes apostrophe
        "upgrade":   "a hard days night",
        "lidarr":    "a hard days night",  # apostrophe is in string.punctuation → removed
        "canonical": "a hard days night",  # "a " NOT stripped; apostrophe → space → collapse
    },
    "title_punctuation_heavy": {
        "dedup":     "dont stop me now",
        "upgrade":   "dont stop me now",
        "lidarr":    "dont stop me now",
        "canonical": "dont stop me now",
    },
    "title_colon": {
        "dedup":     "hello the collection",  # ":" removed; strips "the " → wait
        # After removing ":": "hello the collection" → check article strip:
        # starts with "hello", not "the " → no strip
        "upgrade":   "hello the collection",
        "lidarr":    "hello the collection",
        "canonical": "hello the collection",  # ":" → space → "hello  the collection" → collapse
    },
    "title_period": {
        "dedup":     "mr brightside",    # "." removed
        "upgrade":   "mr brightside",
        "lidarr":    "mr brightside",
        "canonical": "mr brightside",
    },
    "title_amp_title": {
        "dedup":     "young beautiful",  # "&" removed
        "upgrade":   "young beautiful",
        "lidarr":    "young  beautiful", # "&" deleted → collapse → "young beautiful"
        "canonical": "young beautiful",  # "&" → space → collapse
    },
    # Combined feat + remaster — canonical strips both, legacy keep both
    "combined_feat_remaster": {
        # dedup: removes "(", ".", ")" from both groups
        "dedup":     "song feat drake remastered 2019",
        "upgrade":   "song feat drake remastered 2019",
        # lidarr: feat regex matches "feat. Drake) (Remastered 2019)" → strips all
        # → "song (" → "(" deleted → "song " → "song"
        "lidarr":    "song",
        "canonical": "song",   # trailing paren feat strip, then remaster strip
    },
}

# Correct a few values that need precise tracing of lidarr's collapse step:
# lidarr does: str → lower → feat-strip → translate(punctuation→"") → re.sub(\s+, " ").strip()
# So double spaces from deletion always collapse to single.
_LIDARR_COLLAPSE_FIXES = {
    "artist_simon_garfunkel": "simon garfunkel",
    "live_bare": "stairway to heaven live",
    "track_number_prefix": "01 song",
    "track_number_prefix2": "05 bohemian rhapsody",
    "title_amp_title": "young beautiful",
    "em_dash_title": "song — live version",  # em dash stays (not in string.punctuation)
    "remaster_after_dash": "song 2011 remaster",
    "remaster_after_dash2": "song remastered",
}
for _k, _v in _LIDARR_COLLAPSE_FIXES.items():
    DIVERGENCE_TABLE[_k]["lidarr"] = _v


# ---------------------------------------------------------------------------
# Golden tests — each legacy normalizer's output is frozen
# ---------------------------------------------------------------------------

import pytest


@pytest.mark.parametrize("label,raw", CORPUS)
def test_golden_dedup(label, raw):
    """dedup.normalize_text output is byte-for-byte frozen."""
    if label not in DIVERGENCE_TABLE:
        pytest.skip(f"No golden entry for {label!r}")
    expected = DIVERGENCE_TABLE[label]["dedup"]
    assert dedup_norm(raw) == expected, (
        f"dedup.normalize_text({raw!r}) changed\n"
        f"  expected: {expected!r}\n"
        f"  got:      {dedup_norm(raw)!r}"
    )


@pytest.mark.parametrize("label,raw", CORPUS)
def test_golden_upgrade(label, raw):
    """upgrade_service._normalize_text output is byte-for-byte frozen."""
    if label not in DIVERGENCE_TABLE:
        pytest.skip(f"No golden entry for {label!r}")
    expected = DIVERGENCE_TABLE[label]["upgrade"]
    assert upgrade_norm(raw) == expected, (
        f"upgrade_service._normalize_text({raw!r}) changed\n"
        f"  expected: {expected!r}\n"
        f"  got:      {upgrade_norm(raw)!r}"
    )


@pytest.mark.parametrize("label,raw", CORPUS)
def test_golden_lidarr(label, raw):
    """lidarr_client._normalize output is byte-for-byte frozen."""
    if label not in DIVERGENCE_TABLE:
        pytest.skip(f"No golden entry for {label!r}")
    expected = DIVERGENCE_TABLE[label]["lidarr"]
    assert lidarr_norm(raw) == expected, (
        f"lidarr_client._normalize({raw!r}) changed\n"
        f"  expected: {expected!r}\n"
        f"  got:      {lidarr_norm(raw)!r}"
    )


@pytest.mark.parametrize("label,raw", CORPUS)
def test_golden_canonical(label, raw):
    """normalize.normalize output matches divergence table."""
    if label not in DIVERGENCE_TABLE:
        pytest.skip(f"No golden entry for {label!r}")
    expected = DIVERGENCE_TABLE[label]["canonical"]
    assert canonical_norm(raw) == expected, (
        f"normalize.normalize({raw!r}) changed\n"
        f"  expected: {expected!r}\n"
        f"  got:      {canonical_norm(raw)!r}"
    )


# ---------------------------------------------------------------------------
# Differential test — the full frozen four-way table
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("label,raw", CORPUS)
def test_differential_all_four(label, raw):
    """
    Assert the complete four-way divergence snapshot for every corpus entry.

    If ANY normalizer's output changes for ANY entry this test breaks loudly
    with a diff that shows exactly what shifted.
    """
    if label not in DIVERGENCE_TABLE:
        pytest.skip(f"No divergence entry for {label!r}")
    row = DIVERGENCE_TABLE[label]
    actual = {
        "dedup":     dedup_norm(raw),
        "upgrade":   upgrade_norm(raw),
        "lidarr":    lidarr_norm(raw),
        "canonical": canonical_norm(raw),
    }
    mismatches = []
    for key in ("dedup", "upgrade", "lidarr", "canonical"):
        if actual[key] != row[key]:
            mismatches.append(
                f"  {key}: expected {row[key]!r}, got {actual[key]!r}"
            )
    assert not mismatches, (
        f"Divergence table drift for {label!r} ({raw!r}):\n" + "\n".join(mismatches)
    )


# ---------------------------------------------------------------------------
# Explicit divergence-category assertions
# ---------------------------------------------------------------------------

class TestDivergenceCategories:
    """High-signal assertions documenting the intended divergences by category."""

    def test_D1_remaster_only_canonical_strips(self):
        """D1: Remaster suffixes — canonical strips, all legacy keep."""
        raw = "Song (Remastered 2019)"
        assert canonical_norm(raw) == "song"
        assert dedup_norm(raw) != "song"
        assert upgrade_norm(raw) != "song"
        assert lidarr_norm(raw) != "song"

    def test_D2_feat_canonical_and_lidarr_strip_dedup_upgrade_keep(self):
        """D2: Feat suffix — canonical + lidarr strip; dedup + upgrade keep."""
        raw = "Track (feat. Drake)"
        assert canonical_norm(raw) == "track"
        assert lidarr_norm(raw) == "track"
        assert "feat" in dedup_norm(raw)
        assert "feat" in upgrade_norm(raw)

    def test_D3_slash_canonical_space_legacy_delete(self):
        """D3: AC/DC — canonical maps '/' to space (two tokens); legacy delete (one token)."""
        raw = "AC/DC"
        assert canonical_norm(raw) == "ac dc"        # two tokens
        assert dedup_norm(raw) == "acdc"              # one token
        assert upgrade_norm(raw) == "acdc"
        assert lidarr_norm(raw) == "acdc"

    def test_D4_the_prefix_canonical_and_dedup_strip_upgrade_lidarr_keep(self):
        """D4: 'The ' prefix — canonical + dedup strip; upgrade + lidarr keep."""
        raw = "The Beatles"
        assert canonical_norm(raw) == "beatles"
        assert dedup_norm(raw) == "beatles"
        assert upgrade_norm(raw) == "the beatles"
        assert lidarr_norm(raw) == "the beatles"

    def test_D5_diacritics_lidarr_keeps_others_strip(self):
        """D5: Diacritics — canonical + dedup + upgrade strip; lidarr keeps."""
        raw = "Beyoncé"
        assert canonical_norm(raw) == "beyonce"
        assert dedup_norm(raw) == "beyonce"
        assert upgrade_norm(raw) == "beyonce"
        assert lidarr_norm(raw) == "beyoncé"   # é survives

    def test_D6_leading_a_dedup_strips_canonical_keeps(self):
        """D6: 'A ' prefix — dedup strips; canonical, upgrade, lidarr keep."""
        raw = "A Tribe Called Quest"
        assert "tribe" in canonical_norm(raw)
        assert canonical_norm(raw).startswith("a ")
        dedup_result = dedup_norm(raw)
        # dedup strips "a ": result starts with "tribe"
        assert dedup_result.startswith("tribe")

    def test_D6_leading_an_dedup_strips_canonical_keeps(self):
        """D6: 'An ' prefix — dedup strips; canonical keeps."""
        raw = "An Horse"
        assert canonical_norm(raw).startswith("an ")
        assert dedup_norm(raw) == "horse"

    def test_live_same_across_all_four(self):
        """'(Live)' is NOT stripped by any normalizer — all four agree."""
        raw = "Cumbersome (Live)"
        canonical = canonical_norm(raw)
        assert "live" in canonical
        assert canonical_norm(raw) == dedup_norm(raw) == upgrade_norm(raw) == lidarr_norm(raw)
