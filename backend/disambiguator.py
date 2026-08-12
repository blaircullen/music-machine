"""
Multi-match disambiguation for AcoustID results.

When AcoustID returns multiple Recording IDs (common for popular songs),
this module selects the best release using a priority cascade.
"""

import logging
from difflib import SequenceMatcher

logger = logging.getLogger(__name__)

# Release type priority (lower = preferred)
RELEASE_TYPE_PRIORITY = {
    "album": 0,
    "ep": 1,
    "single": 2,
    "compilation": 3,
    "soundtrack": 4,
    "other": 5,
}

# Below this token-sorted similarity ratio, two normalized text strings
# (artist or title) are treated as genuinely different values — not a
# formatting/diacritic/word-order variant. Raised 2026-08-12 from 0.6 after
# confirmed false negatives at that floor: "The Beat"/"The Beatles" (0.842),
# "Muse"/"Museum" (0.800), "David Bowie"/"David Byrne" (0.727),
# "America"/"American Authors" (0.609) — all now correctly flagged as
# different at 0.88. Verified not to false-positive on formatting variants:
# "The Beatles"/"Beatles, The" (word-order — 1.0 via token sort), "Beyoncé"/
# "Beyonce" (diacritic — 1.0), "Various Artists"/"VA" (initialism — exempted
# below).
TEXT_SIMILARITY_FLOOR = 0.88


def _normalize_for_compare(s: str) -> str:
    import unicodedata

    s = unicodedata.normalize("NFKD", s).lower().strip()
    return "".join(c for c in s if c.isalnum() or c.isspace())


def _is_initialism(short: str, other: str) -> bool:
    """True when `short` is exactly the initials of `other`'s words (e.g.
    "va" / "various artists"). Guards a known-abbreviation formatting
    variant from being flagged as a genuine mismatch."""
    words = other.split()
    if len(words) < 2:
        return False
    initials = "".join(w[0] for w in words if w)
    return short == initials


def text_differs(existing: str, new: str, floor: float = TEXT_SIMILARITY_FLOOR) -> bool:
    """True when two text strings (artist or title) look like genuinely
    different values — not just a case/diacritic/word-order/"The"-prefix/
    known-abbreviation formatting difference.

    Deliberately does NOT use a blind substring-containment shortcut
    (dropped 2026-08-12) — "Chicago" is a substring of "Chicago Symphony
    Orchestra" and "Now" is a substring of "Now United", but those are
    different artists, not formatting variants. The fuzzy-ratio floor is
    the only signal, applied to word-sorted normalized text so pure
    reordering ("The Beatles" vs "Beatles, The") does not trip it.
    """
    a, b = _normalize_for_compare(existing), _normalize_for_compare(new)
    if not a or not b or a == b:
        return False
    if _is_initialism(a, b) or _is_initialism(b, a):
        return False
    a_sorted = " ".join(sorted(a.split()))
    b_sorted = " ".join(sorted(b.split()))
    return SequenceMatcher(None, a_sorted, b_sorted).ratio() < floor


def select_best_release(
    candidates: list[dict],
    existing_tags: dict | None = None,
    dir_lock: dict | None = None,
) -> dict | None:
    """
    Select the best release from multiple AcoustID/MB candidates.

    Priority cascade:
    1. Directory-level album lock (if set, force that release)
    2. Release type: Album > EP > Single > Compilation > Soundtrack > Other
    3. Existing tag hint: prefer release whose name is closest to current album tag
    4. Release date: prefer earliest original release
    5. Cover art availability
    6. Metadata completeness

    Each candidate dict should have:
        artist, title, album, album_artist, date, track_number, disc_number,
        total_tracks, release_group_id, release_id, isrc, label, composer,
        genre_tags
    """
    if not candidates:
        return None

    if len(candidates) == 1:
        return candidates[0]

    # 1. Directory lock — if we already locked to a release, find it
    if dir_lock and dir_lock.get("release_id"):
        for c in candidates:
            if c.get("release_id") == dir_lock["release_id"]:
                return c

    scored = []
    for c in candidates:
        score = _score_candidate(c, existing_tags)
        scored.append((score, c))

    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[0][1]


