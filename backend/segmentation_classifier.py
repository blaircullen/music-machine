"""
Live & Holiday segmentation classifier (docs/live-holiday-split-spec.md §5).

Per-track classification from resolved metadata (AudD-corrected preferred, raw tags
fallback — §2). All patterns are editable module-level data (lists of compiled regexes,
case-insensitive, Unicode-normalized, word/marker-anchored — never bare substring). Pure
`classify(title, album, genre)` has no DB dependency, so it is trivially unit-testable;
`resolve_metadata()` / `classify_track()` layer the §2 resolution rule on top for the mover
and sweep.

Output shape (per track):
    (matched: bool, target_library, matched_field, matched_pattern, tier, confidence_reason)
where target_library ∈ {'live','holiday'|None}, tier ∈ {'auto','review'|None}.

Precedence when a track matches both live and holiday: **holiday wins** (§5). A "Christmas
(Live)" track is more surprising in summer shuffle than in a live-only library, so it lands
in Holiday. Confidence-tier decision (§5C): a track is `auto` (physically movable without
review) ONLY on a §5A high-confidence live marker OR a §5B strong holiday signal; everything
else — any weak/single-word holiday keyword, any bare `live` token — is `review`.
"""

import re
import unicodedata
from typing import NamedTuple, Optional, Tuple

# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

# (matched, target_library, matched_field, matched_pattern, tier, confidence_reason)
ClassifyResult = Tuple[bool, Optional[str], Optional[str], Optional[str], Optional[str], Optional[str]]

NO_MATCH: ClassifyResult = (False, None, None, None, None, None)


class _Sig(NamedTuple):
    """An internal per-domain signal hit before precedence is applied."""
    target: str          # 'live' | 'holiday'
    field: str           # 'title' | 'album' | 'genre'
    pattern: str         # human label of the marker that fired
    tier: str            # 'auto' | 'review'
    reason: str


# ---------------------------------------------------------------------------
# Editable pattern data — tune here, no control-flow change needed (§5)
# ---------------------------------------------------------------------------

_F = re.IGNORECASE | re.UNICODE

# 5A. LIVE — high-confidence, anchored structural markers (title). tier=auto.
LIVE_STRONG_TITLE = [
    (r"\((?:live[^)]*)\)", "(Live…) parenthetical"),
    (r"\[(?:live[^\]]*)\]", "[Live…] bracket"),
    (r"\blive (?:at|from|in|on)\b", "live at/from/in/on"),
    (r"-\s*live\b", "- Live trailing marker"),
    (r"\blive version\b", "live version"),
    (r"\blive recording\b", "live recording"),
    (r"\bunplugged\b", "unplugged"),
    (r"\bmtv unplugged\b", "mtv unplugged"),
]

# 5A. LIVE — ambiguous markers (title). tier=review. Only fire when no strong live marker.
LIVE_WEAK_TITLE = [
    (r"\bacoustic\b", "acoustic"),
    (r"\bsession(?:s)?\b", "session(s)"),
    (r"\bconcert\b", "concert"),
]

# A bare live-family token that is not one of the anchored strong markers → review.
LIVE_BARE_TOKEN = (r"\b(?:live|living|alive|livin)\b", "bare live token")

# Known non-live titles — suppressed even from the review queue to cut noise (§5A).
# Compared against the normalized title (apostrophes stripped, whitespace collapsed).
LIVE_STOPLIST = [
    "livewire",
    "alive",
    "live and let die",
    "livin on a prayer",       # "Livin' on a Prayer" (apostrophe stripped by _norm)
    "living on a prayer",
    "stayin alive",            # "Stayin' Alive"
    "live to tell",
    "live and learn",
]

# 5B. HOLIDAY — strong album-scoped occasion terms (preferred anchor). tier=auto.
HOLIDAY_STRONG_ALBUM = [
    (r"\bchristmas\b", "album: christmas"),
    (r"\bx-?mas\b", "album: xmas"),
    (r"\bholiday(?:s)?\b", "album: holiday"),
    (r"\bno[eë]l?\b", "album: noel"),
    (r"\bnavidad\b", "album: navidad"),
    (r"\bhanukkah\b", "album: hanukkah"),
    (r"\bkwanzaa\b", "album: kwanzaa"),
]

