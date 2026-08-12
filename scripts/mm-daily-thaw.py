#!/usr/bin/env python3
"""
mm-daily-thaw.py — daily FLAC-upgrade drip report for Music Machine.

Runs INSIDE the music-machine container each morning via cron:
    docker exec -i -e PYTHONPATH=/app music-machine python3 - < mm-daily-thaw.py

The one-time bulk pass already requested every findable album in Lidarr (monitored +
usenet-searched); Lidarr/SABnzbd import them over days. This job CATCHES those imports:
  1. polls Lidarr and advances each landed track in upgrade_queue to 'found',
  2. counts how many tracks were upgraded in the previous 24 hours (status='found',
     updated_at within the window — both CURRENT_TIMESTAMP/UTC, so the window is correct),
  3. Telegrams Blair a one-line summary every morning (heartbeat, even on a zero day).

Safe by construction: poll only advances COMPLETED downloads. It never thaws, never
triggers new downloads, never trashes. upgrade_paused is left untouched.
"""

import json
import datetime
import urllib.request
import urllib.parse

import database
import upgrade_thaw

TG_TOKEN = "8367682905:AAG2dW5-YzZh05eHOwyXcwU-qWBZshLs8oQ"  # Gary bot (Claude Code ops)
TG_CHAT = "8375486668"


def telegram(text):
    body = urllib.parse.urlencode({"chat_id": TG_CHAT, "text": text}).encode()
    req = urllib.request.Request(
        "https://api.telegram.org/bot%s/sendMessage" % TG_TOKEN, data=body, method="POST")
    urllib.request.urlopen(req, timeout=30).read()


def main():
    poll = upgrade_thaw.poll_thawed_upgrades()

    with database.get_db() as db:
        found_24h = db.execute(
            "SELECT COUNT(*) FROM upgrade_queue WHERE status='found' "
            "AND updated_at >= datetime('now','-24 hours')").fetchone()[0]
        found_total = db.execute(
            "SELECT COUNT(*) FROM upgrade_queue WHERE status='found'").fetchone()[0]

    st = upgrade_thaw.thaw_status()
    au = st.get("album_upgrades", {})
    searching = au.get("searching", 0) + au.get("inflight", 0)

    msg = ("\U0001F3B5 Music Machine — FLAC upgrade drip (%s)\n"
           "• Upgraded last 24h: %d tracks\n"
           "• Still searching: %d albums\n"
           "• Total upgraded via usenet: %d tracks"
           % (datetime.date.today().strftime("%a %b %d"),
              found_24h, searching, found_total))
    print(msg)
    print("poll:", json.dumps(poll))

    try:
        telegram(msg)
        print("telegram sent")
    except Exception as exc:  # noqa: BLE001
        print("telegram FAILED:", exc)


if __name__ == "__main__":
    main()
