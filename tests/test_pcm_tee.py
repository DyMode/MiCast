import asyncio

import pytest

from micast.pcm_tee import BRANCH_BUFFER_SECONDS, BoundedPCMReader, PCMTee


@pytest.mark.asyncio
async def test_bounded_pcm_reader_keeps_live_edge_and_eof():
    reader = BoundedPCMReader(max_chunks=2)
    reader.feed_data(b"old")
    reader.feed_data(b"middle")
    reader.feed_data(b"latest")
    reader.feed_eof()

    assert await reader.read() == b"latest"
    assert await reader.read() == b""
    assert reader.at_eof()


@pytest.mark.asyncio
async def test_pcm_tee_outputs_are_bounded_by_time():
    source = asyncio.StreamReader()
    tee = PCMTee(source, outputs=2)
    tee.start()
    source.feed_data(b"x" * 32768 * 100)  # ~5.5s of 48k stereo
    source.feed_eof()
    await tee._task

    for out in tee.outputs:
        assert out.depth_ms() <= out.capacity_ms()
    assert tee.capacity_ms() == pytest.approx(BRANCH_BUFFER_SECONDS * 1000)


@pytest.mark.asyncio
async def test_a_burst_within_the_window_is_never_dropped():
    """The field regression: a paced pump drains at 1x, so a source burst has
    to be able to WAIT in the branch buffer. With a count-based bound, a burst
    of small fragments overflowed it and real audio was thrown away."""
    reader = BoundedPCMReader()
    fragment = b"y" * 2048  # a small source fragment
    for _ in range(60):  # 120KB ≈ 0.6s at 48k stereo
        reader.feed_data(fragment)

    assert reader.dropped_chunks == 0
    assert reader.depth_ms() > 500


@pytest.mark.asyncio
async def test_overflow_past_the_window_drops_the_oldest_chunk():
    reader = BoundedPCMReader(max_seconds=1.0)
    for _ in range(10):  # 3.4s worth, window is 1s
        reader.feed_data(b"z" * 32768)

    assert reader.dropped_chunks > 0
    assert reader.depth_ms() <= reader.capacity_ms()
