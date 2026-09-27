"""Sanitizer + report/clipboard selection coverage for the diagnostics feature."""

import logging
import re
import time

import pytest

from micast.diagnostics import build_log_text, build_report, sanitize_obj, sanitize_text
from micast.runtime_log import PREVIEW_LIMIT, LogQuery, RuntimeLogHandler, runtime_logs
from micast.url_safety import validate_http_url


def test_sanitize_redacts_credential_pairs():
    text = "login serviceToken=abc123 passToken: tok_789 ssecurity=hexdead"
    out = sanitize_text(text)
    assert "abc123" not in out and "tok_789" not in out and "hexdead" not in out


def test_sanitize_redacts_cookie_and_webhook_tokens():
    # The cookie scrub intentionally masks to end of line: cookie values
    # contain spaces, so a partial mask would leak the tail pairs.
    assert sanitize_text("cookie: a=1; passToken=xyz") == "cookie: ***"
    out = sanitize_text("GET https://open.feishu.cn/open-apis/bot/v2/hook/deadbeefcafe 200")
    assert "deadbeefcafe" not in out
    assert "open.feishu.cn" in out  # host stays readable


def test_sanitize_obj_scrubs_nested_strings():
    obj = {"outer": {"token": "AT_1234567890ab"}, "items": ["uid=UID_abcdef12"]}
    out = sanitize_obj(obj)
    assert "AT_1234567890ab" not in str(out)
    assert "UID_abcdef12" not in str(out)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/x.mp3",
        "http://localhost/a",
        "http://169.254.169.254/latest/meta-data",
        "http://0.0.0.0/",
        "http://[::1]/x",
        "http://224.0.0.1/",
        "ftp://example.com/x",
        "not-a-url",
    ],
)
@pytest.mark.asyncio
async def test_validate_http_url_rejects(url):
    with pytest.raises(ValueError):
        await validate_http_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://192.168.1.10/music/a.mp3",  # LAN NAS is a legitimate target
        "http://10.0.0.5:8000/a.flac",
    ],
)
@pytest.mark.asyncio
async def test_validate_http_url_allows_lan(url):
    assert await validate_http_url(url) == url


class _FakeStreamServer:
    def total_flowing_clients(self) -> int:
        return 0

    def total_bytes(self) -> int:
        return 0


class _FakeBridge:
    status = {"status": "running", "stream_url": "http://127.0.0.1:1/stream.aac"}
    diagnostics = {"raop": {}, "streams": {}}
    _stream_server = _FakeStreamServer()


class _FakeAuth:
    def stored_identity(self):
        return (True, "1")

    def cloud_health(self):
        return {"failures": 0, "last_ok_at": 0, "last_failure_at": 0}

    async def ensure_service(self):
        raise AssertionError("diagnostics must not build a cloud service")


class _FakeDeviceManager:
    selected_device_id = "lx06"
    auth = _FakeAuth()

    def cached_devices(self):
        return [
            {"deviceID": "lx06", "name": "客厅小爱", "hardware": "LX06", "presence": "online"}
        ]

    async def list_devices(self, force: bool = False):
        raise AssertionError("diagnostics must not fetch the device list from the cloud")


@pytest.fixture
def clean_logs():
    """A buffer that starts and ends empty.

    Other tests in the suite attach the same buffer to the root logger, so
    records can arrive mid-test; assertions filter by logger name where that
    matters.
    """
    runtime_logs.clear()
    yield
    runtime_logs.clear()


def _emit(message: str, at: float, level: int = logging.INFO, name: str = "micast.test.report") -> None:
    """Buffer a record with a chosen timestamp (logging would use the clock)."""
    record = logging.LogRecord(name, level, __file__, 1, message, None, None)
    record.created = at
    runtime_logs.emit(record)


def _emitted(logs: list[dict]) -> list[tuple[str, str]]:
    return [
        (item["logger"], item["message"]) for item in logs if item["logger"].startswith("micast.test.")
    ]


@pytest.mark.asyncio
async def test_report_logs_follow_the_query_it_was_given(clean_logs):
    _emit("app info line", 1000)
    _emit("app warning line", 1001, level=logging.WARNING)

    report = await build_report(
        _FakeBridge(), _FakeDeviceManager(), query=LogQuery(source="app", level="warn")
    )

    assert _emitted(report["logs"]) == [("micast.test.report", "app warning line")]
    assert report["log_scope"]["count"] == len(report["logs"])
    assert report["log_scope"]["covered"] == {"from": 1001.0, "to": 1001.0}
    assert report["log_scope"]["truncated"] is False
    # The state half is state: the log section is not repeated inside it.
    assert "logs" not in report["state"]


