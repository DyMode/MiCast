"""PCM source stall detection: a wedged source during a live session restarts."""

import asyncio
import time

import pytest

from micast.pcm_source import PCMSource
from micast.speaker_pipeline import SpeakerPipeline
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
async def test_session_start_resets_idle_time_and_first_audio_disarms_watchdog():
    pipeline = _pipeline(StarvingSource(), lambda: True)
    pipeline._last_feed_at = 1.0

    started_at = time.monotonic()
    await pipeline.session_start()

    assert pipeline._last_feed_at >= started_at
    assert pipeline._stall_armed is True
    pipeline._note_source_bytes(b"audio")
    assert pipeline._stall_armed is False




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
    monkeypatch.setattr("micast.speaker_pipeline.SOURCE_STALL_CHECK_SECONDS", 0.2)
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
        await asyncio.sleep(0.6)  # three shrunk check intervals
        assert recovered == ["classic"]
        assert source.starts == 1
    finally:
        await pipeline.stop()


@pytest.mark.asyncio
async def test_silence_after_healthy_audio_does_not_restart_source(monkeypatch):
    # Shrink the stall window and the check cadence together: the ratio between
    # them is what this test is about, not the wall clock.
    monkeypatch.setattr("micast.speaker_pipeline.SOURCE_STALL_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr("micast.speaker_pipeline.SOURCE_STALL_CHECK_SECONDS", 0.2)
    source = StarvingSource()
    pipeline = _pipeline(source, lambda: True)

    await pipeline.start()
    try:
        assert source.starts == 1
        await asyncio.sleep(0.9)  # four check intervals of silence
        assert source.starts == 1
        assert source.stops == 0
        assert pipeline.status == "running"
    finally:
        await pipeline.stop()


@pytest.mark.asyncio
async def test_silent_source_without_session_is_left_alone(monkeypatch):
    monkeypatch.setattr("micast.speaker_pipeline.SOURCE_STALL_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr("micast.speaker_pipeline.SOURCE_STALL_CHECK_SECONDS", 0.2)
    source = StarvingSource()
    pipeline = _pipeline(source, lambda: False)  # no sender: silence is normal

    await pipeline.start()
    try:
        await asyncio.sleep(0.9)  # four check intervals of silence
        assert source.starts == 1
        assert source.stops == 0
    finally:
        await pipeline.stop()
