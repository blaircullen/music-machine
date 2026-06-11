"""
Behavior tests for backend/normalize.py.

These tests pin the canonical normalizer's contract.  Any change that breaks
a test here is intentional (update the test with a comment explaining why)
or a regression.
"""

import pytest
from normalize import normalize, similar


# ---------------------------------------------------------------------------
# normalize() — basic transformations
# ---------------------------------------------------------------------------

class TestNormalizeBasic:
    def test_empty_string(self):
        assert normalize("") == ""

    def test_none_like_empty(self):
        # The public contract says "empty / falsy → return ''"
        # normalize() accepts str; callers must coerce None first.
        # But guard it anyway via the falsy check:
        assert normalize("") == ""

    def test_casefold(self):
        assert normalize("Hello World") == "hello world"

    def test_casefold_german_sharp_s(self):
        # casefold turns ß → ss; .lower() would not
        assert normalize("Straße") == "strasse"

    def test_strips_diacritics(self):
        assert normalize("Beyoncé") == "beyonce"
        assert normalize("Björk") == "bjork"
        assert normalize("Sigur Rós") == "sigur ros"
        assert normalize("Motörhead") == "motorhead"

    def test_leading_the_stripped(self):
        assert normalize("The Beatles") == "beatles"
        assert normalize("The Rolling Stones") == "rolling stones"

    def test_leading_the_case_insensitive(self):
        # After casefold "THE " → "the ", should still strip
        assert normalize("THE BEATLES") == "beatles"

    def test_leading_a_NOT_stripped(self):
        # "A" is intentionally kept — "A Day in the Life" should not become
        # "Day in the Life".
        result = normalize("A Day in the Life")
        assert result.startswith("a ")

    def test_leading_an_NOT_stripped(self):
        result = normalize("An Honest Mistake")
        assert result.startswith("an ")

    def test_punctuation_collapsed_to_space(self):
        # "/" becomes a space
        result = normalize("AC/DC")
        assert result == "ac dc"

    def test_hyphen_becomes_space(self):
        result = normalize("Jean-Michel Jarre")
        # hyphen → space, then whitespace collapse
        assert result == "jean michel jarre"

    def test_whitespace_collapse(self):
        assert normalize("  hello   world  ") == "hello world"

    def test_ampersand_becomes_space(self):
        result = normalize("Simon & Garfunkel")
        assert result == "simon garfunkel"


# ---------------------------------------------------------------------------
# normalize() — featuring credit stripping
# ---------------------------------------------------------------------------

class TestNormalizeFeat:
    def test_feat_dot_bare(self):
        # "A feat. B" → "a"
        assert normalize("A feat. B") == "a"

    def test_ft_dot_bare(self):
        assert normalize("Song ft. Drake") == "song"

    def test_featuring_bare(self):
        assert normalize("Track featuring Rihanna") == "track"

    def test_feat_in_trailing_paren(self):
        # "Track (feat. Drake)" → "track"
        assert normalize("Track (feat. Drake)") == "track"

    def test_ft_in_trailing_paren(self):
        assert normalize("Track (ft. Drake)") == "track"

    def test_featuring_in_trailing_paren(self):
        assert normalize("Track (featuring Beyoncé)") == "track"

    def test_with_in_trailing_paren(self):
        # "with" is stripped only inside trailing parenthetical
        assert normalize("Track (with Drake)") == "track"

    def test_with_bare_NOT_stripped(self):
        # "Song with Guitar" — "with" as a bare separator is NOT stripped
        # (it only strips inside trailing parens to avoid over-matching
        # titles like "Dancing with Wolves")
        result = normalize("Dancing with Wolves")
        assert "with" in result

    def test_feat_separator_multi_artist(self):
        assert normalize("One Dance feat. Wizkid & Kyla") == "one dance"


# ---------------------------------------------------------------------------
# normalize() — edition / remaster suffix stripping
# ---------------------------------------------------------------------------

