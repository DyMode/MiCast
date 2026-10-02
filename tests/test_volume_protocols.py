"""Protocol-volume regression tests. All device I/O is mocked."""

import asyncio
import io
import struct
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import av
import httpx
import pytest

from micast.airplay_targets import _Hub, _TargetRuntime
from micast.audio_bridge import AudioBridge
from micast.audio_encoder import MediaProxyPump, stream_media_as_mp3
from micast.config import ReceiverConfig, settings
from micast.device_volume import DeviceVolume
from micast.dlna import DlnaService
from micast.dlna_client import DlnaDevice, DlnaTargetManager
from micast.volume import apply_pcm_gain, db_to_percent


async def test_dlna_linked_source_volume_applies_to_both_network_outputs():
    from unittest.mock import Mock

    airplay = SimpleNamespace(set_volume=AsyncMock())
    dlna = SimpleNamespace(set_volume=AsyncMock())
    bridge = SimpleNamespace(_airplay_targets=airplay, _dlna_targets=dlna)
    media = SimpleNamespace(bridge=bridge, set_volume=Mock())
    manager = SimpleNamespace(owned_targets=lambda receiver, owner: ["s"], set_volume=AsyncMock())
    service = DlnaService(manager, media=media)
    state = service.state_for("r")
    state.volume_mode = "linked"
    state.state = "PLAYING"
    await service.set_volume("r", 25)
    manager.set_volume.assert_awaited_once_with("s", 25)
    airplay.set_volume.assert_awaited_once_with("dlna:r", 25)
    dlna.set_volume.assert_awaited_once_with("dlna:r", 25)
    media.set_volume.assert_called_once_with("r", state)
    assert state.volume == 25


@pytest.mark.parametrize("db,value", [(-144, 0), (-30, 0), (-15, 50), (0, 100)])
def test_airplay_db_mapping(db, value):
    assert db_to_percent(db) == value


@pytest.mark.parametrize("db", [float("nan"), float("inf"), 1, -31])
def test_invalid_db_rejected(db):
    with pytest.raises(ValueError):
        db_to_percent(db)


def test_common_gain_preserves_identity_and_mutes():
    pcm = struct.pack("<hhhh", -30000, 30000, -123, 123)
    assert apply_pcm_gain(pcm, 100) is pcm
    assert apply_pcm_gain(pcm, 0) == bytes(len(pcm))
    result = struct.unpack("<hhhh", apply_pcm_gain(pcm, 50))
    assert 5300 < result[1] < 5400
    assert result[0] == -result[1]


def test_gain_rounding_matches_python_round_sample_by_sample():
    """The vectorised gain must stay bit-identical to ``round(s * gain)``.

    It replaces a per-sample Python loop on the audio hot path (see
    micast.volume); both round half to even, so any drift here would be an
    audio change, not an optimisation.
    """
    samples = [-32768, -32767, -1001, -1, 0, 1, 999, 12345, 32766, 32767] * 40
    pcm = struct.pack(f"<{len(samples)}h", *samples)
    for percent in (1, 7, 33, 50, 66, 99):
        gain = 10 ** ((percent * 0.3 - 30) / 20)
        expected = struct.pack(f"<{len(samples)}h", *[round(sample * gain) for sample in samples])
        assert apply_pcm_gain(pcm, percent) == expected


async def test_airplay2_pipeline_and_network_share_policy(monkeypatch):
    bridge = object.__new__(AudioBridge)
    bridge._pipelines = {}
    pipeline = SimpleNamespace(set_input_volume=Mock(), set_loudness_level=Mock())
    bridge._airplay2_pipelines = {"receiver": pipeline}
    bridge._volume_modes = {}
    bridge._sender_volumes = {}
    bridge._airplay_targets = SimpleNamespace(
        set_input_volume=Mock(), independent_volume=Mock(), set_volume=AsyncMock()
    )
    bridge._dlna_targets = SimpleNamespace(set_volume=AsyncMock())
    bridge.on_receiver_volume = AsyncMock()
    monkeypatch.setattr(settings, "sender_volume_mode", "independent")
    await bridge._local_volume("receiver", 35)
    pipeline.set_input_volume.assert_called_with(35)
    bridge._airplay_targets.set_input_volume.assert_called_with("receiver", 35)
    bridge._airplay_targets.set_volume.assert_not_awaited()
    bridge._dlna_targets.set_volume.assert_not_awaited()
    bridge._volume_modes["receiver"] = "linked"
    await bridge._local_volume("receiver", 25)
    pipeline.set_input_volume.assert_called_with(100)
    bridge._airplay_targets.set_volume.assert_awaited_with("receiver", 25)
    bridge._dlna_targets.set_volume.assert_awaited_with("receiver", 25)


