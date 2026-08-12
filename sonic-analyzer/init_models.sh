#!/bin/bash
# init_models.sh — verify Essentia CLI is available
# High-level TF models (mood/genre/voice) are Phase 2.
# V1 uses acoustic features only (BPM, MFCC, HPCP, spectral, rhythm).

set -e

echo "[init] Checking essentia_streaming_extractor_music..."
if ! command -v essentia_streaming_extractor_music &>/dev/null; then
    echo "[init] ERROR: essentia_streaming_extractor_music not found in PATH"
    exit 1
fi

echo "[init] Essentia extractor found: $(which essentia_streaming_extractor_music)"
echo "[init] Models directory: ${MODELS_DIR:-/models}"
echo "[init] Ready."
