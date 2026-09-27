"""In-process PyAV encoder: format headers, filter chain, resample, lifecycle."""

import asyncio
import io
import math
import queue
import struct

import av

from micast.audio_encoder import (
    AudioEncoder,
    _open_encoder,
    _put_latest_async,
    _put_latest_sync,
    _StreamSink,
    encoder_silence_chunks,
    mp3_silence,
    raw_pcm_format,
    transcode_file_to_wav,
)
from micast.config import settings


def _sine_pcm(seconds: float = 1.0, rate: int = 44100, freq: float = 440.0) -> bytes:
    pcm = bytearray()
    for i in range(int(seconds * rate)):
        v = int(10000 * math.sin(2 * math.pi * freq * i / rate))
        pcm += struct.pack("<hh", v, v)
    return bytes(pcm)


def _audio_config(fmt: str, sample_rate: int = 48000):
    return settings.audio.model_copy(update={"format": fmt, "sample_rate": sample_rate})


def test_mp3_silence_is_decodable_and_cached():
    encoded = mp3_silence(48000, "320k", 1)
    assert encoded is mp3_silence(48000, "320k", 1)
    with av.open(io.BytesIO(encoded)) as container:
        frames = list(container.decode(audio=0))
    assert frames
    assert sum(frame.samples for frame in frames) >= 48000


def test_every_stream_format_has_silence_to_send():
    """Delay fill needs something to send in *each* format.

    Only mp3 had a silence frame, so a flac/raw-PCM client whose delay was
    filling received nothing at all — an empty socket long enough for a speaker
    to decide the stream had died.
    """
    assert encoder_silence_chunks("audio/mpeg", 48000, "320k") == (mp3_silence(48000, "320k", 0),)

    flac = encoder_silence_chunks("audio/flac", 48000, "320k")
    assert flac and all(not chunk.startswith(b"fLaC") for chunk in flac)

    pcm = encoder_silence_chunks("audio/wav", 48000, "320k")
    assert pcm == (bytes(3840),)  # 20ms of s16 stereo, frame-aligned


def test_flac_silence_decodes_as_a_continuation_of_the_live_stream():
    """The frames must be usable mid-stream — and carry no second header.

    A decoder given a fresh `fLaC` + STREAMINFO in the middle of a live stream
    would restart; the frames themselves are what keeps a filling client alive.
    """
    chunks: list[bytes] = []
    container = av.open(_StreamSink(chunks.append), mode="w", format="flac")
    stream = _open_encoder(container, "flac", "320k", 48000)
    frame = av.AudioFrame(format="s16", layout="stereo", samples=4096)
    frame.sample_rate = 48000
    frame.planes[0].update(bytes(4096 * 4))
    for packet in stream.encode(frame):
        container.mux(packet)
    for packet in stream.encode(None):
        container.mux(packet)
    container.close()
    header, live_frames = chunks[0], chunks[1:]
    assert header.startswith(b"fLaC")

    silence = encoder_silence_chunks("audio/flac", 48000, "320k")
    stream_bytes = b"".join([header, *live_frames, *silence, *silence])
    with av.open(io.BytesIO(stream_bytes)) as decoded:
        frames = list(decoded.decode(audio=0))

    assert len(frames) >= len(live_frames) + 2 * len(silence)
    assert max(abs(sample) for frame in frames for sample in frame.to_ndarray().flatten()) == 0


def test_realtime_queues_drop_oldest_instead_of_growing():
    sync_queue = queue.Queue(maxsize=2)
    _put_latest_sync(sync_queue, b"old")
    _put_latest_sync(sync_queue, b"middle")
    _put_latest_sync(sync_queue, b"latest")
    assert [sync_queue.get_nowait(), sync_queue.get_nowait()] == [b"middle", b"latest"]

    async_queue = asyncio.Queue(maxsize=2)
    _put_latest_async(async_queue, b"old")
    _put_latest_async(async_queue, b"middle")
    _put_latest_async(async_queue, b"latest")
    assert [async_queue.get_nowait(), async_queue.get_nowait()] == [b"middle", b"latest"]


async def _encode(
    fmt: str,
    pcm: bytes,
    *,
    rate_in: int = 44100,
    rate_out: int = 48000,
    audio_filter: str | None = None,
) -> bytes:
    encoder = AudioEncoder(
        _audio_config(fmt, rate_out), input_sample_rate=rate_in, audio_filter=audio_filter
    )
    await encoder.start()
    # Feed in realistic chunks, like the pipeline's pump does.
    for offset in range(0, len(pcm), 8192):
        encoder.stdin.write(pcm[offset : offset + 8192])
    encoder.stdin.write_eof()
    assert await encoder.wait() == 0

    out = bytearray()
    while chunk := await encoder.stdout.read():
        out += chunk
    return bytes(out)


