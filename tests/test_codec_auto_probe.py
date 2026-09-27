"""Background format detection.

Field report (0.5.4): the capability table only ever learned the format
currently being served, so FLAC/WAV stayed "未测" until the user ran the
diagnostics test by hand. The background probe fills those in on the speakers'
own time — silent fixtures, only while nothing is playing.

What must never happen: probing a speaker somebody is using, probing raw pcm
(its support cannot be established remotely at all), or re-probing a verdict
that is still fresh.
"""

import struct
from pathlib import Path
from types import SimpleNamespace

import pytest

import micast.codec_probe as codec_probe
import micast.playback_orchestrator as orchestrator_module
from micast.playback_orchestrator import AUTO_PROBE_FORMATS, PlaybackOrchestrator
from micast.test_tone import silent_probe_wav
from micast.test_tone import test_tone_wav as tone_wav


class _FakeStreamServer:
    """Counts what each speaker read, like the real diagnostic media route.

    `series` fixes the byte totals the samples will observe — that is how a
    "fast full read" is told apart from a "burst then stop"; without it each
    sample simply sees more bytes, like a speaker steadily playing the file.
    """

    def __init__(self, reading: bool = True, series: list[int] | None = None):
        self.media: dict[str, tuple[Path, str]] = {}
        self.bytes: dict[str, int] = {}
        self.reading = reading
        self.series = series
        self._step = 0

    def register_diagnostic_media(self, token, path, media_type):
        self.media[token] = (path, media_type)
        self.bytes[token] = 0

    def diagnostic_bytes(self, token: str) -> int:
        if self.series is not None:
            value = self.series[min(self._step, len(self.series) - 1)]
            self._step += 1
            return value
        if self.reading:
            self.bytes[token] = self.bytes.get(token, 0) + codec_probe.MIN_PULL_BYTES
        return self.bytes.get(token, 0)


class _FakeDeviceManager:
    def __init__(self, scores: dict[str, bool] | None = None, owners: dict[str, str] | None = None):
        self.scores = scores or {}
        self.owners = owners or {}
        self.degraded = False
        self.records: dict[str, dict[str, bool | None]] = {}
        self.meta: dict[str, dict[str, dict]] = {}
        self.play_calls: list[str] = []
        self.stopped: list[str] = []

    def codec_capability_details(self, did: str) -> dict[str, dict]:
        return {fmt: dict(meta) for fmt, meta in self.meta.get(did, {}).items()}

    def cloud_degraded(self) -> bool:
        return self.degraded

    def note_codec_capability(self, did, fmt, supported, reason="stream_verified"):
        # Mirrors the real policy for the one case that matters here.
        verdict: bool | None = supported
        if fmt == "pcm" and supported:
            verdict = None
        self.records.setdefault(did, {})[fmt] = verdict
        self.meta.setdefault(did, {})[fmt] = {
            "status": "unverified" if verdict is None else "supported" if verdict else "unsupported",
            "verified_at": int(__import__("time").time()),
            "reason": reason,
        }
        return verdict

    def owner_of(self, did: str) -> str | None:
        return self.owners.get(did)

    def get_alias(self, did: str) -> str:
        return did

    async def play_stream(self, did, url, owner=None, force=False, audio_id=None):
        self.play_calls.append(did)
        return self.scores.get(did, True)

    async def stop_playback(self, did, owner=None, keep_error=False):
        self.stopped.append(did)


def _orchestrator(monkeypatch, *, bridge=None, device_manager=None, targets=("d1", "d2")):
    monkeypatch.setattr(
        orchestrator_module,
        "settings",
        SimpleNamespace(
            receivers=[SimpleNamespace(id="r1", enabled=True)],
            receiver_targets=lambda receiver_id: list(targets),
        ),
    )
    bridge = bridge or SimpleNamespace(
        has_active_sessions=lambda: False,
        stream_server=_FakeStreamServer(),
    )
    device_manager = device_manager or _FakeDeviceManager()
    tasks: set = set()

    def start_background(coro, name):
        import asyncio

        task = asyncio.create_task(coro, name=name)
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return task

    return PlaybackOrchestrator(bridge, device_manager, start_background), device_manager


