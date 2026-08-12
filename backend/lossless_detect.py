#!/usr/bin/env python3
"""Standalone spectral cutoff detector for conservative FLAC triage."""

from __future__ import annotations

import argparse
import json
import math
import subprocess
from pathlib import Path
from typing import Any

import numpy as np
from scipy import signal


METHOD_VERSION = 1

FLOOR_DB = 50.0
ABS_NOISE_FLOOR_DB = -120.0
SILENCE_RMS = 1e-4
MIN_AUDIO_SECONDS = 1.0
MIN_SPECTRUM_SAMPLES = 1024

SHARP_THRESH = 0.60
SHELF_DB_MIN = 18.0
SHARP_SLOPE_DB_PER_KHZ = 18.0

LOSSLESS_MIN_CUTOFF_HZ = 21000.0
MIN_TRANSCODE_CUTOFF_HZ = 13000.0
MAX_TRANSCODE_CUTOFF_HZ = 20600.0
HIRES_UPSAMPLE_CUTOFF_HZ = 22500.0
HIRES_LOSSLESS_MIN_CUTOFF_HZ = 24000.0


def _run_json(cmd: list[str]) -> dict[str, Any]:
    proc = subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return json.loads(proc.stdout.decode("utf-8"))


def probe(path: str | Path) -> dict[str, Any]:
    """Return basic audio metadata from ffprobe."""
    data = _run_json(
        [
            "ffprobe",
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_streams",
            "-show_format",
            str(path),
        ]
    )
    audio_stream = None
    for stream in data.get("streams", []):
        if stream.get("codec_type") == "audio":
            audio_stream = stream
            break
    if audio_stream is None:
        raise ValueError("no audio stream found")

    sample_rate = int(audio_stream.get("sample_rate") or 0)
    channels = int(audio_stream.get("channels") or 0)
    duration_raw = audio_stream.get("duration") or data.get("format", {}).get("duration") or 0
    duration = float(duration_raw or 0.0)
    return {
        "sample_rate": sample_rate,
        "channels": channels,
        "duration": duration,
    }


def _window_offsets(duration: float, n_windows: int, win_sec: float) -> list[float]:
    if duration <= 0 or n_windows <= 0 or win_sec <= 0:
        return []
    if duration < 5.0:
        return [0.0]
    if duration <= win_sec:
        return [0.0]

    region_start = max(0.0, duration * 0.10)
    region_end = min(max(0.0, duration - win_sec), duration * 0.90 - win_sec)
    if region_end < region_start:
        return [max(0.0, min(duration - win_sec, (duration - win_sec) / 2.0))]

    max_non_overlap = int(math.floor((region_end - region_start) / win_sec)) + 1
    count = max(1, min(n_windows, max_non_overlap))
    if count == 1:
        return [float((region_start + region_end) / 2.0)]
    return [float(x) for x in np.linspace(region_start, region_end, count)]


def _decode_windows(
    path: str | Path,
    sample_rate: int,
    duration: float,
    n_windows: int = 8,
    win_sec: float = 4.0,
) -> list[np.ndarray]:
    """Decode mono float32 windows with ffmpeg, skipping near-silent audio."""
    del sample_rate
    windows: list[np.ndarray] = []
    for offset in _window_offsets(duration, n_windows, win_sec):
        cmd = [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-ss",
            f"{offset:.6f}",
            "-i",
            str(path),
            "-t",
            f"{win_sec:.6f}",
            "-ac",
            "1",
            "-f",
            "f32le",
            "-acodec",
            "pcm_f32le",
            "-",
        ]
        proc = subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if not proc.stdout:
            continue
        usable = len(proc.stdout) - (len(proc.stdout) % 4)
        if usable <= 0:
            continue
        audio = np.frombuffer(proc.stdout[:usable], dtype="<f4").astype(np.float32, copy=False)
        if audio.size == 0:
            continue
        rms = float(np.sqrt(np.mean(np.square(audio, dtype=np.float64))))
        if rms < SILENCE_RMS:
            continue
        windows.append(audio)
    return windows