# 5B. HOLIDAY — strong whole-word occasion terms in TITLE. tier=auto.
# NOTE: generic "holiday" is intentionally absent (title "Holiday" is an ordinary song).
HOLIDAY_STRONG_TITLE = [
    (r"\bchristmas\b", "title: christmas"),
    (r"\bx-?mas\b", "title: xmas"),
    (r"\bhanukkah\b", "title: hanukkah"),
    (r"\bfeliz navidad\b", "title: feliz navidad"),
    (r"\bauld lang syne\b", "title: auld lang syne"),
    (r"\bhalloween\b", "title: halloween"),
]

# "White Christmas" needs a co-occurring holiday album/genre signal to auto (§5B).
HOLIDAY_WHITE_CHRISTMAS = (r"\bwhite christmas\b", "title: white christmas")

# 5B. HOLIDAY — weak single words that collide with ordinary songs. tier=review alone;
# promote to auto ONLY when co-occurring with a strong signal.
HOLIDAY_WEAK = [
    (r"\bwinter\b", "winter"),
    (r"\bsnow\b", "snow"),
    (r"\bbell(?:s)?\b", "bell(s)"),
    (r"\bjingle\b", "jingle"),
    (r"\bsanta\b", "santa"),
    (r"\bsleigh\b", "sleigh"),
    (r"\bmistletoe\b", "mistletoe"),
    (r"\breindeer\b", "reindeer"),
    (r"\bfrosty\b", "frosty"),
    (r"\bnoel\b", "noel"),
    (r"\bspooky\b", "spooky"),
    (r"\bpumpkin\b", "pumpkin"),
    (r"\bfireworks\b", "fireworks"),
    (r"\bindependence day\b", "independence day"),
    (r"\bbirthday\b", "birthday"),
    (r"\bnew year'?s?\b", "new year"),
    (r"\bthanksgiving\b", "thanksgiving"),
    (r"\beaster\b", "easter"),
]

# Compile once.
_LIVE_STRONG_TITLE = [(re.compile(p, _F), lbl) for p, lbl in LIVE_STRONG_TITLE]
_LIVE_WEAK_TITLE = [(re.compile(p, _F), lbl) for p, lbl in LIVE_WEAK_TITLE]
_LIVE_BARE_TOKEN = (re.compile(LIVE_BARE_TOKEN[0], _F), LIVE_BARE_TOKEN[1])
_HOLIDAY_STRONG_ALBUM = [(re.compile(p, _F), lbl) for p, lbl in HOLIDAY_STRONG_ALBUM]
_HOLIDAY_STRONG_TITLE = [(re.compile(p, _F), lbl) for p, lbl in HOLIDAY_STRONG_TITLE]
_HOLIDAY_WHITE_CHRISTMAS = (re.compile(HOLIDAY_WHITE_CHRISTMAS[0], _F), HOLIDAY_WHITE_CHRISTMAS[1])
_HOLIDAY_WEAK = [(re.compile(p, _F), lbl) for p, lbl in HOLIDAY_WEAK]


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

