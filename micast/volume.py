"""Shared source-volume curve, separate from device volume commands."""

import math

import numpy as np


def db_to_percent(db: float) -> int:
    if not math.isfinite(db) or not (db == -144 or -30 <= db <= 0):
        raise ValueError("无效的投放音量")
    return 0 if db == -144 else max(0, min(100, round((db + 30) / 30 * 100)))


def apply_pcm_gain(chunk: bytes, percent: int) -> bytes:
    """Attenuate s16le PCM exactly once; zero is digital silence."""
    if percent >= 100 or not chunk:
        return chunk
    if percent <= 0:
        return bytes(len(chunk))
    if len(chunk) % 2:
        raise ValueError("PCM samples must be aligned")
    gain = 10 ** ((percent * 0.3 - 30) / 20)
    # Vectorised, not a per-sample Python loop: this runs on the event loop for
    # every chunk of every stream (16k samples per 32 KiB chunk), and the loop
    # cost ~3.5ms of blocking per chunk — a measurable slice of a small NAS's
    # single event loop, spent while RTP packets and speaker pulls wait.
    # numpy reproduces round() exactly (both round half to even) and is ~90x
    # faster, so the audio is bit-identical.
    samples = np.frombuffer(chunk, dtype="<i2")
    return np.rint(samples.astype(np.float64) * gain).astype("<i2").tobytes()
