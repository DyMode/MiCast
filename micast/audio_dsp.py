"""Shared sound character for HTTP encoders and RTP PCM output."""

from fractions import Fraction

import av

from micast.audio_encoder import _build_filter_graph, firequalizer_available
from micast.curve_fit import (
    add_curve,
    equalizer_chain,
    firequalizer_args,
    gain_table,
    loudness_curve,
)
from micast.pcm_format import PCMFormat


def build_audio_filter(curve=None, loudness=False, level=100, channel=None, gain_db=0):
    parts = []
    if loudness:
        curve = add_curve(curve or [], loudness_curve(level))
    if curve:
        table = gain_table(curve)
        if firequalizer_available():
            parts.append(("firequalizer", firequalizer_args(table)))
        else:
            for link in equalizer_chain(table):
                name, _, args = link.partition("=")
                parts.append((name, args))
    if channel in ("left", "right"):
        side = "FL" if channel == "left" else "FR"
        parts.append(("pan", f"stereo|c0={side}|c1={side}"))
    if gain_db:
        parts.append(("volume", f"{gain_db}dB"))
    return parts or None


class PCMProcessor:
    def __init__(self, source: PCMFormat, target: PCMFormat, filters=None):
        self.source = source
        self.graph = _build_filter_graph(filters, source.sample_rate, target.sample_rate)
        self.pending = bytearray()
        self.samples = 0

    def _drain(self):
        chunks = []
        while True:
            try:
                frame = self.graph.pull()
            except (av.error.BlockingIOError, av.error.EOFError):
                return b"".join(chunks)
            chunks.append(bytes(frame.planes[0])[: frame.samples * 4])

    def convert(self, data):
        self.pending.extend(data)
        size = len(self.pending) // 4 * 4
        if not size:
            return b""
        frame = av.AudioFrame(format="s16", layout="stereo", samples=size // 4)
        frame.sample_rate = self.source.sample_rate
        frame.time_base = Fraction(1, self.source.sample_rate)
        frame.pts = self.samples
        self.samples += frame.samples
        frame.planes[0].update(bytes(self.pending[:size]))
        del self.pending[:size]
        self.graph.push(frame)
        return self._drain()

    def flush(self):
        if self.pending:
            raise ValueError("Incomplete PCM frame")
        self.graph.push(None)
        return self._drain()