def _norm(s: Optional[str]) -> str:
    """Lowercase, NFKC-normalize, straighten/strip apostrophes, collapse whitespace."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", str(s))
    s = s.replace("’", "'").replace("‘", "'").replace("′", "'")
    s = s.replace("'", "")                    # drop apostrophes for token/stoplist matching
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s


def _genre_is(genre: Optional[str], label: str) -> bool:
    """True if the resolved (already-normalized) genre equals the given category, case-insensitive."""
    return bool(genre) and str(genre).strip().lower() == label.lower()


# ---------------------------------------------------------------------------
# Per-domain classification
# ---------------------------------------------------------------------------

def _classify_live(title_n: str, genre: Optional[str]) -> Optional[_Sig]:
    # Strong genre signal (only if AudD emits a "Live" genre — [VERIFY AT BUILD] §5A).
    if _genre_is(genre, "Live"):
        return _Sig("live", "genre", "genre: Live", "auto", "resolved genre = Live")

    # Strong anchored title markers → auto.
    for rx, lbl in _LIVE_STRONG_TITLE:
        if rx.search(title_n):
            return _Sig("live", "title", lbl, "auto",
                        f"title has high-confidence live marker: {lbl}")

    # Stoplist: suppress known non-live titles entirely (not even review).
    for phrase in LIVE_STOPLIST:
        if phrase in title_n:
            return None

    # Weak title markers → review.
    for rx, lbl in _LIVE_WEAK_TITLE:
        if rx.search(title_n):
            return _Sig("live", "title", lbl, "review",
                        f"title has ambiguous live marker '{lbl}' with no anchored marker")

    # Bare live-family token → review.
    rx, lbl = _LIVE_BARE_TOKEN
    if rx.search(title_n):
        return _Sig("live", "title", lbl, "review",
                    "title has a bare 'live' token with no anchored marker")

    return None


def _classify_holiday(title_n: str, album_n: str, genre: Optional[str]) -> Optional[_Sig]:
    genre_holiday = _genre_is(genre, "Holiday")

    # Strong album term → auto (album-level anchoring is the strongest signal).
    for rx, lbl in _HOLIDAY_STRONG_ALBUM:
        if rx.search(album_n):
            return _Sig("holiday", "album", lbl, "auto",
                        f"album has strong occasion term: {lbl}")

    # Strong genre → auto.
    if genre_holiday:
        return _Sig("holiday", "genre", "genre: Holiday", "auto",
                    "resolved genre normalizes to Holiday")

    # "White Christmas" special-case: strong only when co-occurring with holiday album/genre.
    rx, lbl = _HOLIDAY_WHITE_CHRISTMAS
    if rx.search(title_n):
        # album/genre already checked above and did not fire → review.
        return _Sig("holiday", "title", lbl, "review",
                    "title 'white christmas' without a holiday album/genre signal (covers exist)")

    # Strong title occasion term → auto.
    for rx, lbl in _HOLIDAY_STRONG_TITLE:
        if rx.search(title_n):
            return _Sig("holiday", "title", lbl, "auto",
                        f"title has strong occasion term: {lbl}")

    # Weak single words → review alone (promotion to auto only happens via a strong signal
    # above, which would already have returned). A weak word alone is never auto.
    for rx, lbl in _HOLIDAY_WEAK:
        if rx.search(title_n):
            return _Sig("holiday", "title", lbl, "review",
                        f"title has weak holiday word '{lbl}' with no strong co-occurring signal")

    return None


# ---------------------------------------------------------------------------
# Public pure classifier
# ---------------------------------------------------------------------------

def classify(title: Optional[str], album: Optional[str], genre: Optional[str]) -> ClassifyResult:
    """Classify one track from resolved metadata. Pure — no DB.

    `genre` is the resolved, already-normalized genre category (e.g. 'Holiday' / 'Live') or
    None. Precedence: holiday wins over live (§5). Returns the 6-tuple documented above.
    """
    title_n = _norm(title)
    album_n = _norm(album)

    live = _classify_live(title_n, genre)
    holiday = _classify_holiday(title_n, album_n, genre)

    if holiday and live:
        # Holiday wins the library assignment (§5). If holiday is a strong (auto) signal it
        # moves as holiday-auto. If holiday is only a weak (review) signal but live is strong,
        # the track is genuinely ambiguous under holiday-precedence → send to review (never
        # silently auto-move to live, never auto-move a weak holiday word).
        if holiday.tier == "auto":
            return _to_result(holiday)
        reason = (f"{holiday.reason}; also matched live ({live.pattern}) — "
                  "holiday precedence, routed to review")
        return (True, holiday.target, holiday.field, holiday.pattern, "review", reason)

    if holiday:
        return _to_result(holiday)
    if live:
        return _to_result(live)
    return NO_MATCH


def _to_result(sig: _Sig) -> ClassifyResult:
    return (True, sig.target, sig.field, sig.pattern, sig.tier, sig.reason)


# ---------------------------------------------------------------------------
# DB-aware resolution (§2) + track classification
# ---------------------------------------------------------------------------

def resolve_metadata(db, track_row) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Resolve (title, album, genre) for a track per §2.

    Prefers fingerprint_results.matched_{title,album,genre} when a row exists with
    status='applied'; otherwise falls back to tracks.{title,album} raw tags. Genre is only
    ever available from fingerprint_results (matched_genre, the normalized value); None when
    absent, in which case genre-based signals simply don't fire.

    `track_row` is a sqlite3.Row / mapping exposing id, title, album (tracks.*). `db` is an
    open connection.
    """
    track_id = track_row["id"]
    title = track_row["title"]
    album = track_row["album"]
    genre = None

    fp = db.execute(
        """SELECT matched_title, matched_album, matched_genre, status
             FROM fingerprint_results WHERE track_id = ?""",
        (track_id,),
    ).fetchone()
    if fp is not None and fp["status"] == "applied":
        if fp["matched_title"]:
            title = fp["matched_title"]
        if fp["matched_album"]:
            album = fp["matched_album"]
        if fp["matched_genre"]:
            genre = fp["matched_genre"]
    return title, album, genre


def classify_track(db, track_row) -> ClassifyResult:
    """Resolve metadata (§2) then classify (§5) for a live tracks row."""
    title, album, genre = resolve_metadata(db, track_row)
    return classify(title, album, genre)