@pytest.fixture
def fast_probe(monkeypatch, tmp_path):
    """No real sampling delay, and fixtures that are just token placeholders."""
    monkeypatch.setattr(codec_probe, "SAMPLE_SECONDS", 0)
    monkeypatch.setattr(codec_probe, "probe_directory", lambda: tmp_path)

    def build(stream_server, _source, _directory, prefix="codec"):
        fixtures = {}
        for fmt in codec_probe.FIXTURE_SPECS:
            token = f"{prefix}-{fmt}"
            stream_server.register_diagnostic_media(token, tmp_path / f"{token}.bin", "audio/x")
            fixtures[fmt] = (token, tmp_path / f"{token}.bin", "audio/x")
        return fixtures

    monkeypatch.setattr(codec_probe, "build_fixtures", build)


def test_missing_formats_skips_fresh_verdicts(monkeypatch, fast_probe):
    orch, device_manager = _orchestrator(monkeypatch)
    device_manager.note_codec_capability("d1", "mp3", True, "stream_pull_confirmed")

    assert orch._missing_probe_formats("d1") == ["flac", "wav"]


def test_missing_formats_reopens_an_ancient_verdict(monkeypatch, fast_probe):
    import time as _time

    orch, device_manager = _orchestrator(monkeypatch)
    device_manager.note_codec_capability("d1", "mp3", True, "stream_pull_confirmed")
    device_manager.meta["d1"]["mp3"]["verified_at"] = int(
        _time.time() - orchestrator_module.AUTO_PROBE_RETRY_SECONDS * 5
    )

    assert orch._missing_probe_formats("d1") == ["mp3", "flac", "wav"]


@pytest.mark.asyncio
async def test_background_probe_fills_in_every_framed_format(monkeypatch, fast_probe):
    orch, device_manager = _orchestrator(monkeypatch)

    await orch._probe_missing_formats()

    for did in ("d1", "d2"):
        assert device_manager.records[did] == {"mp3": True, "flac": True, "wav": True}
    # Ownership released after every format: the speaker must be left idle.
    assert device_manager.stopped == ["d1", "d1", "d1", "d2", "d2", "d2"]
    # Nothing is left to learn, so the caller stops rescheduling itself.
    assert await orch._probe_missing_formats() is False


@pytest.mark.asyncio
async def test_background_probe_never_probes_pcm(monkeypatch, fast_probe):
    orch, device_manager = _orchestrator(monkeypatch)

    assert "pcm" not in AUTO_PROBE_FORMATS
    await orch._probe_missing_formats()

    assert "pcm" not in device_manager.records["d1"]


@pytest.mark.asyncio
async def test_background_probe_leaves_a_speaker_alone_while_anything_plays(
    monkeypatch, fast_probe
):
    orch, device_manager = _orchestrator(
        monkeypatch,
        bridge=SimpleNamespace(
            has_active_sessions=lambda: True,
            stream_server=_FakeStreamServer(),
        ),
        targets=("d1",),
    )

    assert await orch._probe_missing_formats() is True  # still owed, not done

    assert device_manager.records == {}
    assert device_manager.play_calls == []


@pytest.mark.asyncio
async def test_background_probe_skips_a_speaker_owned_by_a_receiver(monkeypatch, fast_probe):
    orch, device_manager = _orchestrator(
        monkeypatch,
        device_manager=_FakeDeviceManager(owners={"d1": "r9"}),
        targets=("d1", "d2"),
    )

    await orch._probe_missing_formats()

    assert "d1" not in device_manager.records
    assert "d2" in device_manager.records


@pytest.mark.asyncio
async def test_a_session_starting_mid_probe_stops_it(monkeypatch, fast_probe):
    active = {"playing": False}
    orch, device_manager = _orchestrator(
        monkeypatch,
        bridge=SimpleNamespace(
            has_active_sessions=lambda: active["playing"],
            stream_server=_FakeStreamServer(),
        ),
        targets=("d1",),
    )
    original = codec_probe.probe_device_formats

    async def probe_then_play(*args, **kwargs):
        result = await original(*args, **kwargs)
        active["playing"] = True  # the phone starts streaming now
        return result

    monkeypatch.setattr(codec_probe, "probe_device_formats", probe_then_play)

    await orch._probe_missing_formats()
    first = dict(device_manager.records.get("d1", {}))

    # A second round must not touch the speaker at all.
    await orch._probe_missing_formats()

    assert first == {"mp3": True, "flac": True, "wav": True}
    assert device_manager.play_calls == ["d1", "d1", "d1"]


