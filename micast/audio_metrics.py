"""Cumulative audio-path metrics — the black box for stutter reports.

Connection-scoped counters (stream-server lag/silence/drop counts) reset every
time a speaker reconnects, so a periodic glitch can be invisible in the live
status while the listener clearly hears it. Everything here accumulates for the
process lifetime and keeps a short rolling event log, so one occurrence of a
stutter can be explained after the fact.
"""

from __future__ import annotations

import logging
import time
from collections import deque

logger = logging.getLogger(__name__)

ENCODER_STALL_MS = 200.0
# Events worth a log line: the rare, listener-visible ones. Drop counters fire
# in bursts and would flood the log.
LOGGED_EVENTS = frozenset({"encoder_stall", "encoder_gap", "source_stall", "lag_skip"})


def _percentile(samples: list[float], ratio: float) -> float:
    if not samples:
        return 0.0
    ordered = sorted(samples)
    index = min(len(ordered) - 1, max(0, round(ratio * (len(ordered) - 1))))
    return ordered[index]


class AudioMetrics:
    """Process-wide audio path counters and a rolling event log."""

    def __init__(self, max_events: int = 30, sample_window: int = 240):
        self._events: deque[dict] = deque(maxlen=max_events)
        self._encode_samples: deque[float] = deque(maxlen=sample_window)
        self.encode_count = 0
        self.encode_stall_count = 0
        self.encode_max_ms = 0.0
        self.encode_stall_max_ms = 0.0
        self.encoder_gap_count = 0
        self.encoder_gap_max_ms = 0.0
        self.source_stall_count = 0
        self.source_stall_max_ms = 0.0
        self.silence_fill_count = 0
        self.lag_skip_count = 0
        self.lag_skip_bytes = 0
        self.lag_skip_max_ms = 0.0
        self.queue_drop_count = 0
        self.queue_drop_max_peak_ms = 0.0
        self.queue_peak_items = 0
        self.queue_peak_ms = 0.0
        self.tee_drop_count = 0
        self.encoder_in_drop_count = 0
        self.encoder_out_drop_count = 0
        self.client_connect_count = 0
        self.client_reconnect_count = 0

    # -- recording ---------------------------------------------------------
    def record_event(
        self, kind: str, *, ms: float | None = None, detail: str | None = None
    ) -> None:
        self._events.append(
            {
                "at": time.time(),
                "kind": kind,
                "ms": round(ms, 1) if ms is not None else None,
                "detail": detail,
            }
        )
        if kind in LOGGED_EVENTS:
            logger.info(
                "audio event %s%s%s",
                kind,
                f" {ms:.0f}ms" if ms is not None else "",
                f" ({detail})" if detail else "",
            )

    def note_encode(self, ms: float) -> None:
        self.encode_count += 1
        self._encode_samples.append(ms)
        if ms > self.encode_max_ms:
            self.encode_max_ms = ms
        if ms >= ENCODER_STALL_MS:
            self.encode_stall_count += 1
            if ms > self.encode_stall_max_ms:
                self.encode_stall_max_ms = ms
            self.record_event("encoder_stall", ms=ms)

    def note_encoder_gap(self, ms: float, expected_ms: float) -> None:
        self.encoder_gap_count += 1
        if ms > self.encoder_gap_max_ms:
            self.encoder_gap_max_ms = ms
        self.record_event(
            "encoder_gap",
            ms=ms,
            detail=f"expected {expected_ms:.0f}ms",
        )

    def note_source_stall(self, ms: float) -> None:
        self.source_stall_count += 1
        if ms > self.source_stall_max_ms:
            self.source_stall_max_ms = ms
        self.record_event("source_stall", ms=ms)

    def note_silence_fill(self) -> None:
        self.silence_fill_count += 1

    def note_lag_skip(self, bytes_: int, ms: float) -> None:
        self.lag_skip_count += 1
        self.lag_skip_bytes += bytes_
        if ms > self.lag_skip_max_ms:
            self.lag_skip_max_ms = ms
        self.record_event("lag_skip", ms=ms)

    def note_queue_drops(self, count: int, queue_ms: float) -> None:
        self.queue_drop_count += count
        if queue_ms > self.queue_drop_max_peak_ms:
            self.queue_drop_max_peak_ms = queue_ms

    def note_queue_depth(self, items: int, ms: float) -> None:
        if items > self.queue_peak_items:
            self.queue_peak_items = items
        if ms > self.queue_peak_ms:
            self.queue_peak_ms = ms

    def note_tee_drop(self, count: int = 1) -> None:
        self.tee_drop_count += count
        self.record_event("tee_drop", detail=f"{count} chunk(s)")

    def note_encoder_drop(self, layer: str, count: int = 1) -> None:
        if layer == "in":
            self.encoder_in_drop_count += count
        else:
            self.encoder_out_drop_count += count
        self.record_event("encoder_drop", detail=f"{layer} x{count}")

    def note_client_connect(self, device_id: str, replacing: bool = False) -> None:
        self.client_connect_count += 1
        if replacing:
            self.client_reconnect_count += 1
            self.record_event("client_reconnect", detail=device_id)

    # -- reporting ---------------------------------------------------------
    def snapshot(self) -> dict:
        samples = list(self._encode_samples)
        return {
            "encode": {
                "chunks": self.encode_count,
                "p50_ms": round(_percentile(samples, 0.5), 1),
                "p95_ms": round(_percentile(samples, 0.95), 1),
                "max_ms": round(self.encode_max_ms, 1),
                "stalls": self.encode_stall_count,
                "stall_max_ms": round(self.encode_stall_max_ms, 1),
            },
            "encoder_gap": {
                "count": self.encoder_gap_count,
                "max_ms": round(self.encoder_gap_max_ms, 1),
            },
            "source": {
                "stalls": self.source_stall_count,
                "stall_max_ms": round(self.source_stall_max_ms, 1),
                "silence_fills": self.silence_fill_count,
            },
            "client": {
                "connects": self.client_connect_count,
                "reconnects": self.client_reconnect_count,
                "lag_skips": self.lag_skip_count,
                "lag_skip_ms_max": round(self.lag_skip_max_ms, 1),
                "lag_skip_bytes": self.lag_skip_bytes,
                "queue_drops": self.queue_drop_count,
                "queue_peak_items": self.queue_peak_items,
                "queue_peak_ms": round(self.queue_peak_ms, 1),
            },
            "drops": {
                "tee": self.tee_drop_count,
                "encoder_in": self.encoder_in_drop_count,
                "encoder_out": self.encoder_out_drop_count,
            },
            "events": list(self._events),
        }

    def reset(self) -> None:
        self.__init__()


metrics = AudioMetrics()
