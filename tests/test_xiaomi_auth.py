from types import SimpleNamespace

import pytest

from micast.xiaomi import auth as auth_module
from micast.xiaomi.auth import XiaomiAuth


@pytest.mark.asyncio
async def test_miot_is_not_created_from_micoapi_only_tokens(monkeypatch):
    auth = XiaomiAuth()
    auth._token_store = SimpleNamespace(
        load=lambda: {"userId": "1", "micoapi": ("security", "token")}
    )

    async def must_not_build(_tokens):
        raise AssertionError("micoapi-only tokens must not build a MIoT account")

    monkeypatch.setattr(auth, "_build_account", must_not_build)
    assert await auth.ensure_miot_service() is None


@pytest.mark.asyncio
async def test_miot_uses_an_isolated_account_when_xiaomiio_token_exists(monkeypatch):
    tokens = {
        "userId": "1",
        "micoapi": ("mina-security", "mina-token"),
        "xiaomiio": ("miot-security", "miot-token"),
    }
    auth = XiaomiAuth()
    auth._token_store = SimpleNamespace(load=lambda: tokens)
    auth._account = object()
    isolated = object()

    async def build_account(value):
        assert value is tokens
        return isolated

    monkeypatch.setattr(auth, "_build_account", build_account)
    monkeypatch.setattr(
        auth_module, "MiIOService", lambda account: SimpleNamespace(account=account)
    )

    service = await auth.ensure_miot_service()
    assert service.account is isolated
    assert auth._miot_account is isolated
    assert auth._miot_account is not auth._account


@pytest.mark.asyncio
async def test_status_poll_reverifies_only_stale_tokens(monkeypatch):
    """A 30s status poll must not re-exchange the serviceToken every time.

    Field data (0.3.6): the UI's 30s poll forced a passToken verification, so
    every poll did two cloud round trips, rewrote the encrypted token file and
    logged "serviceToken healed after API failure" — clockwork noise in every
    report, with nothing actually failing.
    """
    import time

    auth = XiaomiAuth()
    fresh = {
        "userId": "1",
        "passToken": "p",
        "refreshedAt": int(time.time()),
    }
    auth._token_store = SimpleNamespace(load=lambda: dict(fresh))
    verified = []

    async def verify():
        verified.append(True)
        return "healed"

    monkeypatch.setattr(auth, "verify_credentials", verify)
    assert await auth.verify_if_stale(1800) == "cached"
    assert verified == []

    # Once the token is older than the gate, the poll verifies again.
    stale = dict(fresh, refreshedAt=int(time.time()) - 3600)
    auth._token_store = SimpleNamespace(load=lambda: stale)
    assert await auth.verify_if_stale(1800) == "healed"
    assert verified == [True]


def test_status_reports_an_unreachable_cloud_instead_of_a_lost_login():
    """"Connected" must mean the cloud answers, not "a token file exists".

    Field report (0.5.2): the account showed connected while every cloud call
    timed out, the speaker list was empty, and no QR was offered — the user was
    stuck with no way back in. Tokens may well still be valid, so the state is
    "unstable" (offer a re-login) rather than "expired" (force one).
    """
    auth = XiaomiAuth()
    auth.stored_identity = lambda: (True, "1")  # tokens present
    auth._save_account_state = lambda *a, **k: None

    assert auth.connection_state()["status"] == "connected"
    assert auth.cloud_degraded() is False

    auth.note_cloud_result(True)
    assert auth.connection_state()["status"] == "connected"
    # One timeout is a hiccup, not a broken account.
    auth.note_cloud_result(False)
    assert auth.cloud_degraded() is False

    for _ in range(3):
        auth.note_cloud_result(False)
    state = auth.connection_state()
    assert state["status"] == "unstable"
    assert state["logged_in"] is True  # never claim the login is gone
    assert state["cloud"]["failures"] >= 3

    # ... and one working call puts it back.
    auth.note_cloud_result(True)
    assert auth.connection_state()["status"] == "connected"
    assert auth.cloud_health()["failures"] == 0


