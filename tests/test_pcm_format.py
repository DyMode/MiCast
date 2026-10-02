import numpy as np
import pytest

from micast.pcm_format import PCMFormat, PCMResampler


def test_streaming_resample_preserves_duration_and_pitch():
    rate = 48000
    wave = (np.sin(np.arange(rate) * 2 * np.pi * 1000 / rate) * 20000).astype("<i2")
    pcm = np.column_stack((wave, wave)).tobytes()
    converter = PCMResampler(PCMFormat(rate), PCMFormat(44100))
    output = b"".join(converter.convert(pcm[i : i + 777]) for i in range(0, len(pcm), 777))
    output += converter.flush()
    samples = np.frombuffer(output, dtype="<i2")[::2]
    assert len(samples) == 44100
    frequency = np.argmax(abs(np.fft.rfft(samples)))
    assert frequency == 1000
    assert PCMFormat(rate).bytes_for_ms(100) == 19200


def test_incomplete_samples_rejected_on_eof():
    converter = PCMResampler(PCMFormat(), PCMFormat())
    assert converter.convert(b"x") == b""
    with pytest.raises(ValueError):
        converter.flush()


@pytest.mark.asyncio
@pytest.mark.parametrize("rate", [44100, 48000])
async def test_pipeline_uses_source_rate_for_all_audio_consumers(rate):
    import asyncio
    from micast.pcm_source import ReaderPCMSource
    from micast.speaker_pipeline import SpeakerPipeline
    from micast.stream_server import StreamServer

    pipeline = SpeakerPipeline("test", "test", ReaderPCMSource(asyncio.StreamReader(), rate), StreamServer())
    assert pipeline._input_sample_rate == rate
    # A second of source audio must be a second for pacing and raw output.
    assert (rate * 4) / (pipeline._input_sample_rate * 4) == 1


@pytest.mark.asyncio
async def test_48k_source_does_not_accumulate_fictitious_pacing_lead(monkeypatch):
    import asyncio
    from micast.pcm_source import ReaderPCMSource
    from micast.speaker_pipeline import SpeakerPipeline
    from micast.stream_server import StreamServer

    reader = asyncio.StreamReader()
    pipeline = SpeakerPipeline("ap2", "test", ReaderPCMSource(reader, 48000), StreamServer())
    pipeline._running = True
    now = [0.0]
    class Clock:
        def time(self): return now[0]
    # Model 30 seconds delivered at the source's declared rate, with no waits.
    chunks = iter([b"\0" * 19200] * 300 + [b""])
    async def read(_reader):
        chunk = next(chunks)
        if chunk: now[0] += .1
        return chunk, False
    class Writer:
        def write(self, chunk): pass
        def write_eof(self): pass
        async def drain(self): pass
    pipeline._read_source_chunk = read
    monkeypatch.setattr("micast.speaker_pipeline.asyncio.get_running_loop", lambda: Clock())
    await pipeline._pump_source_to_encoder(reader, Writer())
    assert abs(pipeline._input_ahead_ms) < .001
    assert pipeline._pace_sleeps == 0