def test_the_mid_probe_abort_check_is_consulted_between_formats(monkeypatch):
    """The probe asks before every format, not once at the start."""
    calls: list[str] = []

    async def fake_probe(stream_server, device_manager, did, token, url, owner, expected_bytes=0):
        calls.append(token)
        return True

    monkeypatch.setattr(codec_probe, "probe_format", fake_probe)
    device_manager = _FakeDeviceManager()
    fixtures = {fmt: (f"t-{fmt}", Path("x"), "audio/x") for fmt in ("mp3", "flac", "wav")}

    import asyncio

    allowed = {"n": 0}

    def should_continue():
        allowed["n"] += 1
        return allowed["n"] <= 2

    asyncio.run(
        codec_probe.probe_device_formats(
            None,
            device_manager,
            "d1",
            fixtures,
            owner="o",
            should_continue=should_continue,
        )
    )

    assert calls == ["t-mp3", "t-flac"]


def test_the_silent_fixture_is_inaudible_but_not_compressible_away():
    """Silence, so a background probe is inaudible; big enough to be sampled.

    Digital zeros shrink an 8-second FLAC to a few kilobytes, which a speaker
    finishes reading before the probe's first sample — indistinguishable from a
    decoder that read a burst and gave up. ±1 LSB keeps the file natural-sized.
    """
    tone = tone_wav()
    quiet = silent_probe_wav()
    assert tone[:4] == quiet[:4] == b"RIFF"
    assert len(quiet) == len(tone)
    assert structured_max(tone[44:]) > 1000
    assert structured_max(quiet[44:]) == 1


def structured_max(data: bytes) -> int:
    samples = struct.unpack(f"<{len(data) // 2}h", data[: (len(data) // 2) * 2])
    return max(abs(value) for value in samples)


@pytest.mark.asyncio
async def test_a_rejected_play_command_is_not_a_format_verdict(monkeypatch, fast_probe):
    """The cloud turning the command down says nothing about the format.

    Recording it as "unsupported" would hide a working format for a day and
    steer the group away from MP3, which is exactly the advice that must stay
    available.
    """
    orch, device_manager = _orchestrator(
        monkeypatch,
        device_manager=_FakeDeviceManager(scores={"d1": False}),
        targets=("d1",),
    )

    await orch._probe_missing_formats()

    assert device_manager.records == {}


@pytest.mark.asyncio
async def test_a_fast_full_read_is_support(monkeypatch):
    """A speaker that reads the whole fixture inside the first sample window."""
    monkeypatch.setattr(codec_probe, "SAMPLE_SECONDS", 0)
    size = 120_000
    server = _FakeStreamServer(series=[0, size, size])
    result = await codec_probe.probe_format(
        server, _FakeDeviceManager(), "d1", "tok", "http://h/x", "o", expected_bytes=size
    )

    assert result is True


@pytest.mark.asyncio
async def test_a_burst_then_stop_is_not_support(monkeypatch):
    """The decoder that reads a prefix and gives up must stay a "no"."""
    monkeypatch.setattr(codec_probe, "SAMPLE_SECONDS", 0)
    size = 120_000
    server = _FakeStreamServer(series=[0, size // 10, size // 10])
    result = await codec_probe.probe_format(
        server, _FakeDeviceManager(), "d1", "tok", "http://h/x", "o", expected_bytes=size
    )

    assert result is False


@pytest.mark.asyncio
async def test_the_probe_sits_out_a_cloud_outage(monkeypatch, fast_probe):
    """Probing costs a cloud round trip per format — pointless while it is down.

    Field report (0.3.3): the NAS lost its way out to the cloud and MiCast kept
    offering work to the same resolver the user's login had to pass through.
    """
    orch, device_manager = _orchestrator(monkeypatch)
    device_manager.degraded = True

    assert await orch._probe_missing_formats() is True  # still owed, not attempted

    assert device_manager.records == {}
    assert device_manager.play_calls == []