class TestNormalizeEditionSuffixes:
    def test_remastered_paren(self):
        assert normalize("Song (Remastered)") == "song"

    def test_remastered_year_paren(self):
        assert normalize("Song (Remastered 2019)") == "song"

    def test_year_remaster_paren(self):
        assert normalize("Song (2019 Remaster)") == "song"

    def test_remaster_after_dash(self):
        # "Song - 2011 Remaster" → "song"
        assert normalize("Song - 2011 Remaster") == "song"

    def test_remastered_after_dash(self):
        assert normalize("Song - Remastered") == "song"

    def test_deluxe_edition_paren(self):
        assert normalize("Album (Deluxe Edition)") == "album"

    def test_deluxe_bare_paren(self):
        assert normalize("Album (Deluxe)") == "album"

    def test_anniversary_edition_paren(self):
        assert normalize("Song (Anniversary Edition)") == "song"

    def test_single_version_paren(self):
        assert normalize("Song (Single Version)") == "song"

    def test_radio_edit_paren(self):
        assert normalize("Song (Radio Edit)") == "song"

    def test_expanded_edition_paren(self):
        assert normalize("Song (Expanded Edition)") == "song"

    def test_bonus_track_paren(self):
        assert normalize("Song (Bonus Track)") == "song"

    def test_bracket_variant(self):
        assert normalize("Song [Remastered 2019]") == "song"


# ---------------------------------------------------------------------------
# normalize() — suffixes that must NOT be stripped (different recordings)
# ---------------------------------------------------------------------------

class TestNormalizeLiveAcousticDemo:
    """Live, Acoustic, and Demo versions are different recordings — must stay distinct."""

    def test_live_NOT_stripped(self):
        result = normalize("Cumbersome (Live)")
        assert "live" in result

    def test_acoustic_NOT_stripped(self):
        result = normalize("Blackbird (Acoustic)")
        assert "acoustic" in result

    def test_demo_NOT_stripped(self):
        result = normalize("Yesterday (Demo)")
        assert "demo" in result

    def test_live_bare_NOT_stripped(self):
        result = normalize("Stairway to Heaven - Live")
        assert "live" in result

    def test_cumbersome_live_differs_from_cumbersome(self):
        """Core contract: (Live) version normalizes differently from studio version."""
        assert normalize("Cumbersome (Live)") != normalize("Cumbersome")

    def test_acoustic_differs(self):
        assert normalize("Blackbird (Acoustic)") != normalize("Blackbird")


# ---------------------------------------------------------------------------
# similar() — score semantics
# ---------------------------------------------------------------------------

class TestSimilarScores:
    def test_identical_strings(self):
        assert similar("song", "song") == 1.0

    def test_empty_empty(self):
        assert similar("", "") == 1.0

    def test_empty_nonempty(self):
        assert similar("", "something") == 0.0
        assert similar("something", "") == 0.0

    def test_the_beatles_vs_beatles(self):
        # "The Beatles" normalizes to "beatles"; "Beatles" → "beatles" → same token set
        score = similar("The Beatles", "Beatles")
        assert score >= 0.9

    def test_song_remastered_equals_song(self):
        # Both normalize to "song"
        assert similar("Song (Remastered 2019)", "Song") == 1.0

    def test_feat_stripped_before_compare(self):
        # "A feat. B" → "a"; compare with "A" → "a"
        assert similar("A feat. B", "A") == 1.0

    def test_diacritics_stripped_before_compare(self):
        # "Beyoncé" → "beyonce"
        assert similar("Beyoncé", "Beyonce") == 1.0

    def test_cumbersome_vs_live_less_than_1(self):
        score = similar("Cumbersome", "Cumbersome (Live)")
        assert score < 1.0

    def test_ac_dc_high_similarity(self):
        # "AC/DC" → "ac dc" (two tokens); "ACDC" → "acdc" (one token)
        # They won't be token-equal but should score reasonably high
        score = similar("AC/DC", "ACDC")
        assert score >= 0.6

    def test_completely_different(self):
        score = similar("Stairway to Heaven", "Bohemian Rhapsody")
        assert score < 0.2

    def test_symmetric(self):
        # similar(a, b) == similar(b, a)
        assert similar("The Beatles", "Beatles") == similar("Beatles", "The Beatles")

    def test_same_after_normalization(self):
        # Two different surface forms that normalize identically score 1.0
        assert similar("Song - 2011 Remaster", "Song") == 1.0

    def test_partial_containment(self):
        # "beatles" is contained in "beatles abbey road" → containment = 1.0
        # score = min(1.0 * 0.95, jaccard) — jaccard = 1/3 < 0.95 → 0.95
        score = similar("Beatles", "Beatles Abbey Road")
        assert score >= 0.9

    def test_score_in_range(self):
        for a, b in [("foo", "bar"), ("", ""), ("hello", "hello world"), ("x", "xyz")]:
            s = similar(a, b)
            assert 0.0 <= s <= 1.0, f"Out of range for ({a!r}, {b!r}): {s}"
