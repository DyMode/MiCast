"""Bounded in-memory application logs for the diagnostics UI.

Nothing is written to disk: the buffer lives for the lifetime of the process and
the diagnostics page polls it. Every record carries an epoch timestamp (`at`)
next to its display clock, so a range can be selected and log lines line up with
the audio events, which are timestamped the same way.

Selection is one code path for all three consumers — the live panel, the
clipboard and the report — because the interesting question is always "which
records", and answering it twice is how the ones they hand out drift from the
one on screen.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from threading import Lock

WARNING_LEVELS = frozenset({"WARNING", "ERROR", "CRITICAL"})

# Duration presets the page offers. Resolved here, against the server's own
# clock, so a browser whose clock is off cannot slide the window off the records
# it is meant to cover. The page renders whatever keys it is given.
WINDOW_SECONDS: dict[str, int] = {"5m": 300, "15m": 900, "30m": 1800, "1h": 3600}

# How many records the live panel draws. The buffer holds more: the page shows
# the tail of whatever it asked for and states the total it did not draw.
PREVIEW_LIMIT = 300

# Slack on "did the buffer reach back as far as the request": epoch seconds, and
# records are stored with second precision, so landing a second past the
# boundary is not truncation.
_RANGE_SLACK = 2.0


def in_scope(record: dict, source: str, level: str) -> bool:
    """Whether a record passes the source/level axes.

    Two independent axes on purpose: `source` picks whose records to keep
    ("app" = MiCast/AirPlay's own logger tree, "all" = every library), `level`
    picks severities. One dropdown mixing them could never be read straight.
    """
    if source == "app" and not str(record.get("logger", "")).startswith("micast"):
        return False
    return level != "warn" or record.get("level") in WARNING_LEVELS


@dataclass(frozen=True)
class LogQuery:
    """Which slice of the buffer a caller wants.

    `since`/`until` are epoch seconds; None leaves that side open, so a default
    query is the whole buffer. Absolute bounds are the only form the server
    accepts: the page resolves "最近 5 分钟" against the `server_time` shipped
    with every response, which keeps the range and the records on one clock.
    """

    source: str = "all"
    level: str = "all"
    since: float | None = None
    until: float | None = None

    def matches(self, record: dict) -> bool:
        if not in_scope(record, self.source, self.level):
            return False
        at = float(record.get("at") or 0)
        if self.since is not None and at < self.since:
            return False
        return self.until is None or at <= self.until

    @classmethod
    def from_window(
        cls, window: str, source: str, level: str, now: float | None = None
    ) -> LogQuery:
        """Resolve a duration preset against the server's clock.

        "session" (or an unknown key) leaves both ends open: the whole buffer,
        however far back that reaches. Resolving here rather than in the browser
        is what makes "最近 5 分钟" mean five minutes of *these* records even
        when the browser's clock is off.
        """
        seconds = WINDOW_SECONDS.get(window)
        if not seconds:
            return cls(source=source, level=level)
        now = time.time() if now is None else now
        return cls(source=source, level=level, since=now - seconds, until=now)


def resolve_query(
    *,
    source: str,
    level: str,
    window: str | None = None,
    since: float | None = None,
    until: float | None = None,
) -> LogQuery:
    """Absolute bounds when the caller gave any, otherwise a duration preset.

    The page uses both forms: a frozen window is an absolute pair it already
    resolved, while the export panel sends a preset and lets the server pick the
    bounds at the moment it runs.
    """
    if since is None and until is None and window:
        return LogQuery.from_window(window, source, level)
    return LogQuery(source=source, level=level, since=since, until=until)


@dataclass
class LogSelection:
    """Matching records plus everything needed to describe the selection."""

    records: list[dict] = field(default_factory=list)
    total: int = 0
    covered_from: float | None = None
    covered_to: float | None = None
    buffer_total: int = 0
    capacity: int = 0

    def payload(self, limit: int = PREVIEW_LIMIT) -> dict:
        """The live-panel view of this selection: a bounded tail plus totals."""
        items = self.records[-limit:]
        return {
            "items": items,
            "total": self.total,
            "shown": len(items),
            "truncated": self.total > len(items),
            "covered": {"from": self.covered_from, "to": self.covered_to},
            "buffer_total": self.buffer_total,
            "buffer_capacity": self.capacity,
        }


class RuntimeLogHandler(logging.Handler):
    """Ring buffer of the process's own log records.

    Bounded by capacity rather than by age on purpose: a logging burst is what
    fills the buffer, and it evicts by count no matter what, so an age limit
    would only ever discard records the count limit had not yet needed to.
    """

    def __init__(self, capacity: int = 5000):
        super().__init__(logging.INFO)
        self._records: deque[dict] = deque(maxlen=capacity)
        self._lock = Lock()

    @property
    def capacity(self) -> int:
        return self._records.maxlen or 0

    def emit(self, record: logging.LogRecord) -> None:
        item = {
            "at": int(record.created),
            "time": datetime.fromtimestamp(record.created).strftime("%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": self.format(record),
        }
        with self._lock:
            self._records.append(item)

    def select(
        self, query: LogQuery | None = None, *, preview_limit: int | None = None
    ) -> LogSelection:
        """Records matching `query`, oldest first.

        `preview_limit` trims to the tail for the live panel; the report and the
        clipboard pass None and get everything the buffer still holds.
        """
        query = query or LogQuery()
        with self._lock:
            # Copies, not the buffer's own dicts: callers sanitize what they
            # hand out, and that must not rewrite the live records.
            records = [dict(item) for item in self._records]
        matched = [item for item in records if query.matches(item)]
        selection = LogSelection(
            records=matched if preview_limit is None else matched[-preview_limit:],
            total=len(matched),
            buffer_total=len(records),
            capacity=self.capacity,
        )
        if matched:
            selection.covered_from = float(matched[0]["at"])
            selection.covered_to = float(matched[-1]["at"])
        return selection

    def count_newer_than(self, until: float, query: LogQuery | None = None) -> int:
        """Matching records that arrived after `until`.

        This is the "you are frozen and N new lines went by" number: the panel
        keeps showing the locked range while telling the user the process did
        not stop talking.
        """
        query = query or LogQuery()
        with self._lock:
            return sum(
                1
                for item in self._records
                if float(item["at"]) > until and in_scope(item, query.source, query.level)
            )

    def buckets(self, query: LogQuery | None = None, now: float | None = None) -> dict[str, int]:
        """Matching record counts per duration preset, in one pass."""
        query = query or LogQuery()
        now = time.time() if now is None else now
        counts = dict.fromkeys([*WINDOW_SECONDS, "session"], 0)
        with self._lock:
            records = list(self._records)
        for item in records:
            if not in_scope(item, query.source, query.level):
                continue
            at = float(item["at"])
            counts["session"] += 1
            for key, seconds in WINDOW_SECONDS.items():
                if at >= now - seconds:
                    counts[key] += 1
        return counts

    def clear(self) -> int:
        """Drop every buffered record; returns how many were discarded."""
        with self._lock:
            count = len(self._records)
            self._records.clear()
        return count


def head_reached_back(selection: LogSelection, since: float | None) -> bool:
    """Whether the buffer stopped short of the requested start.

    True means the report is missing earlier records that the request asked for
    and the buffer no longer has — the only honest way to say "this is not
    everything you asked for".
    """
    if since is None or selection.covered_from is None:
        return False
    return selection.covered_from - since > _RANGE_SLACK


runtime_logs = RuntimeLogHandler()
runtime_logs.setFormatter(logging.Formatter("%(message)s"))


def install_runtime_log() -> None:
    root = logging.getLogger()
    if runtime_logs not in root.handlers:
        root.addHandler(runtime_logs)
    root.setLevel(min(root.level or logging.INFO, logging.INFO))
    # miservice logs every cloud request at INFO — that is every poll of
    # device_list and every ubus command, which drowns real events in the
    # diagnostics view. Warnings and errors still come through.
    logging.getLogger("miservice").setLevel(logging.WARNING)


def install_asyncio_exception_filter(loop) -> None:
    """Hide harmless Windows connection resets while preserving real loop errors."""

    def handle_exception(_loop, context: dict) -> None:
        error = context.get("exception")
        if isinstance(error, ConnectionResetError) and getattr(error, "winerror", None) == 10054:
            logging.getLogger("micast.network").debug(
                "Peer closed a network connection during cleanup"
            )
            return
        _loop.default_exception_handler(context)

    loop.set_exception_handler(handle_exception)