@pytest.mark.asyncio
async def test_report_carries_the_whole_interval_not_the_panel_preview(clean_logs):
    for index in range(PREVIEW_LIMIT + 5):
        _emit(f"line-{index}", 1000 + index)
    selection = runtime_logs.select(LogQuery(source="app"))
    assert selection.total == PREVIEW_LIMIT + 5

    # The panel only ever draws a tail; the file must still carry everything in
    # the interval, or "export the last 15 minutes" would silently be a preview.
    report = await build_report(
        _FakeBridge(),
        _FakeDeviceManager(),
        query=LogQuery(source="app", since=1000),
    )
    assert report["log_scope"]["count"] == PREVIEW_LIMIT + 5
    assert len(report["logs"]) == PREVIEW_LIMIT + 5


@pytest.mark.asyncio
async def test_report_says_when_the_buffer_stopped_short(clean_logs):
    runtime_logs.clear()
    handler = RuntimeLogHandler(capacity=2)
    for index in range(4):
        record = logging.LogRecord("micast.test.report", logging.INFO, __file__, 1, f"l-{index}", None, None)
        record.created = 1000 + index
        handler.emit(record)
    monkey = pytest.MonkeyPatch()
    monkey.setattr("micast.diagnostics.runtime_logs", handler)
    try:
        report = await build_report(
            _FakeBridge(), _FakeDeviceManager(), query=LogQuery(source="app", since=900)
        )
    finally:
        monkey.undo()

    scope = report["log_scope"]
    assert scope["requested"]["from"] == 900
    assert scope["covered"]["from"] == 1002
    assert scope["truncated"] is True
    assert scope["buffer_capacity"] == 2


@pytest.mark.asyncio
async def test_report_includes_third_party_records_only_when_asked(clean_logs):
    _emit("cloud rejected the request", 1000, level=logging.WARNING, name="miservice.client")

    scoped = await build_report(_FakeBridge(), _FakeDeviceManager(), query=LogQuery(source="app"))
    everything = await build_report(_FakeBridge(), _FakeDeviceManager(), query=LogQuery(source="all"))

    assert _emitted(scoped["logs"]) == []
    assert [item["message"] for item in everything["logs"] if item["logger"] == "miservice.client"] == [
        "cloud rejected the request"
    ]


def test_log_text_is_sanitized_and_self_describing(clean_logs):
    _emit("login passToken=abc123 completed", 1_700_000_000)

    text = build_log_text(
        LogQuery(source="app", level="all", since=1_699_999_000), runtime_logs.select()
    )

    assert "abc123" not in text
    assert "passToken=***" in text
    # Dated header (local clock, so assert the shape), the scope, and an honest
    # note when the requested start predates what the buffer still has.
    assert re.search(r"# 区间 \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} – ", text)
    assert "应用日志 · 全部级别" in text
    assert "1 条" in text
    assert "更早的记录已被丢弃" in text
    assert "06:13:20 INFO    micast.test.report login passToken=*** completed" in text


