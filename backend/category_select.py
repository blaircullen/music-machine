"""Read-only category selector for identity corrections.

Usage:  category_select.py <category> [out_file]
Categories: fill_empty_artist | concat_feat_artist | artist_spelling | all_artist

Classifies each review-bucket row's current artist vs the resolver PICK and emits
the matching track_ids (one per line) to out_file, or stdout if omitted. Performs
NO writes to the DB or to any audio file. Excludes trashed/staging paths.

Classification (artist field):
  fill_empty_artist   : current artist blank, PICK has one
  concat_feat_artist  : same names, broken separators ('2PacDr. Dre' ->
                        '2Pac feat. Dr. Dre'); PICK is longer (inserted separator)
  artist_spelling     : close edit (ratio >= 0.82) — misspelling/accent/cruft
  artist_reattribution: genuinely different artist (NOT emitted by all_artist;
                        needs human judgment, surfaced only when asked explicitly)
"""
import re
import sqlite3
import sys
from difflib import SequenceMatcher

CATEGORIES = ("fill_empty_artist", "concat_feat_artist",
              "artist_spelling", "artist_reattribution", "all_artist")
SAFE_SET = {"fill_empty_artist", "concat_feat_artist", "artist_spelling"}
DB_PATH = "/data/music-machine.db"


def norm(v):
    return (v or "").strip().lower()


def collapse(v):
    s = (v or "").lower()
    s = re.sub(r"\b(feat\.?|ft\.?|featuring|with|and|vs\.?|x)\b", " ", s)
    s = re.sub(r"[^a-z0-9]", "", s)
    return s


def ratio(a, b):
    return SequenceMatcher(None, norm(a), norm(b)).ratio()


def classify_artist(ca, pa):
    """Return the category for an artist change, or None if no real change."""
    if norm(pa) == norm(ca):
        return None
    if not norm(ca):
        return "fill_empty_artist"
    if collapse(ca) == collapse(pa) and len(pa) > len(ca):
        return "concat_feat_artist"
    if ratio(ca, pa) >= 0.82:
        return "artist_spelling"
    return "artist_reattribution"


def select(category):
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    rows = db.execute(
        """SELECT ti.track_id, ti.artist AS p_artist, t.artist AS c_artist
           FROM track_identity ti JOIN tracks t ON t.id = ti.track_id
           WHERE ti.state = 'review' AND t.status = 'active'
             AND ti.artist IS NOT NULL
             AND t.file_path NOT LIKE '%/.fake-flac-trash%'
             AND t.file_path NOT LIKE '%/.recue-%'"""
    ).fetchall()
    want = SAFE_SET if category == "all_artist" else {category}
    return sorted({r["track_id"] for r in rows
                   if classify_artist(r["c_artist"], r["p_artist"]) in want})


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in CATEGORIES:
        print(f"usage: category_select.py <{'|'.join(CATEGORIES)}> [out_file]")
        sys.exit(1)
    category = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else None
    ids = select(category)
    text = "\n".join(str(i) for i in ids)
    if out:
        with open(out, "w") as fh:
            fh.write(text + "\n")
        print(f"{category}: {len(ids)} ids -> {out}")
    else:
        print(text)


if __name__ == "__main__":
    main()