def _avg_spectrum_db(windows: list[np.ndarray], sample_rate: int) -> tuple[np.ndarray, np.ndarray]:
    """Average Welch spectra on a common frequency grid and return dB power."""
    usable = [np.asarray(w, dtype=np.float32) for w in windows if len(w) >= MIN_SPECTRUM_SAMPLES]
    if not usable:
        raise ValueError("insufficient audio for spectrum")

    nperseg = min(8192, max(len(w) for w in usable))
    spectra = []
    freqs = None
    for window in usable:
        if len(window) < nperseg:
            padded = np.zeros(nperseg, dtype=np.float32)
            padded[: len(window)] = window
            window = padded
        f, psd = signal.welch(
            window,
            fs=sample_rate,
            window="hann",
            nperseg=nperseg,
            scaling="spectrum",
        )
        if freqs is None:
            freqs = f
        spectra.append(psd)
    if freqs is None or not spectra:
        raise ValueError("insufficient audio for spectrum")

    avg_psd = np.mean(np.vstack(spectra), axis=0)
    power_db = 10.0 * np.log10(avg_psd + np.finfo(np.float64).tiny)
    return freqs, power_db


def _median_band(freqs: np.ndarray, power_db: np.ndarray, low: float, high: float) -> float:
    mask = (freqs >= low) & (freqs <= high)
    if not np.any(mask):
        return float("nan")
    return float(np.median(power_db[mask]))


def _find_cutoff(freqs: np.ndarray, power_db: np.ndarray) -> tuple[float, float, float]:
    """Find the highest meaningful spectral cutoff and edge shape."""
    if len(freqs) == 0 or len(power_db) == 0:
        return 0.0, 0.0, 0.0

    smoothed = signal.medfilt(power_db, kernel_size=9 if len(power_db) >= 9 else 1)
    ref = _median_band(freqs, smoothed, 1000.0, 6000.0)
    if not np.isfinite(ref):
        ref = float(np.median(smoothed))

    threshold = max(ref - FLOOR_DB, ABS_NOISE_FLOOR_DB)
    valid = smoothed >= threshold
    cutoff_hz = 0.0
    for idx in range(len(freqs) - 1, -1, -1):
        if valid[idx]:
            cutoff_hz = float(freqs[idx])
            break

    if cutoff_hz <= 0:
        return 0.0, 0.0, 0.0

    below_db = _median_band(freqs, smoothed, max(0.0, cutoff_hz - 1200.0), max(0.0, cutoff_hz - 250.0))
    above_db = _median_band(freqs, smoothed, cutoff_hz + 250.0, cutoff_hz + 1600.0)
    if not np.isfinite(below_db):
        below_db = _median_band(freqs, smoothed, max(0.0, cutoff_hz - 1800.0), cutoff_hz)
    if not np.isfinite(above_db):
        above_db = float(np.min(smoothed[-max(1, min(32, len(smoothed))) :]))

    shelf_db = max(0.0, float(below_db - above_db))
    edge_width_khz = max(0.25, 1.35)
    slope = shelf_db / edge_width_khz
    sharpness = max(0.0, min(1.0, slope / SHARP_SLOPE_DB_PER_KHZ))
    return cutoff_hz, shelf_db, sharpness


def _source_guess_for_cutoff(cutoff_hz: float) -> str | None:
    if cutoff_hz < 16800.0:
        return "mp3_128"
    if cutoff_hz < 18600.0:
        return "mp3_192"
    if cutoff_hz < 19300.0:
        return "mp3_256"
    if cutoff_hz <= 20600.0:
        return "mp3_320"
    return None


def _confidence(sharpness: float, shelf_db: float, base: float = 0.0) -> float:
    shelf_score = min(1.0, max(0.0, shelf_db / 36.0))
    return round(max(base, min(1.0, sharpness * 0.65 + shelf_score * 0.35)), 3)