def test_cloud_degradation_forgets_failures_that_are_no_longer_fresh():
    """A failure counter left over from long ago says nothing about now."""
    import time

    from micast.xiaomi.auth import CLOUD_FAILURE_FRESH_SECONDS

    auth = XiaomiAuth()
    for _ in range(3):
        auth.note_cloud_result(False)
    assert auth.cloud_degraded() is True

    auth._cloud_last_failure_at = time.time() - CLOUD_FAILURE_FRESH_SECONDS - 60
    assert auth.cloud_degraded() is False


@pytest.mark.asyncio
async def test_a_qr_request_that_cannot_reach_the_account_server_says_so(monkeypatch):
    """The page must get a sentence, not an open request and a blank code.

    Field report (0.3.3): with the cloud out of reach the QR sheet sat on
    "正在连接…" until the request gave up, because every failure looked the same
    from the page. The reason now names the host and what to check, and the
    attempt is bounded here rather than by the browser.
    """
    import aiohttp

    from micast.xiaomi.auth import ACCOUNT_HOST, XiaomiAuthError

    class _RefusingSession:
        def get(self, *args, **kwargs):
            raise aiohttp.ClientError("Name or service not known")

    auth = XiaomiAuth()

    async def session():
        return _RefusingSession()

    monkeypatch.setattr(auth, "_get_session", session)

    with pytest.raises(XiaomiAuthError) as failure:
        await auth.start_qr_login()

    message = str(failure.value)
    assert ACCOUNT_HOST in message
    assert "外网" in message

    # A timeout takes the same path (aiohttp raises ServerTimeoutError, which is
    # both a ClientError and a TimeoutError).
    class _HangingSession:
        def get(self, *args, **kwargs):
            raise TimeoutError

    async def hanging_session():
        return _HangingSession()

    monkeypatch.setattr(auth, "_get_session", hanging_session)

    with pytest.raises(XiaomiAuthError):
        await auth.start_qr_login()


@pytest.mark.asyncio
async def test_an_unreadable_qr_reply_is_reported_as_such(monkeypatch):
    """An intercepting proxy returns HTML; that must not surface as a traceback."""
    from micast.xiaomi.auth import XiaomiAuthError

    class _HtmlSession:
        def get(self, *args, **kwargs):
            class _Response:
                status = 200

                async def read(self):
                    return b"<html>blocked by proxy</html>"

                async def __aenter__(self):
                    return self

                async def __aexit__(self, *exc):
                    return False

            return _Response()

    auth = XiaomiAuth()

    async def session():
        return _HtmlSession()

    monkeypatch.setattr(auth, "_get_session", session)

    with pytest.raises(XiaomiAuthError) as failure:
        await auth.start_qr_login()
    assert "无法识别" in str(failure.value)


@pytest.mark.asyncio
async def test_a_failure_storm_verifies_the_login_once_a_minute(monkeypatch):
    """Every failed call used to send its own credential exchange.

    Field report (0.3.3): with the resolver degraded, MiCast's per-failure
    verification doubled the lookups going into the same pool — the login page's
    own request then queued behind calls that could not finish either.
    """
    from micast.xiaomi.auth import CLOUD_VERIFY_MIN_INTERVAL_SECONDS

    auth = XiaomiAuth()
    auth._token_store = SimpleNamespace(load=lambda: None)  # nothing to verify
    calls = {"n": 0}

    async def verify():
        calls["n"] += 1
        return "unknown"

    monkeypatch.setattr(auth, "verify_credentials", verify)

    for _ in range(5):
        assert await auth.recover_after_failure() in {"unknown", "healed"}

    assert calls["n"] == 1

    # After the window another failure is allowed to look again.
    auth._last_recovery_attempt_at -= CLOUD_VERIFY_MIN_INTERVAL_SECONDS + 1
    await auth.recover_after_failure()

    assert calls["n"] == 2


def test_one_line_per_outage_and_one_for_the_recovery(caplog):
    """A timeline a reader can follow, instead of a line per failed call."""
    import logging

    auth = XiaomiAuth()
    with caplog.at_level(logging.INFO, logger="micast.xiaomi.auth"):
        for _ in range(6):
            auth.note_cloud_result(False)
        auth.note_cloud_result(True)

    messages = [record.message for record in caplog.records]
    assert sum("小米云端连续" in message for message in messages) == 1
    assert any("小米云端已恢复" in message for message in messages)
    assert auth.cloud_health()["failures"] == 0  # the counter that drives the banner