def _debug_client():
    """The debug router alone, on the fakes above — no Xiaomi session needed."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from micast.routes import debug as debug_routes

    app = FastAPI()
    app.include_router(debug_routes.install(_FakeBridge(), _FakeDeviceManager()))
    return TestClient(app)


def test_report_route_takes_the_query_the_page_is_showing(clean_logs):
    now = time.time()
    _emit("app info line", now - 60)
    _emit("app warning line", now - 30, level=logging.WARNING)
    _emit("app hour-old line", now - 3600)
    client = _debug_client()

    scoped = client.get(
        "/api/debug/report",
        params={"log_source": "app", "log_level": "warn", "log_window": "5m"},
    )

    assert scoped.status_code == 200
    assert "attachment" in scoped.headers["content-disposition"]
    body = scoped.json()
    assert body["log_scope"]["source"] == "app"
    assert body["log_scope"]["level"] == "warn"
    # A preset is resolved server-side, at request time.
    assert body["log_scope"]["requested"]["from"] == pytest.approx(now - 300, abs=5)
    assert body["log_scope"]["requested"]["to"] == pytest.approx(now, abs=5)
    assert _emitted(body["logs"]) == [("micast.test.report", "app warning line")]
    assert scoped.headers["X-MiCast-Log-Count"] == "1"
    assert "logs" not in body["state"]

    # "session" is the whole buffer, and an absolute pair beats a preset.
    assert _emitted(client.get("/api/debug/report", params={"log_window": "session"}).json()["logs"]) == [
        ("micast.test.report", "app info line"),
        ("micast.test.report", "app warning line"),
        ("micast.test.report", "app hour-old line"),
    ]
    # Absolute bounds beat the preset. `at` is stored with second precision, so
    # the bound is a second short of the record it is meant to keep.
    absolute = client.get("/api/debug/report", params={"log_window": "5m", "log_since": now - 3601})
    assert len(absolute.json()["logs"]) == 3

    # Anyone opening the URL without the page's query gets the page's default:
    # the last 15 minutes, MiCast's own records, all levels.
    default = client.get("/api/debug/report").json()
    assert default["log_scope"]["source"] == "app"
    assert default["log_scope"]["level"] == "all"
    assert default["log_scope"]["requested"]["from"] == pytest.approx(now - 900, abs=5)
    assert _emitted(default["logs"]) == [
        ("micast.test.report", "app info line"),
        ("micast.test.report", "app warning line"),
    ]

    assert client.get("/api/debug/report", params={"log_level": "everything"}).status_code == 422
    assert client.get("/api/debug/report", params={"log_window": "2h"}).status_code == 422
    assert client.get("/api/debug/report", params={"log_since": "yesterday"}).status_code == 422


def test_logs_txt_route_serves_the_same_selection_as_text(clean_logs):
    _emit("app info line", 1000)
    client = _debug_client()

    response = client.get("/api/debug/logs.txt", params={"log_source": "app", "log_since": 1000})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert response.headers["X-MiCast-Log-Count"] == "1"
    assert "app info line" in response.text
    assert "app info line" not in client.get(
        "/api/debug/logs.txt", params={"log_source": "app", "log_level": "warn"}
    ).text


def test_state_route_ships_the_tail_plus_the_numbers_the_panel_needs(clean_logs):
    now = time.time()
    _emit("an hour old", now - 3600, name="micast.audio_bridge")
    _emit("brand new", now - 5)

    logs = _debug_client().get("/api/debug/state", params={"log_source": "app"}).json()["logs"]

    assert [item["message"] for item in logs["items"]] == ["an hour old", "brand new"]
    assert logs["total"] == 2
    assert logs["shown"] == 2
    assert logs["truncated"] is False
    assert logs["buffer_capacity"] == runtime_logs.capacity
    assert logs["server_time"] == pytest.approx(now, abs=30)
    assert logs["buckets"]["5m"] == 1
    assert logs["buckets"]["session"] == 2
    assert logs["new_count"] == 0  # only set when the panel is frozen
    # The keys the panel reads for its freeze hint; spelled out so a rename on
    # either side fails here instead of in the browser.
    assert logs["covered"] == {"from": pytest.approx(now - 3600, abs=2), "to": pytest.approx(now - 5, abs=2)}
    assert logs["buffer_total"] == 2
    assert logs["items"][0]["at"] == pytest.approx(now - 3600, abs=2)


def test_state_route_counts_what_arrived_while_frozen(clean_logs):
    _emit("frozen window line", 1000)
    _emit("arrived later", 2000)
    client = _debug_client()

    logs = client.get(
        "/api/debug/state",
        params={"log_source": "app", "log_since": 900, "log_until": 1000},
    ).json()["logs"]

    assert [item["message"] for item in logs["items"]] == ["frozen window line"]
    assert logs["total"] == 1
    assert logs["new_count"] == 1


def test_clear_logs_route_empties_the_buffer(clean_logs):
    client = _debug_client()

    _emit("recorded before the user hit clear", 1000)
    assert _emitted(runtime_logs.select().records)

    response = client.post("/api/debug/logs/clear")

    assert response.status_code == 200
    assert response.json()["cleared"] >= 1
    # The request itself logs after the buffer is emptied, so check the record
    # this test planted rather than the whole (racing) buffer.
    assert _emitted(runtime_logs.select().records) == []


def test_state_and_report_never_call_the_cloud(clean_logs):
    """Both must work while the account is unreachable.

    Field report (0.3.3): the Xiaomi cloud was out of reach, and the diagnostics
    page stayed blank (it looked like "the log is empty") while the report
    download failed too — i.e. the two things that explain a failure were the
    two things that failed with it. The fakes here raise on any cloud call, so
    reaching for one fails the test instead of the user's only diagnostic.
    """
    client = _debug_client()

    state = client.get("/api/debug/state").json()

    assert state["devices"][0]["did"] == "lx06"
    assert state["devices_cached"] is True
    assert state["cloud"] == {"failures": 0, "last_ok_at": 0, "last_failure_at": 0}
    assert state["logged_in"] is True
    assert client.get("/api/debug/report").status_code == 200
