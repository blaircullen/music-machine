"""Generic artist-only apply driver: apply_batch.py <ids_file>.

Same hardened kill-switch discipline as apply_batch1.py (flip on INSIDE try,
always re-lock in finally), applied to whatever ids file is passed.
"""
import sys

import correction_pass as cp
from database import get_db
from mutagen import File as MutagenFile

IDS_FILE = sys.argv[1] if len(sys.argv) > 1 else "/app/batch2_ids.txt"
FIELDS = ("artist",)


def set_switch(value: str) -> None:
    with get_db() as db:
        db.execute(
            "UPDATE settings SET value=? WHERE key='identity_act_enabled'", (value,)
        )


def switch_value() -> str:
    with get_db() as db:
        row = db.execute(
            "SELECT value FROM settings WHERE key='identity_act_enabled'"
        ).fetchone()
    return row[0] if row else "(missing)"


ids = [int(x) for x in open(IDS_FILE).read().split() if x.strip()]
print(f"ids file: {IDS_FILE}  batch size: {len(ids)}  fields={FIELDS}")

try:
    set_switch("true")
    res = cp.run(mode="review", dry_run=False, track_ids=ids, fields=FIELDS)
finally:
    try:
        set_switch("false")
    except Exception as e:  # noqa: BLE001
        print(f"!!! CRITICAL: failed to re-lock kill switch ({e}). "
              f"Manually set settings.identity_act_enabled='false' NOW.")
        raise

print("kill switch now:", switch_value())
print("summary:", res.summary())
if res.errors:
    print("first errors:", res.errors[:10])

with get_db() as db:
    print("on-disk spot-check (3 most-recent applied rows):")
    rows = db.execute(
        "SELECT track_id, new_artist FROM identity_corrections "
        "WHERE status='applied' ORDER BY id DESC LIMIT 3"
    ).fetchall()
    for r in rows:
        fp = db.execute(
            "SELECT file_path FROM tracks WHERE id=?", (r["track_id"],)
        ).fetchone()["file_path"]
        audio = MutagenFile(fp)
        on_disk = audio.get("artist") if audio is not None else None
        print(f"  track {r['track_id']}: disk={on_disk}  expected={r['new_artist']!r}")
