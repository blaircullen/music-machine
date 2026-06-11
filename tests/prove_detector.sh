#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 <path-to-a-genuine-flac>" >&2
  exit 2
fi

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DETECTOR="$ROOT_DIR/backend/lossless_detect.py"
GENUINE_SRC="$1"

if [[ ! -f "$GENUINE_SRC" ]]; then
  echo "Input file not found: $GENUINE_SRC" >&2
  exit 2
fi

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

GENUINE="$TMP_DIR/genuine.flac"
cp "$GENUINE_SRC" "$GENUINE"

ffmpeg -nostdin -v error -y -i "$GENUINE" -b:a 128k "$TMP_DIR/t128.mp3"
ffmpeg -nostdin -v error -y -i "$TMP_DIR/t128.mp3" "$TMP_DIR/t128.flac"
ffmpeg -nostdin -v error -y -i "$GENUINE" -b:a 320k "$TMP_DIR/t320.mp3"
ffmpeg -nostdin -v error -y -i "$TMP_DIR/t320.mp3" "$TMP_DIR/t320.flac"
ffmpeg -nostdin -v error -y -i "$GENUINE" -q:a 0 "$TMP_DIR/v0.mp3"
ffmpeg -nostdin -v error -y -i "$TMP_DIR/v0.mp3" "$TMP_DIR/v0.flac"

run_detector() {
  local label="$1"
  local file="$2"
  local out="$TMP_DIR/$label.json"
  echo "===== $label ====="
  python3 "$DETECTOR" "$file" | tee "$out"
}

run_detector genuine "$GENUINE"
run_detector t128 "$TMP_DIR/t128.flac"
run_detector t320 "$TMP_DIR/t320.flac"
run_detector v0 "$TMP_DIR/v0.flac"

python3 - "$TMP_DIR" <<'PY'
import json
import pathlib
import sys

tmp = pathlib.Path(sys.argv[1])

def load(name):
    with (tmp / f"{name}.json").open() as fh:
        return json.load(fh)

genuine = load("genuine")
t128 = load("t128")
t320 = load("t320")

failures = []

genuine_ok = genuine.get("verdict") == "lossless"
print(f"ASSERT genuine.verdict==lossless: {'PASS' if genuine_ok else 'FAIL'}")
if not genuine_ok:
    failures.append("genuine verdict was not lossless")

t128_cutoff = float(t128.get("cutoff_hz") or 0.0)
t128_ok = (
    t128.get("verdict") == "transcode"
    and t128.get("source_guess") == "mp3_128"
    and 15000.0 <= t128_cutoff <= 17000.0
)
print(
    "ASSERT t128.verdict==transcode AND source_guess==mp3_128 "
    f"AND cutoff 15k-17k: {'PASS' if t128_ok else 'FAIL'}"
)
if not t128_ok:
    failures.append("t128 did not classify as mp3_128 transcode near 16k")

t320_cutoff = float(t320.get("cutoff_hz") or 0.0)
t320_ok = t320.get("verdict") in ("transcode", "suspect") and 19000.0 <= t320_cutoff <= 21000.0
print(
    "ASSERT t320.verdict in (transcode,suspect) AND cutoff 19k-21k: "
    f"{'PASS' if t320_ok else 'FAIL'}"
)

if failures:
    print("HARD ASSERTIONS FAILED:")
    for failure in failures:
        print(f"- {failure}")
    sys.exit(1)
PY
