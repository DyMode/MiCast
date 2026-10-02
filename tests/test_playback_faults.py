"""Regression tests for playback-fault fixes (fnOS 0.3.0 field reports).

Real-device failure mode: with AirPlay 1 and AirPlay 2 both targeting one
speaker, switching the cast on the phone moved ownership while a play error
recorded during the churn stayed queued in the retry loop — minutes later the
loop re-issued the STALE url with the STALE owner, stealing the speaker back
to a dead stream (silent speaker, cloud still "playing", light on).
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from micast.xiaomi.device_manager import DeviceManager


def _manager(monkeypatch) -> DeviceManager:
    dm = DeviceManager(auth=None)
    dm._service = SimpleNamespace()
    monkeypatch.setattr(
        "micast.xiaomi.device_manager.MinaAPI",
        lambda service, did: SimpleNamespace(
            play_music_url=AsyncMock(),
            play_url=AsyncMock(),
            pause=AsyncMock(),
            stop=AsyncMock(),
        ),
    )
    return dm


async def _run_one_retry_pass(dm, monkeypatch) -> list:
    """Drive _error_retry_loop through exactly one scan of the entries."""
    plays = []

    async def fake_play(device_id, replay_url, owner=None, force=False, audio_id=None, steal=True):
        plays.append((device_id, replay_url, owner, force))
        return True

    monkeypatch.setattr(dm, "play_stream", fake_play)

    scans = 0

    async def fake_sleep(_seconds):
        nonlocal scans
        scans += 1
        if scans > 1:
            # First scan consumed the entries; make the while-loop exit.
            dm._play_errors.clear()

    monkeypatch.setattr("micast.xiaomi.device_manager.asyncio.sleep", fake_sleep)
    await dm._error_retry_loop()
    return plays


@pytest.mark.asyncio
async def test_error_retry_drops_stale_entry_after_ownership_move(monkeypatch):
    dm = _manager(monkeypatch)
    old_url = "http://h:8080/stream/airplay2/for/airplay2/d1?s=1"
    new_url = "http://h:8080/stream/classic/for/classic/d1?s=2"
    dm._owners = {"d1": "classic"}
    dm._playing = {"d1"}
    dm._stream_urls = {"d1": new_url}
    dm.note_play_error("d1", "airplay2", "boom", url=old_url, retry=False)
    dm._play_error_attempts["d1"] = 1  # one failed retry already happened

    plays = await _run_one_retry_pass(dm, monkeypatch)

    assert plays == []  # the stale url was never re-issued
    assert "d1" not in dm._play_errors
    assert dm._owners["d1"] == "classic"
    assert dm._stream_urls["d1"] == new_url


@pytest.mark.asyncio
async def test_error_retry_drops_entry_when_url_replaced(monkeypatch):
    """Same owner but a newer play superseded the url: retry must not pull the
    speaker back to the superseded url."""
    dm = _manager(monkeypatch)
    old_url = "http://h:8080/stream/r1/for/r1/d1?s=1"
    new_url = "http://h:8080/stream/r1/for/r1/d1?s=2"
    dm._owners = {"d1": "r1"}
    dm._playing = {"d1"}
    dm._stream_urls = {"d1": new_url}
    dm.note_play_error("d1", "r1", "boom", url=old_url, retry=False)

    plays = await _run_one_retry_pass(dm, monkeypatch)

    assert plays == []
    assert "d1" not in dm._play_errors


@pytest.mark.asyncio
async def test_error_retry_replays_current_entry(monkeypatch):
    """A still-current error (same owner, same url) IS retried via force play."""
    dm = _manager(monkeypatch)
    url = "http://h:8080/stream/r1/for/r1/d1?s=1"
    dm._owners = {"d1": "r1"}
    dm._playing = {"d1"}
    dm._stream_urls = {"d1": url}
    dm.note_play_error("d1", "r1", "boom", url=url, retry=False)

    plays = await _run_one_retry_pass(dm, monkeypatch)

    assert plays == [("d1", url, "r1", True)]
    assert "d1" not in dm._play_errors
