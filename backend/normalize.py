"""
Canonical string normalizer for the identity resolver.

This module is the single authoritative normalizer used by the resolver
pipeline (Phase 1).  The three legacy normalizers in dedup.py,
upgrade_service.py, and lidarr_client.py are NOT changed; this module
documents where and why the canonical behaviour diverges from each one.

Public API
----------
normalize(s: str) -> str
    Reduce a raw artist / title string to a stable comparison key.

similar(a: str, b: str) -> float
    Token-set similarity in [0, 1].  1.0 = same token set.

Divergence summary (detail in test_normalizer_golden.py)
---------------------------------------------------------
vs dedup.normalize_text
    - Canonical strips feat / remaster suffixes; dedup does not.
    - Canonical uses casefold(); dedup uses .lower().
    - Canonical drops ONLY "The " prefix; dedup also drops "a " / "an ".
    - Canonical collapses punctuation to spaces; dedup preserves hyphens.
    - Both strip diacritics (dedup via NFKD + regex, canonical via NFD +
      unicodedata.category filter).

vs upgrade_service._normalize_text
    - Canonical strips feat / remaster suffixes; upgrade does not.
    - Canonical drops "The " prefix; upgrade does not.
    - Canonical uses casefold(); upgrade uses .lower().

vs lidarr_client._normalize
    - Both strip feat / featuring / ft.
    - Canonical also strips remaster/edition suffixes; lidarr does not.
    - Canonical strips diacritics; lidarr does not.
    - Canonical drops "The " prefix; lidarr does not.
    - Both collapse all punctuation (lidarr uses string.punctuation,
      canonical uses a category-based approach that also covers
      unicode punctuation/symbols and maps them to spaces rather than
      deleting them — the spacing difference matters for token splits).
"""

from __future__ import annotations

import re
import unicodedata

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

# Suffixes that identify a different *release* but the SAME recording identity.
# These are safe to strip when comparing whether two strings refer to the same
# track.  "Live", "Acoustic", and "Demo" are intentionally NOT listed here —
# they denote genuinely different recordings.
_SUFFIX_PATTERNS: list[re.Pattern[str]] = [
    # Trailing parenthetical / bracketed edition suffixes, e.g.
    #   "Song (Remastered 2019)"  "(Deluxe Edition)"  "(Anniversary Edition)"
    #   "(Single Version)"  "(Radio Edit)"  "(Expanded Edition)"
    #   "(Bonus Track)"  "(2019 Remaster)"
    re.compile(
        r"""[\(\[]\s*
            (?:
                (?:\d{4}\s+)?remaster(?:ed)?(?:\s+\d{4})?  # (Remastered) / (2019 Remaster) / (Remastered 2019)
              | deluxe(?:\s+\w+)*                            # (Deluxe Edition) / (Deluxe)
              | anniversary(?:\s+\w+)*                       # (Anniversary Edition) / (25th Anniversary)
              | single\s+version                             # (Single Version)
              | radio\s+edit                                 # (Radio Edit)
              | expanded(?:\s+\w+)*                          # (Expanded Edition)
              | bonus\s+track(?:\s+\w+)*                     # (Bonus Track) / (Bonus Track Version)
            )
            \s*[\)\]]""",
        re.IGNORECASE | re.VERBOSE,
    ),
    # After a " - " separator, e.g. "Song - 2011 Remaster" / "Song - Remastered"
    re.compile(
        r"""\s+-\s+
            (?:
                (?:\d{4}\s+)?remaster(?:ed)?(?:\s+\d{4})?
              | deluxe(?:\s+\w+)*
              | anniversary(?:\s+\w+)*
              | single\s+version
              | radio\s+edit
              | expanded(?:\s+\w+)*
              | bonus\s+track(?:\s+\w+)*
            )
            \s*$""",
        re.IGNORECASE | re.VERBOSE,
    ),
]

# Featuring credit patterns.  These should be stripped regardless of position
# when they appear after the main title token (trailing parens, or after a
# feat-style separator in the title string).
#
# Patterns handled:
#   "A feat. B"          → "A"
#   "A ft. B"            → "A"
#   "A featuring B"      → "A"
#   "Track (feat. Drake)" → "Track"
#   "Track (ft. Drake)"   → "Track"
#   "Track (with Drake)"  → "Track"   (only inside trailing parens)
_FEAT_TRAILING_PAREN: re.Pattern[str] = re.compile(
    r"""\s*[\(\[]\s*
        (?:feat(?:uring)?\.?|ft\.?|with\b)
        \b.*?[\)\]]""",
    re.IGNORECASE | re.VERBOSE,
)

# "Song feat. Artist" / "Song feat Artist" / "Song ft. Artist" as a bare
# separator (not inside parens).
_FEAT_SEPARATOR: re.Pattern[str] = re.compile(
    r"""\s+(?:feat(?:uring)?\.?|ft\.?)\s+.*$""",
    re.IGNORECASE | re.VERBOSE,
)

