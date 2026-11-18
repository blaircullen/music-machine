#!/usr/bin/env python3
"""
mm-daily-thaw.py — daily FLAC-upgrade drip report for Music Machine.

Runs INSIDE the music-machine container each morning via cron:
    TELEGRAM_BOT_TOKEN="$(cat /home/olares/.telegram_token)" docker exec -i -e PYTHONPATH=/app \
        -e TELEGRAM_BOT_TOKEN music-machine python3 - < mm-daily-thaw.py
(-e NAME with no value copies it from the caller's env, so the token is not in the process args.)

The Gary bot token is NOT in this file. It comes from env TELEGRAM_BOT_TOKEN, which the
cron line must pass into the container with -e (the script runs from stdin inside the
container and cannot read host files). Beast keeps that token in /home/olares/.telegram_token.

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
import urllib.error
import os

import database
import upgrade_thaw

TG_CHAT = "8375486668"


def _token():
    tok = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not tok:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is not set inside the container; pass it with "
            "TELEGRAM_BOT_TOKEN=\"$(cat /home/olares/.telegram_token)\" docker exec -e TELEGRAM_BOT_TOKEN ...")
    return tok


def telegram(text):
    body = urllib.parse.urlencode({"chat_id": TG_CHAT, "text": text}).encode()
    req = urllib.request.Request(
        "https://api.telegram.org/bot%s/sendMessage" % _token(), data=body, method="POST")
    try:
        urllib.request.urlopen(req, timeout=30).read()
    except urllib.error.HTTPError as e:
        raise RuntimeError("Telegram sendMessage failed: HTTP %d: %s"
                           % (e.code, e.read().decode(errors="replace")[:300])) from None


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
        print("telegram FAILED: %s: %s" % (type(exc).__name__, exc))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
