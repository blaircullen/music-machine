import re
import struct
import base64
import unicodedata
from collections import defaultdict
from typing import Iterator

from scanner import quality_score

import logging

logger = logging.getLogger(__name__)


def normalize_text(text: str) -> str:
    """Normalize text for duplicate comparison."""
    if not text:
        return ""
    # Unicode normalization
    text = unicodedata.normalize("NFKD", text)
    text = text.lower().strip()
    # Strip punctuation except hyphens
    text = re.sub(r"[^\w\s\-]", "", text)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    # Strip common leading articles
    for article in ("the ", "a ", "an "):
        if text.startswith(article):
            text = text[len(article):]
            break
    return text.strip()


def fingerprint_similarity(fp1: str, fp2: str) -> float:
    """Compare two Chromaprint fingerprint strings. Returns 0.0 to 1.0."""
    if not fp1 or not fp2:
        return 0.0
    try:
        # Chromaprint fingerprints are base64url-encoded packed int32 arrays
        # Pad to multiple of 4
        def decode_fp(fp: str) -> list[int]:
            # Convert base64url to standard base64
            padded = fp.replace("-", "+").replace("_", "/")
            pad = (4 - len(padded) % 4) % 4
            padded += "=" * pad
            raw = base64.b64decode(padded)
            # Unpack as little-endian uint32 array
            count = len(raw) // 4
            return list(struct.unpack(f"<{count}I", raw[:count * 4]))

        ints1 = decode_fp(fp1)
        ints2 = decode_fp(fp2)

        if not ints1 or not ints2:
            return 0.0

        # Compare over the shorter length
        length = min(len(ints1), len(ints2))
        if length == 0:
            return 0.0

        # Count matching bits across all compared integers
        total_bits = length * 32
        matching_bits = 0
        for i in range(length):
            xor = ints1[i] ^ ints2[i]
            # Count zero bits in xor (matching bits) = 32 - popcount(xor)
            matching_bits += 32 - bin(xor).count("1")

        return matching_bits / total_bits

    except Exception as e:
        logger.debug(f"Fingerprint comparison error: {e}")
        return 0.0


def _duration_confidence(durations: list[float]) -> float:
    """Compute confidence from duration similarity."""
    valid = [d for d in durations if d and d > 0]
    if len(valid) < 2:
        return 0.60

    avg = sum(valid) / len(valid)
    if avg == 0:
        return 0.60

    max_deviation = max(abs(d - avg) / avg for d in valid)

    if max_deviation < 0.02:
        return 0.90
    elif max_deviation < 0.05:
        return 0.80
    elif max_deviation < 0.10:
        return 0.65
    else:
        return 0.60


def _within_duration_threshold(tracks: list[dict], threshold_seconds: float = 5.0) -> bool:
    """Check that all tracks with duration data are within threshold_seconds of each other."""
    durations = [t.get("duration") for t in tracks if t.get("duration") and t["duration"] > 0]
    if len(durations) < 2:
        return True
    min_d = min(durations)
    max_d = max(durations)
    return (max_d - min_d) <= threshold_seconds


def find_duplicates(tracks: list[dict]) -> list[dict]:
    """
    Full duplicate detection pipeline.

    Stage 1: Group by (normalized artist, normalized title).
             Album is NOT required — same song on different albums is still a dupe.
             Duration must be within ±5 seconds.

    Stage 2: For groups where all members have fingerprints, compute fingerprint similarity.
             If >= 0.75 → confirmed duplicate (fingerprint match).
             Otherwise → metadata match with duration-based confidence.

    Returns list of group dicts: {match_type, confidence, tracks (sorted best first), keep_track, trash_tracks}
    """
    # Stage 1: metadata grouping
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for track in tracks:
        key = (
            normalize_text(track.get("artist") or ""),
            normalize_text(track.get("title") or ""),
        )
        # Skip tracks with empty artist AND empty title (ungroupable)
        if not key[0] and not key[1]:
            continue
        groups[key].append(track)

    results = []
    for key, members in groups.items():
        if len(members) < 2:
            continue

        # Duration gate: require all tracks to be within ±5 seconds
        if not _within_duration_threshold(members, threshold_seconds=5.0):
            # Try splitting into sub-groups by duration proximity
            sub_groups = _split_by_duration(members, threshold_seconds=5.0)
            for sub in sub_groups:
                if len(sub) >= 2:
                    result = _build_group_result(sub)
                    if result:
                        results.append(result)
        else:
            result = _build_group_result(members)
            if result:
                results.append(result)

    return results


def _split_by_duration(tracks: list[dict], threshold_seconds: float = 5.0) -> list[list[dict]]:
    """Split a group of tracks into sub-groups where all members are within threshold of each other."""
    remaining = list(tracks)
    groups = []

    while remaining:
        seed = remaining.pop(0)
        seed_dur = seed.get("duration") or 0
        group = [seed]
        still_remaining = []

        for t in remaining:
            t_dur = t.get("duration") or 0
            if seed_dur == 0 or t_dur == 0 or abs(t_dur - seed_dur) <= threshold_seconds:
                group.append(t)
            else:
                still_remaining.append(t)

        groups.append(group)
        remaining = still_remaining

    return groups


def _build_group_result(members: list[dict]) -> dict | None:
    """Build a group result dict from a list of track dicts. Returns None if not a real group."""
    if len(members) < 2:
        return None

    # Stage 2: fingerprint check
    fingerprints = [m.get("fingerprint") for m in members]
    all_have_fingerprints = all(fp for fp in fingerprints)

    match_type = "metadata"
    confidence = _duration_confidence([m.get("duration") for m in members])

    if all_have_fingerprints:
        # Compare all pairs — take the minimum similarity (most conservative)
        min_similarity = 1.0
        for i in range(len(fingerprints)):
            for j in range(i + 1, len(fingerprints)):
                sim = fingerprint_similarity(fingerprints[i], fingerprints[j])
                min_similarity = min(min_similarity, sim)

        if min_similarity >= 0.75:
            match_type = "fingerprint"
            confidence = max(0.95, min(0.99, 0.95 + (min_similarity - 0.75) * 0.2))
        else:
            # Fingerprints available but don't match well — lower confidence
            confidence = min(confidence, 0.60)

    # Rank tracks by quality score
    ranked = sorted(members, key=lambda t: quality_score(t), reverse=True)
    keep_track = ranked[0]
    trash_tracks = ranked[1:]

    return {
        "match_type": match_type,
        "confidence": confidence,
        "tracks": ranked,
        "keep_track": keep_track,
        "trash_tracks": trash_tracks,
    }
