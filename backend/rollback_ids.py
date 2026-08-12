"""Roll back specific corrections by id: rollback_ids.py <id> [<id> ...]
Flips the kill switch on (rollback rewrites tags), reverts each, re-locks."""
import sys

import correction_pass as cp
from database import get_db


def set_switch(value: str) -> None:
    with get_db() as db:
        db.execute(
            "UPDATE settings SET value=? WHERE key='identity_act_enabled'", (value,)
        )


ids = [int(x) for x in sys.argv[1:]]
if not ids:
    print("usage: rollback_ids.py <correction_id> [<correction_id> ...]")
    sys.exit(1)

try:
    set_switch("true")
    for cid in ids:
        print(f"rollback {cid}: {cp.rollback_correction(cid)}")
finally:
    try:
        set_switch("false")
    except Exception as e:  # noqa: BLE001
        print(f"!!! CRITICAL: re-lock failed ({e}) — set identity_act_enabled='false' NOW")
        raise
