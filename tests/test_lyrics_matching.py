"""Control-only track matching regressions; no sockets or event loop needed."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from micast.lyrics import LyricsSession


def complete(coro):
    try:
        coro.send(None)
    except StopIteration:
        return
    finally:
        coro.close()
    raise AssertionError("Unexpected external I/O")


def test_initial_metadata_tries_derived_title():
    server = SimpleNamespace(
        daap_meta={"title": "作词：某人", "derived": "晴天", "artist": "周杰伦"}
    )
    resend = AsyncMock()
    session = LyricsSession("r1", server, None, resend)
    session._search = AsyncMock(return_value={"audio_id": "123"})
    session._watch = AsyncMock()
    complete(session._run())
    session._search.assert_awaited_once_with("晴天", server.daap_meta, fuzzy=True)
    resend.assert_awaited_once_with({"audio_id": "123"})


def test_watcher_consumes_metadata_already_present(monkeypatch):
    meta = {"title": "晴天", "artist": "周杰伦"}
    session = LyricsSession("r1", SimpleNamespace(daap_events=[(7, meta)]), None, AsyncMock())
    session._search = AsyncMock(return_value={"audio_id": "123"})
    ticks = 0

    async def sleep(_):
        nonlocal ticks
        ticks += 1
        if ticks > 1:
            raise asyncio.CancelledError

    monkeypatch.setattr("micast.lyrics.asyncio.sleep", sleep)
    coro = session._watch()
    with pytest.raises(asyncio.CancelledError):
        coro.send(None)
    session._resend.assert_awaited_once_with({"audio_id": "123"})


def test_credit_lines_never_become_search_candidates():
    assert LyricsSession._candidates({"title": "作词：某人"}) == []
    assert LyricsSession._candidates({"title": "晴天", "derived": "晴天"}) == [("晴天", False)]
