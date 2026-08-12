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