# Leading "The " (case-insensitive, after casefolding it will be "the ").
# Only "The" — NOT "A" / "An" — because the resolver needs to match
# "The Beatles" ≈ "Beatles" but must not lose meaning from "A Day in the Life".
_LEADING_THE: re.Pattern[str] = re.compile(r"^the\s+")


def _strip_diacritics(s: str) -> str:
    """NFD-decompose then remove Unicode combining marks (category Mn)."""
    return "".join(
        ch for ch in unicodedata.normalize("NFD", s)
        if unicodedata.category(ch) != "Mn"
    )


def normalize(s: str) -> str:
    """
    Return a stable comparison key for an artist or track title string.

    Steps applied in order:
    1. Empty / falsy → return "".
    2. casefold  (handles ß → ss, Turkish I, etc. — stricter than .lower()).
    3. Strip diacritics via NFD decomposition + Mn-category drop.
    4. Strip trailing featuring credits (parenthetical form, then bare form).
    5. Strip trailing edition / remaster suffixes (parens/brackets or " - ").
    6. Drop leading "The ".
    7. Replace non-alphanumeric, non-space characters with a single space
       (covers ASCII punctuation, Unicode punctuation Pc/Pd/Pe/Pf/Pi/Po/Ps,
       Unicode symbols Sc/Sk/Sm/So, and miscellaneous separators).
    8. Collapse runs of whitespace to a single space and strip leading/trailing.

    Intentionally NOT stripped: "(Live)", "(Acoustic)", "(Demo)" — these
    denote genuinely different recordings and must remain distinct.

    Examples
    --------
    >>> normalize("The Beatles")
    'beatles'
    >>> normalize("Song (Remastered 2019)")
    'song'
    >>> normalize("A feat. B")
    'a'
    >>> normalize("Track (feat. Drake)")
    'track'
    >>> normalize("Beyoncé")
    'beyonce'
    >>> normalize("Cumbersome (Live)")   # Live NOT stripped
    'cumbersome live'
    >>> normalize("Song - 2011 Remaster")
    'song'
    """
    if not s:
        return ""

    # 1. casefold
    text = s.casefold()

    # 2. strip diacritics
    text = _strip_diacritics(text)

    # 3. strip featuring credits (parens form first, then bare separator)
    text = _FEAT_TRAILING_PAREN.sub("", text)
    text = _FEAT_SEPARATOR.sub("", text)

    # 4. strip edition / remaster suffixes
    for pat in _SUFFIX_PATTERNS:
        text = pat.sub("", text)

    # 5. drop leading "The "
    text = _LEADING_THE.sub("", text.lstrip())

    # 6. drop apostrophes (don't → dont, matching legacy normalizers),
    #    replace remaining non-alphanumeric non-space with space, collapse
    text = text.replace("'", "").replace("’", "")
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()

    return text


# ---------------------------------------------------------------------------
# Token-set similarity
# ---------------------------------------------------------------------------

def similar(a: str, b: str) -> float:
    """
    Return a token-set similarity score in [0.0, 1.0].

    Both strings are normalized before comparison.  Tokens are obtained by
    splitting on whitespace after normalization.

    Semantics
    ---------
    Let A and B be the token sets of the two normalized strings.

    - Both empty → 1.0  (two empty strings are identical)
    - One empty → 0.0
    - Otherwise:
        jaccard    = |A ∩ B| / |A ∪ B|
        containment = |A ∩ B| / min(|A|, |B|)
        score = max(jaccard, containment × 0.95)

    The containment term (dampened by 0.95) ensures that when one string's
    tokens are a strict subset of the other's, the score stays high even
    when the larger set adds tokens (e.g. "Beatles" vs "Beatles Abbey Road").
    The 0.95 damping prevents full-containment from reaching 1.0, so true
    equality (jaccard = 1.0) is always preferred.

    The pure jaccard rewards token equality; the blended max rewards partial
    containment without over-rewarding it.  "ACDC" vs "AC/DC" — both
    normalize to the same token set — score 1.0.

    Examples
    --------
    >>> similar("The Beatles", "Beatles")
    0.95
    >>> similar("", "")
    1.0
    >>> similar("", "something")
    0.0
    >>> similar("song", "song")
    1.0
    """
    na = normalize(a)
    nb = normalize(b)

    tokens_a = set(na.split()) if na else set()
    tokens_b = set(nb.split()) if nb else set()

    if not tokens_a and not tokens_b:
        return 1.0
    if not tokens_a or not tokens_b:
        return 0.0

    # Spacing-only variants ("AC/DC" → "ac dc" vs "ACDC" → "acdc") are the
    # same name; token sets can't see it, so compare squashed strings first.
    if na.replace(" ", "") == nb.replace(" ", ""):
        return 1.0

    intersection = tokens_a & tokens_b
    union = tokens_a | tokens_b

    jaccard = len(intersection) / len(union)
    containment = len(intersection) / min(len(tokens_a), len(tokens_b))

    return max(jaccard, containment * 0.95)
