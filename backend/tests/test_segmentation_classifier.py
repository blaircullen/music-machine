"""
Segmentation classifier tests (docs/live-holiday-split-spec.md §5).

Exercises the pure classify() decision surface: strong holiday album/genre/title matches,
weak-word-alone → review, weak-word-with-strong-co-occurrence → auto, high-confidence live
parenthetical → auto, bare 'live' substring → review, stoplist suppression, and the
holiday-wins-over-live precedence rule.
"""

from segmentation_classifier import classify


def _matched(res):
    return res[0]


def _target(res):
    return res[1]


def _tier(res):
    return res[4]


# --- HOLIDAY strong signals → auto -----------------------------------------

def test_strong_holiday_album_match_auto():
    res = classify(title="Jingle Bells", album="A Christmas Album", genre=None)
    assert _matched(res)
    assert _target(res) == "holiday"
    assert res[2] == "album"          # matched_field
    assert _tier(res) == "auto"


def test_strong_holiday_genre_match_auto():
    # Weak title word, but resolved genre normalizes to Holiday → strong → auto.
    res = classify(title="Winter", album="Some Album", genre="Holiday")
    assert _matched(res)
    assert _target(res) == "holiday"
    assert res[2] == "genre"
    assert _tier(res) == "auto"


def test_strong_holiday_title_term_auto():
    res = classify(title="Feliz Navidad", album="Greatest Hits", genre=None)
    assert _matched(res)
    assert _target(res) == "holiday"
    assert res[2] == "title"
    assert _tier(res) == "auto"


# --- HOLIDAY weak signals → review -----------------------------------------

def test_weak_word_alone_goes_to_review():
    # "Winter" the indie song — weak word, no strong co-occurrence → review, never auto.
    res = classify(title="Winter", album="Youth", genre=None)
    assert _matched(res)
    assert _target(res) == "holiday"
    assert _tier(res) == "review"


def test_snow_hey_oh_weak_word_review():
    res = classify(title="Snow (Hey Oh)", album="Stadium Arcadium", genre=None)
    assert _matched(res)
    assert _tier(res) == "review"


def test_weak_word_with_strong_cooccurrence_auto():
    # "Jingle Bells" (weak: jingle/bells) but album is a Christmas comp → strong → auto.
    res = classify(title="Jingle Bells", album="Now That's What I Call Christmas", genre=None)
    assert _matched(res)
    assert _target(res) == "holiday"
    assert _tier(res) == "auto"


def test_white_christmas_without_holiday_signal_review():
    # "christmas" is a strong title term, but White Christmas covers exist → review unless a
    # holiday album/genre co-occurs (§5B special case).
    res = classify(title="White Christmas", album="Duo", genre=None)
    assert _matched(res)
    assert _target(res) == "holiday"
    assert _tier(res) == "review"


def test_white_christmas_with_holiday_album_auto():
    res = classify(title="White Christmas", album="A Christmas Gift For You", genre=None)
    assert _matched(res)
    assert _target(res) == "holiday"
    assert _tier(res) == "auto"


# --- LIVE signals -----------------------------------------------------------

def test_high_confidence_live_parenthetical_auto():
    res = classify(title="Thunderstruck (Live at Donington)", album="Live", genre=None)
    assert _matched(res)
    assert _target(res) == "live"
    assert _tier(res) == "auto"


def test_live_at_positional_phrase_auto():
    res = classify(title="Live at Wembley", album="Wembley 1986", genre=None)
    assert _matched(res)
    assert _target(res) == "live"
    assert _tier(res) == "auto"


def test_trailing_dash_live_marker_auto():
    res = classify(title="Thunderstruck - Live", album="Best Of", genre=None)
    assert _matched(res)
    assert _target(res) == "live"
    assert _tier(res) == "auto"


def test_bare_live_substring_goes_to_review():
    # "Live Wire" — bare live token, not an anchored marker → review.
    res = classify(title="Live Wire", album="High Voltage", genre=None)
    assert _matched(res)
    assert _target(res) == "live"
    assert _tier(res) == "review"


def test_acoustic_weak_live_review():
    res = classify(title="Layla (Acoustic)", album="Unplugged Sessions", genre=None)
    # Note: "Unplugged" only checked in title; title has "acoustic" (weak) → review.
    assert _matched(res)
    assert _target(res) == "live"
    assert _tier(res) == "review"


# --- Stoplist suppression ---------------------------------------------------

def test_stoplist_livin_on_a_prayer_not_flagged():
    res = classify(title="Livin' on a Prayer", album="Slippery When Wet", genre=None)
    assert not _matched(res)


def test_stoplist_stayin_alive_not_flagged():
    res = classify(title="Stayin' Alive", album="Saturday Night Fever", genre=None)
    assert not _matched(res)


def test_stoplist_live_and_let_die_not_flagged():
    res = classify(title="Live and Let Die", album="Band on the Run", genre=None)
    assert not _matched(res)


# --- Precedence: holiday wins over live ------------------------------------

def test_holiday_wins_over_live_precedence():
    # "Christmas (Live)": strong holiday title term AND strong live parenthetical.
    # Holiday wins → target holiday, auto.
    res = classify(title="Christmas (Live)", album="Holiday Concert", genre=None)
    assert _matched(res)
    assert _target(res) == "holiday"
    assert _tier(res) == "auto"


def test_no_match_ordinary_track():
    res = classify(title="Bohemian Rhapsody", album="A Night at the Opera", genre="Rock")
    assert not _matched(res)
