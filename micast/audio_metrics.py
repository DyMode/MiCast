"""Cumulative audio-path metrics — the black box for stutter reports.

Connection-scoped counters (stream-server lag/silence/drop counts) reset every
time a speaker reconnects, so a periodic glitch can be invisible in the live
status while the listener clearly hears it. Everything here accumulates for the
process lifetime and keeps a short rolling event log, so one occurrence of a
stutter can be explained after the fact.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque

logger = logging.getLogger(__name__)

ENCODER_STALL_MS = 200.0
# A loop this late is not jitter: our single event loop is what reads every
# receiver socket, feeds every encoder and serves every speaker, so a blocked
# loop stalls ALL pipelines at the same instant — the one cause no drop counter
# can see (nothing is dropped, everything is late).
LOOP_LAG_WARN_MS = 200.0
# CPU is averaged over this window: a one-second sample of a bursty workload is
# noise, and the question is whether the box is *sustainedly* near its ceiling.
CPU_WINDOW_SECONDS = 5.0
# A gap this long between encoded frames is cadence jitter, not lost audio: the
# paced pump hands PCM over in bursts and the muxer emits one frame per period
# (flac: ~100ms), so a late frame shows up here while the downstream delay line
# absorbs it. Audio the encoder itself cost is counted by the in/out drop
# counters, not here.
# Events worth a log line: the rare, listener-visible ones. Drop counters fire
# in bursts and would flood the log.
LOGGED_EVENTS = frozenset(
    {"encoder_stall", "encoder_gap", "source_stall", "source_gap", "lag_skip", "loop_lag"}
)


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
        self.source_gap_count = 0
        self.source_gap_max_ms = 0.0
        self.silence_fill_count = 0
        self.lag_skip_count = 0
        self.lag_skip_bytes = 0
        self.lag_skip_max_ms = 0.0
        self.queue_drop_count = 0
        self.queue_drop_max_peak_ms = 0.0
        self.queue_peak_items = 0
        self.queue_peak_ms = 0.0
        self.tee_drop_count = 0
        self.tee_drops_by_entry: dict[str, int] = {}
        self.pace_sleeps = 0
        self.pace_sleep_total_ms = 0.0
        self.pace_sleep_max_ms = 0.0
        self.encoder_in_drop_count = 0
        self.encoder_out_drop_count = 0
        self.client_connect_count = 0
        self.client_reconnect_count = 0
        # Runtime health: this process's own CPU load (all encoder threads
        # included) and how late the event loop ran its own timer. Together
        # they answer "is the box, not the source, the reason it stutters".
        self.cpu_percent = 0.0
        self.cpu_cores = 0
        self.loop_lag_ms = 0.0
        self.loop_lag_max_ms = 0.0
        self._cpu_window: deque[tuple[float, float]] = deque()

    # -- recording ---------------------------------------------------------
    def record_event(
        self,
        kind: str,
        *,
        ms: float | None = None,
        detail: str | None = None,
        entry: str | None = None,
        label: str | None = None,
    ) -> None:
        """``detail`` is engineer-facing (log files, reports); ``entry`` plus
        ``label`` let the diagnostics page say the same thing in the user's
        words instead of quoting internal fields."""
        self._events.append(
            {
                "at": time.time(),
                "kind": kind,
                "ms": round(ms, 1) if ms is not None else None,
                "detail": detail,
                "entry": entry,
                "label": label,
            }
        )
        if kind in LOGGED_EVENTS:
            logger.info(
                "audio event %s%s%s",
                kind,
                f" {ms:.0f}ms" if ms is not None else "",
                f" ({detail})" if detail else "",
            )

    def note_encode(self, ms: float, entry: str | None = None) -> None:
        self.encode_count += 1
        self._encode_samples.append(ms)
        if ms > self.encode_max_ms:
            self.encode_max_ms = ms
        if ms >= ENCODER_STALL_MS:
            self.encode_stall_count += 1
            if ms > self.encode_stall_max_ms:
                self.encode_stall_max_ms = ms
            self.record_event("encoder_stall", ms=ms, entry=entry, label="编码间隔偏大")

    def note_encoder_gap(self, ms: float, expected_ms: float, entry: str | None = None) -> None:
        self.encoder_gap_count += 1
        if ms > self.encoder_gap_max_ms:
            self.encoder_gap_max_ms = ms
        self.record_event(
            "encoder_gap",
            ms=ms,
            detail=f"expected {expected_ms:.0f}ms",
            entry=entry,
            label="编码输出间隔",
        )

    def note_source_stall(self, ms: float, entry: str | None = None) -> None:
        self.source_stall_count += 1
        if ms > self.source_stall_max_ms:
            self.source_stall_max_ms = ms
        self.record_event("source_stall", ms=ms, entry=entry, label="音源停滞")

    def note_source_gap(self, ms: float, entry: str | None = None) -> None:
        """A hole between two delivered PCM chunks, shorter than a stall.

        This is the fingerprint of a bursty source: the listener hears the
        audio arrive in lumps even though the average rate is right. Counting
        it next to the encoder-side gaps separates "the sender delivers in
        bursts" from "our reading introduced the bursts".
        """
        self.source_gap_count += 1
        if ms > self.source_gap_max_ms:
            self.source_gap_max_ms = ms
        self.record_event("source_gap", ms=ms, entry=entry, label="音源空缺")

    def note_silence_fill(self) -> None:
        self.silence_fill_count += 1

    def note_lag_skip(self, bytes_: int, ms: float, entry: str | None = None) -> None:
        self.lag_skip_count += 1
        self.lag_skip_bytes += bytes_
        if ms > self.lag_skip_max_ms:
            self.lag_skip_max_ms = ms
        self.record_event("lag_skip", ms=ms, entry=entry, label="延迟线跳过")

    def note_queue_drops(self, count: int, queue_ms: float) -> None:
        self.queue_drop_count += count
        if queue_ms > self.queue_drop_max_peak_ms:
            self.queue_drop_max_peak_ms = queue_ms

    def note_queue_depth(self, items: int, ms: float) -> None:
        if items > self.queue_peak_items:
            self.queue_peak_items = items
        if ms > self.queue_peak_ms:
            self.queue_peak_ms = ms

    def note_tee_drop(self, count: int = 1, entry: str | None = None) -> None:
        self.tee_drop_count += count
        if entry:
            self.tee_drops_by_entry[entry] = self.tee_drops_by_entry.get(entry, 0) + count
        self.record_event("tee_drop", detail=f"{count} chunk(s)", entry=entry, label="PCM 分发丢弃")

    def note_pace_sleep(self, ms: float) -> None:
        """Time the pump spent holding itself back to realtime.

        A paced pump sleeps between source reads, so an inter-read interval on
        its own cannot say whether the SOURCE paused or WE chunked it. Counting
        the sleep separately is what separates the two.
        """
        self.pace_sleeps += 1
        self.pace_sleep_total_ms += ms
        if ms > self.pace_sleep_max_ms:
            self.pace_sleep_max_ms = ms

    def note_runtime_sample(self, *, wall: float, cpu: float, loop_lag_ms: float) -> None:
        """One monitor tick: process CPU time (all threads) plus loop lag."""
        self._cpu_window.append((wall, cpu))
        while (
            len(self._cpu_window) > 1
            and wall - self._cpu_window[0][0] > CPU_WINDOW_SECONDS
        ):
            self._cpu_window.popleft()
        span = wall - self._cpu_window[0][0]
        if span > 0:
            used = cpu - self._cpu_window[0][1]
            self.cpu_percent = max(0.0, used / span * 100.0)
        self.loop_lag_ms = max(0.0, loop_lag_ms)
        if self.loop_lag_ms > self.loop_lag_max_ms:
            self.loop_lag_max_ms = self.loop_lag_ms
        if self.loop_lag_ms >= LOOP_LAG_WARN_MS:
            self.record_event(
                "loop_lag", ms=self.loop_lag_ms, label="事件循环阻塞"
            )

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
            self.record_event(
                "client_reconnect", detail=device_id, entry=device_id, label="音箱重连"
            )

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
                "gaps": self.source_gap_count,
                "gap_max_ms": round(self.source_gap_max_ms, 1),
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
                "tee_by_entry": dict(self.tee_drops_by_entry),
                "encoder_in": self.encoder_in_drop_count,
                "encoder_out": self.encoder_out_drop_count,
            },
            "pace": {
                "sleeps": self.pace_sleeps,
                "total_ms": round(self.pace_sleep_total_ms, 1),
                "max_ms": round(self.pace_sleep_max_ms, 1),
            },
            "runtime": {
                "cpu_percent": round(self.cpu_percent, 1),
                "cores": self.cpu_cores,
                "loop_lag_ms": round(self.loop_lag_ms, 1),
                "loop_lag_max_ms": round(self.loop_lag_max_ms, 1),
            },
            "events": list(self._events),
        }

    def reset(self) -> None:
        self.__init__()


async def run_runtime_monitor(interval: float = 1.0) -> None:
    """Sample process CPU and event-loop lag once per ``interval``.

    Both numbers exist to answer one question the drop counters cannot: when a
    speaker stutters with every counter at zero, is OUR side simply too busy or
    too blocked to feed it? Loop lag is the overshoot of a plain timer — the
    single number that shows a blocked event loop (which stalls every pipeline
    at the same instant); CPU is this process's own usage across all its
    encoder threads, so a box pinned at one core is visible even though nothing
    was dropped.
    """
    metrics.cpu_cores = os.cpu_count() or 1
    previous_wall = time.monotonic()
    while True:
        await asyncio.sleep(interval)
        now_wall = time.monotonic()
        # The CPU window lives in the metrics object: it is a 5s average, so
        # this loop does not need to remember its own previous sample.
        metrics.note_runtime_sample(
            wall=now_wall,
            cpu=time.process_time(),
            loop_lag_ms=(now_wall - previous_wall - interval) * 1000.0,
        )
        previous_wall = now_wall


metrics = AudioMetrics()