def _decode(data: bytes, fmt: str) -> tuple[int, int]:
    """Return (decoded sample rate, total samples) to prove the stream is valid."""
    container = av.open(io.BytesIO(data), format=fmt)
    stream = container.streams.audio[0]
    samples = 0
    for frame in container.decode(stream):
        samples += frame.samples
    return stream.codec_context.sample_rate, samples


async def test_mp3_stream_is_valid_and_resampled():
    data = await _encode("mp3", _sine_pcm())
    assert data[:3] == b"ID3" or data[0] == 0xFF  # ID3 tag or frame sync
    rate, samples = _decode(data, "mp3")
    assert rate == 48000  # 44100 input resampled to the configured 48000
    assert samples > 40000


async def test_flac_stream_is_valid():
    data = await _encode("flac", _sine_pcm())
    assert data[:4] == b"fLaC"
    rate, samples = _decode(data, "flac")
    assert rate == 48000
    assert samples == 48000  # lossless: exactly 1s after resample


async def test_wav_stream_has_streaming_header():
    data = await _encode("wav", _sine_pcm(), rate_out=44100)
    assert data[:4] == b"RIFF"
    assert struct.unpack("<I", data[4:8])[0] == 0xFFFFFFFF  # unknown length
    rate, samples = _decode(data, "wav")
    assert samples == 44100


async def test_equalizer_filter_boosts_signal_energy():
    pcm = _sine_pcm(seconds=0.5, freq=1000.0)
    flat = await _encode("wav", pcm, rate_out=44100)
    boosted = await _encode(
        "wav", pcm, rate_out=44100, audio_filter="equalizer=f=1000:t=q:w=1.0:g=12"
    )

    def rms(data: bytes) -> float:
        start = data.index(b"data") + 8  # skip the container header
        samples = struct.unpack(f"<{(len(data) - start) // 2}h", data[start:])
        return math.sqrt(sum(s * s for s in samples) / len(samples))

    assert rms(boosted) > rms(flat) * 2  # +12dB ≈ 4x amplitude at the band


async def test_pan_filter_picks_one_channel():
    # Left channel sine, right channel silence.
    pcm = bytearray()
    for i in range(44100 // 2):
        v = int(10000 * math.sin(2 * math.pi * 440 * i / 44100))
        pcm += struct.pack("<hh", v, 0)
    data = await _encode("wav", bytes(pcm), rate_out=44100, audio_filter="pan=stereo|c0=FR|c1=FR")
    start = data.index(b"data") + 8
    samples = struct.unpack(f"<{(len(data) - start) // 2}h", data[start:])
    assert max(abs(s) for s in samples) < 100  # FR was silent → near silence out


def test_stream_format_byte_rates():
    mp3 = AudioEncoder(_audio_config("mp3"), 44100).format
    assert mp3.content_type == "audio/mpeg"
    assert mp3.byte_rate == 40000  # 320kbps
    wav = AudioEncoder(_audio_config("wav", 44100), 44100).format
    assert wav.byte_rate == 44100 * 4
    raw = raw_pcm_format(48000)
    assert raw.byte_rate == 48000 * 4


async def test_encoder_stop_mid_stream():
    encoder = AudioEncoder(_audio_config("mp3"), 44100)
    await encoder.start()
    encoder.stdin.write(_sine_pcm(seconds=0.1))
    await encoder.stop()
    assert encoder.returncode in (0, None)  # stop is a clean EOF, never a crash


def test_transcode_file_to_wav(tmp_path):
    # Build a small MP3 first, then run the debug-page transcode over it.
    src = tmp_path / "tone.mp3"
    container = av.open(str(src), mode="w", format="mp3")
    stream = container.add_stream("libmp3lame", rate=44100)
    stream.layout = "stereo"
    frame = av.AudioFrame(format="s16", layout="stereo", samples=44100 // 10)
    frame.sample_rate = 44100
    frame.planes[0].update(_sine_pcm(seconds=0.1))
    for packet in stream.encode(frame):
        container.mux(packet)
    for packet in stream.encode(None):
        container.mux(packet)
    container.close()

    wav = transcode_file_to_wav(src)
    assert wav[:4] == b"RIFF"
    rate, samples = _decode(wav, "wav")
    assert rate == 44100
    assert samples > 0
