import asyncio
import wave

import numpy as np

from micast.media_source import MediaPCMSource


async def test_media_decode_uses_explicit_pcm_and_finite_eof(tmp_path):
    path = tmp_path / "tone.wav"
    tone = (np.sin(np.arange(4800) * 2 * np.pi * 440 / 48000) * 10000).astype("<i2")
    with wave.open(str(path), "wb") as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(48000)
        output.writeframes(np.column_stack((tone, tone)).tobytes())
    source = MediaPCMSource(str(path))
    ended = []
    source.on_end = ended.append
    reader = await source.start()
    chunks = []
    while chunk := await asyncio.wait_for(reader.read(32768), 3):
        chunks.append(chunk)
    assert len(b"".join(chunks)) == 4410 * 4
    assert source.position == 0.1
    assert source.ended and ended == [None]
    await source.stop()


def test_open_input_tolerates_pyav_without_metadata_errors(monkeypatch):
    """PyAV 19 removed the metadata_errors kwarg (TypeError); open_input must
    retry without it, keeping every other argument intact."""
    from micast import pyav_compat

    calls = []

    class FakeContainer:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_open(url, **kwargs):
        calls.append(kwargs)
        if "metadata_errors" in kwargs:
            raise TypeError("open() got an unexpected keyword argument 'metadata_errors'")
        return FakeContainer()

    monkeypatch.setattr(pyav_compat.av, "open", fake_open)
    with pyav_compat.open_input(
        "http://x/y.mp3", options={"user_agent": "t"}, timeout=(10, None)
    ) as container:
        assert container is not None
    assert len(calls) == 2
    assert calls[0]["metadata_errors"] == "ignore"
    assert "metadata_errors" not in calls[1]
    assert calls[1]["options"] == {"user_agent": "t"}
    assert calls[1]["timeout"] == (10, None)


def test_decoder_survives_pyav_without_metadata_errors(monkeypatch):
    """Regression for the field failure: on PyAV 19 the deployed decoder died
    with TypeError before even contacting the media server."""
    import micast.media_source as media_source

    calls = []

    def fake_open(url, **kwargs):
        calls.append(kwargs)
        if "metadata_errors" in kwargs:
            raise TypeError("open() got an unexpected keyword argument 'metadata_errors'")

        class Boom:
            def __enter__(self):
                raise RuntimeError("boom")

            def __exit__(self, *args):
                return False

        return Boom()

    monkeypatch.setattr(media_source.av, "open", fake_open)
    source = media_source.MediaPCMSource("http://x/y.mp3")
    source._decode()
    assert len(calls) == 2
    assert "metadata_errors" not in calls[1]
    assert source.error is not None

async def test_source_cleanup_does_not_wait_for_default_executor(monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    release = threading.Event()
    pool = ThreadPoolExecutor(max_workers=1)
    occupied = pool.submit(release.wait)
    loop = asyncio.get_running_loop()
    previous = loop._default_executor
    loop.set_default_executor(pool)
    source = MediaPCMSource('unused')
    worker_release = threading.Event()
    source._thread = threading.Thread(target=worker_release.wait, daemon=True)
    source._thread.start()
    try:
        await asyncio.wait_for(source.stop(), .5)
        assert source._stop.is_set()
    finally:
        worker_release.set()
        release.set()
        source._thread.join(1)
        occupied.result(timeout=1)
        loop._default_executor = previous
        pool.shutdown(wait=True)