def _score_candidate(candidate: dict, existing_tags: dict | None) -> float:
    """Compute a ranking score for a candidate release (higher = better)."""
    score = 0.0

    # Release type preference (max 50 points)
    release_type = (candidate.get("release_type") or "other").lower()
    type_priority = RELEASE_TYPE_PRIORITY.get(release_type, 5)
    score += (5 - type_priority) * 10  # Album=50, EP=40, Single=30, etc.

    # Release status: official preferred (20 points)
    status = (candidate.get("release_status") or "").lower()
    if status == "official":
        score += 20

    # Existing tag similarity (max 30 points)
    if existing_tags and existing_tags.get("album") and candidate.get("album"):
        similarity = SequenceMatcher(
            None,
            _normalize(existing_tags["album"]),
            _normalize(candidate["album"]),
        ).ratio()
        score += similarity * 30

    # Earlier release date preferred (max 10 points)
    date = candidate.get("date") or ""
    if date and date != "9999":
        try:
            year = int(date[:4])
            # Give bonus to earlier releases (scale: 1950=10, 2025=0)
            score += max(0, min(10, (2030 - year) / 8))
        except (ValueError, IndexError):
            pass

    # Metadata completeness (max 15 points)
    completeness_fields = [
        "artist", "title", "album", "date", "track_number",
        "isrc", "label", "composer",
    ]
    present = sum(1 for f in completeness_fields if candidate.get(f))
    score += (present / len(completeness_fields)) * 15

    # Genre tags available (5 points)
    if candidate.get("genre_tags"):
        score += 5

    # Cover art availability hint (5 points)
    if candidate.get("release_group_id"):
        score += 5

    return score


def _normalize(text: str) -> str:
    """Normalize text for comparison."""
    import unicodedata

    text = unicodedata.normalize("NFKD", text)
    return text.strip().lower()


def build_dir_lock(results: list[dict]) -> dict | None:
    """
    Given a list of fingerprint results from the same directory,
    check if 2+ tracks matched the same release_id. If so, return
    a lock dict for that release.
    """
    release_votes: dict[str, dict] = {}
    for r in results:
        rid = r.get("release_id")
        if not rid:
            continue
        if rid not in release_votes:
            release_votes[rid] = {"count": 0, "data": r}
        release_votes[rid]["count"] += 1

    # Find the release with the most votes (minimum 2)
    best = None
    best_count = 1
    for rid, info in release_votes.items():
        if info["count"] > best_count:
            best = info["data"]
            best_count = info["count"]

    if best:
        return {
            "release_id": best.get("release_id"),
            "album": best.get("album") or best.get("matched_album", ""),
            "release_group_id": best.get("release_group_id"),
        }
    return None


def resolve_match_candidates(
    tier_matches: list[dict],
    fetch_metadata,
    existing_tags: dict | None = None,
    dir_lock: dict | None = None,
    score_margin: float = 0.02,
    max_candidates: int = 3,
) -> tuple[dict | None, str | None, bool]:
    """
    Resolve a list of tier-eligible AcoustID matches (sorted by score
    descending) to a single (metadata, recording_id, ambiguous) result,
    routing AcoustID score collisions through select_best_release() instead
    of blindly trusting whichever candidate happened to sort first.

    A collision is any set of top candidates within `score_margin` of the
    best score — a fingerprint score tie is not proof of identity (see
    fingerprint_engine.AMBIGUITY_SCORE_MARGIN docs). `fetch_metadata(
    recording_id)` supplies the MusicBrainz lookup so this stays agnostic
    to caller (local-mirror-aware fingerprint engine vs. public-API-only
    legacy tagger).

    Returns (metadata, recording_id, ambiguous). metadata is None when no
    candidate's metadata could be resolved at all.
    """
    best_score = tier_matches[0]["score"]
    close_candidates = [
        m for m in tier_matches[:max_candidates]
        if (best_score - m["score"]) <= score_margin
    ]
    ambiguous = len(close_candidates) > 1

    if not ambiguous:
        recording_id = tier_matches[0]["recording_id"]
        metadata = fetch_metadata(recording_id)
        return metadata, recording_id, ambiguous

    candidate_meta = []
    for m in close_candidates:
        md = fetch_metadata(m["recording_id"])
        if md:
            md["_recording_id"] = m["recording_id"]
            candidate_meta.append(md)

    if not candidate_meta:
        return None, None, ambiguous

    metadata = select_best_release(candidate_meta, existing_tags=existing_tags, dir_lock=dir_lock)
    recording_id = metadata.get("_recording_id", close_candidates[0]["recording_id"]) if metadata else None
    return metadata, recording_id, ambiguous
