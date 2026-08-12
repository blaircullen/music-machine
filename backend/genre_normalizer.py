"""
Genre normalization — maps MusicBrainz folksonomy tags and AudD genres
to ~30 curated categories.
"""

import logging

from database import get_db

logger = logging.getLogger(__name__)

# Default genre mappings: raw_tag → normalized_genre
# These are seeded into the genre_map table on first run.
DEFAULT_GENRE_MAP = {
    # Rock
    "rock": "Rock", "classic rock": "Rock", "alternative rock": "Rock",
    "indie rock": "Rock", "hard rock": "Rock", "progressive rock": "Rock",
    "soft rock": "Rock", "garage rock": "Rock", "psychedelic rock": "Rock",
    "southern rock": "Rock", "arena rock": "Rock", "glam rock": "Rock",
    # Pop
    "pop": "Pop", "synth-pop": "Pop", "synthpop": "Pop", "indie pop": "Pop",
    "dream pop": "Pop", "electropop": "Pop", "power pop": "Pop",
    "chamber pop": "Pop", "art pop": "Pop", "dance pop": "Pop",
    "teen pop": "Pop", "k-pop": "Pop", "j-pop": "Pop",
    # Hip-Hop
    "hip hop": "Hip-Hop", "hip-hop": "Hip-Hop", "rap": "Hip-Hop",
    "trap": "Hip-Hop", "conscious hip hop": "Hip-Hop", "gangsta rap": "Hip-Hop",
    "east coast hip hop": "Hip-Hop", "west coast hip hop": "Hip-Hop",
    "southern hip hop": "Hip-Hop", "boom bap": "Hip-Hop",
    # Electronic
    "electronic": "Electronic", "edm": "Electronic", "house": "Electronic",
    "techno": "Electronic", "trance": "Electronic", "drum and bass": "Electronic",
    "dubstep": "Electronic", "ambient": "Electronic", "idm": "Electronic",
    "downtempo": "Electronic", "breakbeat": "Electronic", "jungle": "Electronic",
    "electronica": "Electronic", "trip hop": "Electronic", "trip-hop": "Electronic",
    # R&B
    "r&b": "R&B", "rnb": "R&B", "rhythm and blues": "R&B",
    "neo-soul": "R&B", "contemporary r&b": "R&B", "new jack swing": "R&B",
    # Country
    "country": "Country", "country rock": "Country", "alt-country": "Country",
    "americana": "Country", "outlaw country": "Country", "country pop": "Country",
    "bluegrass": "Country", "honky tonk": "Country",
    # Jazz
    "jazz": "Jazz", "smooth jazz": "Jazz", "bebop": "Jazz", "fusion": "Jazz",
    "jazz fusion": "Jazz", "cool jazz": "Jazz", "free jazz": "Jazz",
    "swing": "Jazz", "big band": "Jazz", "vocal jazz": "Jazz",
    "acid jazz": "Jazz", "bossa nova": "Jazz",
    # Classical
    "classical": "Classical", "baroque": "Classical", "romantic": "Classical",
    "orchestral": "Classical", "symphony": "Classical", "chamber music": "Classical",
    "opera": "Classical", "choral": "Classical", "contemporary classical": "Classical",
    # Blues
    "blues": "Blues", "blues rock": "Blues", "delta blues": "Blues",
    "chicago blues": "Blues", "electric blues": "Blues", "rhythm and blues": "Blues",
    # Metal
    "metal": "Metal", "heavy metal": "Metal", "thrash metal": "Metal",
    "death metal": "Metal", "black metal": "Metal", "doom metal": "Metal",
    "power metal": "Metal", "progressive metal": "Metal",
    "nu metal": "Metal", "metalcore": "Metal", "speed metal": "Metal",
    # Punk
    "punk": "Punk", "punk rock": "Punk", "pop punk": "Punk",
    "hardcore punk": "Punk", "post-punk": "Punk", "skate punk": "Punk",
    "hardcore": "Punk", "emo": "Punk", "screamo": "Punk",
    # Folk
    "folk": "Folk", "folk rock": "Folk", "indie folk": "Folk",
    "acoustic": "Folk", "singer-songwriter": "Folk", "folk pop": "Folk",
    "neofolk": "Folk", "traditional folk": "Folk",
    # Soul
    "soul": "Soul", "motown": "Soul", "funk": "Soul", "neo soul": "Soul",
    "deep funk": "Soul", "p-funk": "Soul", "northern soul": "Soul",
    # Reggae
    "reggae": "Reggae", "ska": "Reggae", "dub": "Reggae",
    "dancehall": "Reggae", "roots reggae": "Reggae", "rocksteady": "Reggae",
    # Latin
    "latin": "Latin", "salsa": "Latin", "reggaeton": "Latin",
    "bachata": "Latin", "cumbia": "Latin", "latin pop": "Latin",
    "latin rock": "Latin", "merengue": "Latin", "samba": "Latin",
    # World
    "world": "World", "afrobeat": "World", "celtic": "World",
    "flamenco": "World", "arabic": "World", "indian classical": "World",
    "african": "World", "worldbeat": "World",
    # Gospel
    "gospel": "Gospel", "christian": "Gospel", "worship": "Gospel",
    "ccm": "Gospel", "christian rock": "Gospel",
    # Soundtrack
    "soundtrack": "Soundtrack", "film score": "Soundtrack", "musical": "Soundtrack",
    "video game music": "Soundtrack", "score": "Soundtrack",
    # New Wave
    "new wave": "New Wave", "synthwave": "New Wave", "darkwave": "New Wave",
    "coldwave": "New Wave", "minimal wave": "New Wave",
    # Disco
    "disco": "Disco", "nu-disco": "Disco", "boogie": "Disco",
    "italo disco": "Disco", "euro disco": "Disco",
    # Grunge
    "grunge": "Grunge", "seattle sound": "Grunge",
    # Experimental
    "experimental": "Experimental", "avant-garde": "Experimental",
    "noise": "Experimental", "industrial": "Experimental", "art rock": "Experimental",
    "noise rock": "Experimental", "avant-rock": "Experimental",
    # Dance
    "dance": "Dance", "eurodance": "Dance", "hi-nrg": "Dance",
    "dance-pop": "Dance",
    # Easy Listening
    "easy listening": "Easy Listening", "lounge": "Easy Listening",
    "adult contemporary": "Easy Listening", "soft rock": "Easy Listening",
    "smooth": "Easy Listening",
    # Instrumental
    "instrumental": "Instrumental", "post-rock": "Instrumental",
    "math rock": "Instrumental", "surf rock": "Instrumental",
    # Comedy
    "comedy": "Comedy", "novelty": "Comedy", "parody": "Comedy",
    "humor": "Comedy",
    # Spoken Word
    "spoken word": "Spoken Word", "podcast": "Spoken Word",
    "audiobook": "Spoken Word", "speech": "Spoken Word",
    # Holiday
    "christmas": "Holiday", "holiday": "Holiday", "xmas": "Holiday",
    # Kids
    "children's music": "Kids", "children": "Kids", "kids": "Kids",
    "nursery": "Kids",
}


