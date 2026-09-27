import asyncio
import logging

import pytest

from micast.runtime_log import (
    LogQuery,
    RuntimeLogHandler,
    head_reached_back,
    in_scope,
    install_asyncio_exception_filter,
    resolve_query,
)


def _emit(handler: RuntimeLogHandler, level: int, message: str, at: float, name: str = "micast.test") -> None:
    """Put a record in the buffer with a chosen timestamp.

    Emitting by hand (rather than through a logger) is the only way to test
    intervals: the buffer stores `record.created`, which logging fills with the
    wall clock.
    """
    record = logging.LogRecord(name, level, __file__, 1, message, None, None)
    record.created = at
    handler.emit(record)


def test_runtime_log_is_bounded():
    handler = RuntimeLogHandler(capacity=3)
    for index in range(5):
        _emit(handler, logging.INFO, f"record-{index}", 1000 + index)
    assert [item["message"] for item in handler.select().records] == [
        "record-2",
        "record-3",
        "record-4",
    ]
    assert handler.capacity == 3


def test_selection_hands_out_copies_not_the_live_records():
    # Callers sanitize what they hand out; that must not rewrite the buffer.
    handler = RuntimeLogHandler(capacity=5)
    _emit(handler, logging.INFO, "original", 1000)
    selection = handler.select()
    selection.records[0]["message"] = "rewritten"
    assert handler.select().records[0]["message"] == "original"


def test_query_selects_an_interval_and_the_preview_keeps_the_tail():
    handler = RuntimeLogHandler(capacity=10)
    for index in range(5):
        _emit(handler, logging.INFO, f"line-{index}", 1000 + index)

    whole = handler.select()
    assert whole.total == 5
    assert (whole.covered_from, whole.covered_to) == (1000, 1004)

    tail = handler.select(preview_limit=2)
    assert [item["message"] for item in tail.records] == ["line-3", "line-4"]
    assert tail.total == 5  # the total is the range, not what was drawn
    assert tail.payload(2)["truncated"] is True
    assert tail.payload(2)["shown"] == 2

    window = handler.select(LogQuery(since=1001, until=1003), preview_limit=None)
    assert [item["message"] for item in window.records] == ["line-1", "line-2", "line-3"]
    assert window.total == 3


def test_query_carries_both_axes_and_an_epoch_stamp():
    handler = RuntimeLogHandler(capacity=10)
    _emit(handler, logging.INFO, "app info", 1000, name="micast.audio_bridge")
    _emit(handler, logging.WARNING, "app warning", 1001, name="micast.audio_bridge")
    _emit(handler, logging.WARNING, "library warning", 1002, name="miservice.client")

    app = handler.select(LogQuery(source="app", level="warn"))
    assert [item["message"] for item in app.records] == ["app warning"]

    everything = handler.select(LogQuery(source="all", level="all"))
    assert everything.total == 3
    assert everything.records[0]["at"] == 1000
    assert everything.records[0]["time"]  # the display clock rides along


def test_window_presets_resolve_against_the_given_clock():
    query = LogQuery.from_window("15m", "app", "warn", now=10_000.0)
    assert (query.source, query.level) == ("app", "warn")
    assert (query.since, query.until) == (9_100.0, 10_000.0)
    # "session" is the whole buffer: open on both ends.
    session = LogQuery.from_window("session", "all", "all", now=10_000.0)
    assert (session.since, session.until) == (None, None)


def test_absolute_bounds_win_over_a_preset():
    # The page sends a preset for the export panel and an absolute pair for a
    # frozen window; a request carrying both must not silently mix them.
    assert resolve_query(source="app", level="all", window="5m", since=900, until=1000) == LogQuery(
        source="app", level="all", since=900, until=1000
    )
    assert resolve_query(source="app", level="all", window=None) == LogQuery(
        source="app", level="all"
    )


def test_buckets_count_the_duration_presets_in_one_pass():
    handler = RuntimeLogHandler(capacity=10)
    now = 10_000.0
    _emit(handler, logging.INFO, "just now", 10_000)
    _emit(handler, logging.INFO, "ten minutes ago", 9_400)
    _emit(handler, logging.INFO, "fifty minutes ago", 7_000)

    assert handler.buckets(LogQuery(source="app"), now) == {
        "5m": 1,
        "15m": 2,
        "30m": 2,
        "1h": 3,
        "session": 3,
    }
    # The buckets follow the axes, so the action panel sizes the same records
    # the view is filtered to.
    assert handler.buckets(LogQuery(source="all", level="warn"), now)["session"] == 0
    assert handler.count_newer_than(9_400, LogQuery(source="app")) == 1
    assert handler.count_newer_than(7_000, LogQuery(source="app")) == 2


def test_head_reached_back_flags_a_buffer_that_stopped_short():
    handler = RuntimeLogHandler(capacity=2)
    for index in range(4):
        _emit(handler, logging.INFO, f"line-{index}", 1000 + index)

    selection = handler.select(LogQuery(since=990))
    assert selection.covered_from == 1002
    assert selection.buffer_total == 2
    assert head_reached_back(selection, 990) is True
    # Landing on the boundary is not truncation, and an open start can't be.
    assert head_reached_back(selection, 1002) is False
    assert head_reached_back(selection, None) is False
    assert head_reached_back(handler.select(LogQuery(since=9900)), 9900) is False


def test_clear_reports_how_many_records_it_dropped():
    handler = RuntimeLogHandler(capacity=5)
    _emit(handler, logging.INFO, "one", 1000)
    _emit(handler, logging.INFO, "two", 1001)
    assert handler.clear() == 2
    assert handler.select().records == []
    assert handler.clear() == 0


@pytest.mark.parametrize(
    ("source", "level", "logger_name", "record_level", "expected"),
    [
        # "app" keeps MiCast's own logger tree — AirPlay included — and nothing
        # from the libraries it depends on.
        ("app", "all", "micast.raop.server", "INFO", True),
        ("app", "all", "miservice.client", "INFO", False),
        ("app", "warn", "micast.audio_bridge", "INFO", False),
        ("app", "warn", "micast.audio_bridge", "ERROR", True),
        ("all", "all", "miservice.client", "INFO", True),
        ("all", "warn", "miservice.client", "WARNING", True),
        ("all", "warn", "miservice.client", "DEBUG", False),
    ],
)
def test_in_scope_splits_source_from_level(source, level, logger_name, record_level, expected):
    record = {"logger": logger_name, "level": record_level}
    assert in_scope(record, source, level) is expected


def test_asyncio_filter_suppresses_windows_connection_reset(monkeypatch):
    loop = asyncio.new_event_loop()
    delegated = []
    monkeypatch.setattr(loop, "default_exception_handler", delegated.append)
    install_asyncio_exception_filter(loop)
    error = ConnectionResetError(10054, "connection reset")
    error.winerror = 10054
    loop.call_exception_handler({"exception": error})
    assert delegated == []
    loop.close()


def test_asyncio_filter_keeps_other_errors(monkeypatch):
    loop = asyncio.new_event_loop()
    delegated = []
    monkeypatch.setattr(loop, "default_exception_handler", delegated.append)
    install_asyncio_exception_filter(loop)
    context = {"exception": RuntimeError("boom")}
    loop.call_exception_handler(context)
    assert delegated == [context]
    loop.close()