async def test_raw_airplay_tap_applies_gain_once():
    reader = asyncio.StreamReader()
    hub = _Hub(reader)
    hub.input_volume = 50
    target = _TargetRuntime("a", "a")
    target.reader = asyncio.StreamReader()
    target.flowing = True
    hub.targets["a"] = target
    pcm = struct.pack("<hh", 10000, -10000)
    reader.feed_data(pcm)
    reader.feed_eof()
    await hub._run()
    assert await target.reader.read() == apply_pcm_gain(pcm, 50)


async def test_dlna_independent_never_writes_speaker_and_stale_url_mutes(monkeypatch):
    monkeypatch.setattr(settings, "sender_volume_mode", "independent")
    manager = SimpleNamespace(set_volume=AsyncMock())
    service = DlnaService(manager)
    await service.set_uri("r", "http://test.invalid/audio.mp3")
    state = service.state_for("r")
    old_session = state.session_id
    state.state = "PLAYING"
    await service.set_volume("r", 32)
    assert service.media_volume("r", old_session) == 32
    await service.set_mute("r", True)
    assert service.media_volume("r", old_session) == 0
    await service.set_mute("r", False)
    assert await service.get_volume("r") == 32
    await service.set_uri("r", "http://test.invalid/new.mp3")
    assert service.media_volume("r", old_session) == 0
    manager.set_volume.assert_not_awaited()


async def test_dlna_linked_mute_restores_actual_volume(monkeypatch):
    monkeypatch.setattr(settings, "sender_volume_mode", "linked")
    monkeypatch.setattr(
        settings,
        "receivers",
        [ReceiverConfig(id="r", name="r", target_type="speaker", target_id="a")],
    )
    value = 47

    async def get(did, refresh=False):
        return value

    async def put(did, volume):
        nonlocal value
        value = volume
        return value

    service = DlnaService(
        SimpleNamespace(
            get_volume=get,
            set_volume=put,
            owned_targets=lambda rid, owner: settings.receiver_targets(rid),
        )
    )
    assert await service.get_volume("r") == 47
    await service.set_mute("r", True)
    assert value == 0
    await service.set_mute("r", False)
    assert value == 47


async def test_dlna_device_uses_rendering_control_and_readback():
    calls = []
    device = DlnaDevice(
        "uuid:a",
        "test",
        rendering_url="http://test.invalid/volume",
        rendering_service="urn:schemas-upnp-org:service:RenderingControl:2",
    )

    def respond(request):
        calls.append(request)
        return httpx.Response(200, text="<Envelope><CurrentVolume>41</CurrentVolume></Envelope>")

    manager = DlnaTargetManager(SimpleNamespace(resolve=lambda _: device))
    await manager.close()
    manager._client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        adapter = DeviceVolume(None, SimpleNamespace(_dlna_targets=manager))
        assert await adapter.set_volume("dlna:uuid:a", 40) == 41
        assert len(calls) == 2
        assert calls[0].headers["soapaction"].endswith('RenderingControl:2#SetVolume"')
        assert b"<DesiredVolume>40</DesiredVolume>" in calls[0].content
    finally:
        await manager.close()


async def test_media_proxy_full_queue_still_delivers_eof(monkeypatch):
    import micast.audio_encoder as module

    def produce(url, seek, emit, stop, ua, volume):
        for i in range(20):
            emit(bytes([i]))

    monkeypatch.setattr(module, "stream_media_as_mp3", produce)
    pump = MediaProxyPump("unused", 0, "test").start()
    try:
        result = []
        while chunk := await asyncio.wait_for(pump.read(), 2):
            result.append(chunk)
        assert len(result) == 20
    finally:
        pump.abort()


def test_media_proxy_gain_changes_encoded_audio(tmp_path):
    # Generate a local fixture only. Never open an audio output or URL.
    import wave

    path = tmp_path / "source.wav"
    with wave.open(str(path), "wb") as source:
        source.setnchannels(2)
        source.setsampwidth(2)
        source.setframerate(44100)
        source.writeframes(struct.pack("<hh", 10000, -10000) * 8820)
    chunks = []
    stream_media_as_mp3(str(path), 0, chunks.append, threading.Event(), "test", lambda: 0)
    with av.open(io.BytesIO(b"".join(chunks)), format="mp3") as encoded:
        frames = list(encoded.decode(audio=0))
        assert frames
        resampler = av.AudioResampler(format="s16", layout="stereo", rate=44100)
        for frame in frames:
            for pcm in resampler.resample(frame):
                assert bytes(pcm.planes[0])[: pcm.samples * 4] == bytes(pcm.samples * 4)
