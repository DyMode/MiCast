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
