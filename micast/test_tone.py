"""Built-in test tone for speaker diagnostics (no public-internet dependency)."""

import math
import struct

SAMPLE_RATE = 44100
# A short, recognizable arpeggio (C5 E5 G5 C6) repeated, with per-note decay.
_NOTES = [(523.25, 0.4), (659.25, 0.4), (783.99, 0.4), (1046.50, 0.8)]
_REPEATS = 4
_AMPLITUDE = 0.4

_cache: bytes | None = None


def test_tone_wav() -> bytes:
    """Synthesize (once) a finite stereo 16-bit WAV test tone."""
    global _cache
    if _cache is not None:
        return _cache

    frames = bytearray()
    for _ in range(_REPEATS):
        for freq, seconds in _NOTES:
            count = int(SAMPLE_RATE * seconds)
            for i in range(count):
                t = i / SAMPLE_RATE
                envelope = math.exp(-3.0 * t / seconds)  # gentle pluck decay
                sample = int(_AMPLITUDE * 32767 * envelope * math.sin(2 * math.pi * freq * t))
                frames.extend(struct.pack("<hh", sample, sample))

    data = bytes(frames)
    byte_rate = SAMPLE_RATE * 2 * 2
    header = (
        b"RIFF"
        + struct.pack("<I", 36 + len(data))
        + b"WAVE"
        + b"fmt "
        + struct.pack("<IHHIIHH", 16, 1, 2, SAMPLE_RATE, byte_rate, 4, 16)
        + b"data"
        + struct.pack("<I", len(data))
    )
    _cache = header + data
    return _cache


_silence_cache: bytes | None = None


def silent_probe_wav(seconds: float = 8.0) -> bytes:
    """A WAV nobody can hear, for probing what a speaker's decoder accepts.

    ±1 LSB (−90 dBFS), not digital zeros: a constant-zero signal is what every
    codec is best at compressing, and an 8-second silent FLAC shrinks to a few
    kilobytes — small enough that a speaker finishes reading it before the probe
    can sample the pull, which would look like "it gave up". Alternating ±1
    keeps every format at its natural size while staying inaudible.
    """
    global _silence_cache
    if _silence_cache is None:
        frames = bytearray()
        for index in range(int(SAMPLE_RATE * seconds)):
            sample = 1 if index % 2 else -1
            frames.extend(struct.pack("<hh", sample, sample))
        data = bytes(frames)
        byte_rate = SAMPLE_RATE * 2 * 2
        header = (
            b"RIFF"
            + struct.pack("<I", 36 + len(data))
            + b"WAVE"
            + b"fmt "
            + struct.pack("<IHHIIHH", 16, 1, 2, SAMPLE_RATE, byte_rate, 4, 16)
            + b"data"
            + struct.pack("<I", len(data))
        )
        _silence_cache = header + data
    return _silence_cache