def classify(
    probe_d: dict[str, Any],
    cutoff_hz: float,
    shelf_db: float,
    sharpness: float,
    n_windows_used: int,
) -> dict[str, Any]:
    sample_rate = int(probe_d.get("sample_rate") or 0)
    channels = int(probe_d.get("channels") or 0)
    duration = float(probe_d.get("duration") or 0.0)
    nyq = sample_rate / 2.0 if sample_rate else 0.0

    verdict = "suspect"
    confidence = 0.2
    source_guess = None

    if n_windows_used == 0 or duration < MIN_AUDIO_SECONDS or cutoff_hz <= 0:
        verdict = "insufficient_audio"
        confidence = 0.0
    elif sample_rate <= 32000:
        verdict = "low_rate"
        confidence = 0.0
    elif sample_rate >= 88200:
        if cutoff_hz <= HIRES_UPSAMPLE_CUTOFF_HZ:
            verdict = "suspect"
            confidence = 0.55
            source_guess = "upsampled_possible"
        elif cutoff_hz >= HIRES_LOSSLESS_MIN_CUTOFF_HZ:
            verdict = "lossless"
            confidence = 0.85
        else:
            verdict = "suspect"
            confidence = 0.4
    elif cutoff_hz >= LOSSLESS_MIN_CUTOFF_HZ or (nyq > 0 and cutoff_hz >= 0.95 * nyq):
        verdict = "lossless"
        confidence = 0.9
    else:
        if (
            MIN_TRANSCODE_CUTOFF_HZ <= cutoff_hz <= MAX_TRANSCODE_CUTOFF_HZ
            and sharpness >= SHARP_THRESH
            and shelf_db >= SHELF_DB_MIN
        ):
            verdict = "transcode"
            source_guess = _source_guess_for_cutoff(cutoff_hz)
            confidence = _confidence(sharpness, shelf_db, base=0.65)
        else:
            source_guess = _source_guess_for_cutoff(cutoff_hz)
            verdict = "suspect"
            confidence = 0.25 if source_guess else 0.15

    return {
        "sample_rate": sample_rate,
        "channels": channels,
        "duration": duration,
        "nyquist_hz": nyq,
        "cutoff_hz": float(cutoff_hz),
        "shelf_db": float(shelf_db),
        "sharpness": float(sharpness),
        "verdict": verdict,
        "confidence": float(confidence),
        "source_guess": source_guess,
        "n_windows_used": int(n_windows_used),
        "method_version": METHOD_VERSION,
    }


def analyze_flac(path: str | Path) -> dict[str, Any]:
    try:
        probe_d = probe(path)
        sample_rate = int(probe_d.get("sample_rate") or 0)
        duration = float(probe_d.get("duration") or 0.0)
        windows = _decode_windows(path, sample_rate, duration)
        if not windows:
            return classify(probe_d, 0.0, 0.0, 0.0, 0)
        freqs, power_db = _avg_spectrum_db(windows, sample_rate)
        cutoff_hz, shelf_db, sharpness = _find_cutoff(freqs, power_db)
        return classify(probe_d, cutoff_hz, shelf_db, sharpness, len(windows))
    except Exception as exc:
        return {
            "verdict": "error",
            "error": str(exc),
            "method_version": METHOD_VERSION,
        }


def render_spectrogram(path: str | Path, out_png: str | Path) -> str:
    cmd = [
        "ffmpeg",
        "-nostdin",
        "-v",
        "error",
        "-y",
        "-i",
        str(path),
        "-lavfi",
        "showspectrumpic=s=640x480:legend=1",
        str(out_png),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return str(out_png)


def main() -> int:
    parser = argparse.ArgumentParser(description="Conservative lossless/transcode spectral detector")
    parser.add_argument("file")
    parser.add_argument("--spectrogram")
    args = parser.parse_args()

    result = analyze_flac(args.file)
    if args.spectrogram:
        render_spectrogram(args.file, args.spectrogram)
        result["spectrogram"] = args.spectrogram
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
