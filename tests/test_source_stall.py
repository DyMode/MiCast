"""PCM source stall detection: a wedged source during a live session restarts."""

import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from micast.audio_bridge import AudioBridge
from micast.pcm_source import PCMSource
from micast.speaker_pipeline import (
    SOURCE_STALL_CHECK_SECONDS,
    SpeakerPipeline,
)
from micast.stream_server import StreamServer


class StarvingSource(PCMSource):
    """Feeds a few bytes once, then hangs forever (a wedged TCP read)."""

    def __init__(self):
        self.starts = 0
        self.stops = 0
        self._reader: asyncio.StreamReader | None = None

    async def start(self) -> asyncio.StreamReader:
        self.starts += 1
        self._reader = asyncio.StreamReader()
        self._reader.feed_data(b"\x00" * 4096)
        return self._reader

    async def stop(self) -> None:
        self.stops += 1


class NeverFeedsSource(StarvingSource):
    """Opens successfully but never produces the first PCM frame."""

    async def start(self) -> asyncio.StreamReader:
        self.starts += 1
        self._reader = asyncio.StreamReader()
        return self._reader


@pytest.mark.asyncio
async def test_bridge_coalesces_stalled_source_recovery():
    bridge = AudioBridge()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def restart():
        entered.set()
        await release.wait()

    bridge.restart = AsyncMock(side_effect=restart)

    first = asyncio.create_task(bridge._recover_stalled_source("r1"))
    await entered.wait()
    await bridge._recover_stalled_source("r1-q1")
    release.set()
    await first

    assert bridge.restart.await_count == 1


@pytest.mark.asyncio
async def test_session_start_resets_idle_time_and_first_audio_disarms_watchdog():
    pipeline = _pipeline(StarvingSource(), lambda: True)
    pipeline._last_feed_at = 1.0

    started_at = time.monotonic()
    await pipeline.session_start()

    assert pipeline._last_feed_at >= started_at
    assert pipeline._stall_armed is True
    pipeline._note_source_bytes(b"audio")
    assert pipeline._stall_armed is False


@pytest.mark.asyncio
async def test_airplay2_stall_rebuilds_only_entry_without_stopping_speaker(monkeypatch):
    from micast.config import AirPlay2InstanceConfig, settings

    monkeypatch.setattr(settings, "airplay2_enabled", True)
    monkeypatch.setattr(
        settings,
        "airplay2_instances",
        [
            AirPlay2InstanceConfig(
                id="airplay2", name="MiCast", target_type="speaker", target_id="did", enabled=True
            )
        ],
    )
    bridge = AudioBridge()
    speaker_stop = AsyncMock()
    bridge.on_session_stop = speaker_stop

    async def rebuild(affected):
        assert affected == {"airplay2"}
        await bridge.session_stop("airplay2")

    bridge._rebuild_airplay2_instances = AsyncMock(side_effect=rebuild)
    bridge.restart = AsyncMock()

    await bridge._recover_stalled_source("airplay2")

    bridge._rebuild_airplay2_instances.assert_awaited_once_with({"airplay2"})
    bridge.restart.assert_not_awaited()
    speaker_stop.assert_not_awaited()


def _pipeline(source: PCMSource, session_active) -> SpeakerPipeline:
    return SpeakerPipeline(
        device_id="airplay2",
        alias="test",
        pcm_source=source,
        stream_server=StreamServer(),
        session_active=session_active,
    )


@pytest.mark.asyncio
async def test_stalled_reader_delegates_upstream_recovery(monkeypatch):
    monkeypatch.setattr("micast.speaker_pipeline.SOURCE_STALL_TIMEOUT_SECONDS", 0.2)
    recovered = []

    async def recover(stream_id):
        recovered.append(stream_id)

    source = NeverFeedsSource()
    pipeline = SpeakerPipeline(
        device_id="classic",
        alias="test",
        pcm_source=source,
        stream_server=StreamServer(),
        session_active=lambda: True,
        on_source_stall=recover,
    )
    await pipeline.start()
    try:
        await asyncio.sleep(SOURCE_STALL_CHECK_SECONDS + 0.5)
        assert recovered == ["classic"]
        assert source.starts == 1
    finally:
        await pipeline.stop()


@pytest.mark.asyncio
async def test_silence_after_healthy_audio_does_not_restart_source(monkeypatch):
    # Shrink the stall window; keep the check cadence intact.
    monkeypatch.setattr("micast.speaker_pipeline.SOURCE_STALL_TIMEOUT_SECONDS", 0.2)
    source = StarvingSource()
    pipeline = _pipeline(source, lambda: True)

    await pipeline.start()
    try:
        assert source.starts == 1
        await asyncio.sleep(SOURCE_STALL_CHECK_SECONDS * 3 + 0.5)
        assert source.starts == 1
        assert source.stops == 0
        assert pipeline.status == "running"
    finally:
        await pipeline.stop()


@pytest.mark.asyncio
async def test_silent_source_without_session_is_left_alone(monkeypatch):
    monkeypatch.setattr("micast.speaker_pipeline.SOURCE_STALL_TIMEOUT_SECONDS", 0.2)
    source = StarvingSource()
    pipeline = _pipeline(source, lambda: False)  # no sender: silence is normal

    await pipeline.start()
    try:
        await asyncio.sleep(SOURCE_STALL_CHECK_SECONDS * 3 + 0.5)
        assert source.starts == 1
        assert source.stops == 0
    finally:
        await pipeline.stop()
