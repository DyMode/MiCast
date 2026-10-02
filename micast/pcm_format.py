"""Explicit PCM contracts at source, tee and output boundaries."""

from dataclasses import dataclass

import av


@dataclass(frozen=True)
class PCMFormat:
    sample_rate: int = 44100
    channels: int = 2
    sample_bytes: int = 2

    def __post_init__(self):
        if self.sample_rate <= 0 or self.channels != 2 or self.sample_bytes != 2:
            raise ValueError("PCM must be stereo s16le at a positive sample rate")

    @property
    def frame_bytes(self) -> int:
        return self.channels * self.sample_bytes

    @property
    def bytes_per_second(self) -> int:
        return self.sample_rate * self.frame_bytes

    def bytes_for_ms(self, milliseconds: float) -> int:
        return int(self.sample_rate * milliseconds / 1000) * self.frame_bytes


class PCMResampler:
    """Streaming conversion preserving incomplete samples and resampler tails."""

    def __init__(self, source: PCMFormat, target: PCMFormat):
        self.source, self.target = source, target
        self._pending = bytearray()
        self._resampler = av.AudioResampler(format="s16", layout="stereo", rate=target.sample_rate)

    @staticmethod
    def _bytes(frames) -> bytes:
        return b"".join(bytes(f.planes[0])[: f.samples * 4] for f in frames)

    def convert(self, chunk: bytes) -> bytes:
        self._pending.extend(chunk)
        aligned = len(self._pending) // self.source.frame_bytes * self.source.frame_bytes
        if not aligned:
            return b""
        data = bytes(self._pending[:aligned])
        del self._pending[:aligned]
        if self.source == self.target:
            return data
        frame = av.AudioFrame(format="s16", layout="stereo", samples=aligned // 4)
        frame.sample_rate = self.source.sample_rate
        frame.planes[0].update(data)
        return self._bytes(self._resampler.resample(frame))

    def flush(self) -> bytes:
        if self._pending:
            raise ValueError("Incomplete PCM frame at end of source")
        return b"" if self.source == self.target else self._bytes(self._resampler.resample(None))