def seed_genre_map():
    """Seed the genre_map table with default mappings if empty."""
    with get_db() as db:
        count = db.execute("SELECT COUNT(*) FROM genre_map").fetchone()[0]
        if count > 0:
            return  # Already seeded

        for raw, normalized in DEFAULT_GENRE_MAP.items():
            db.execute(
                "INSERT OR IGNORE INTO genre_map (raw_genre, normalized_genre) VALUES (?, ?)",
                (raw, normalized),
            )
        logger.info(f"Seeded genre_map with {len(DEFAULT_GENRE_MAP)} entries")


def normalize_genre(raw_tags: list[dict | str]) -> str:
    """
    Given a list of MB folksonomy tags (ordered by vote count desc),
    return the first matching normalized genre. Falls back to 'Other'.

    Accepts either list of strings or list of dicts with 'tag' key.
    """
    with get_db() as db:
        for tag_item in raw_tags:
            if isinstance(tag_item, dict):
                tag = tag_item.get("tag", "")
            else:
                tag = str(tag_item)

            normalized_key = tag.strip().lower()
            row = db.execute(
                "SELECT normalized_genre FROM genre_map WHERE raw_genre = ?",
                (normalized_key,),
            ).fetchone()
            if row:
                return row["normalized_genre"]

    return "Other"


def get_all_genres() -> list[str]:
    """Return all normalized genre categories."""
    return sorted(set(DEFAULT_GENRE_MAP.values())) + ["Other"]


def get_genre_stats() -> list[dict]:
    """Get count of tracks per normalized genre from fingerprint_results."""
    try:
        with get_db() as db:
            rows = db.execute("""
                SELECT matched_genre, COUNT(*) as count
                FROM fingerprint_results
                WHERE matched_genre IS NOT NULL
                GROUP BY matched_genre
                ORDER BY count DESC
            """).fetchall()
            return [dict(r) for r in rows]
    except Exception:
        return []
