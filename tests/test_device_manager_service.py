import asyncio
from unittest.mock import AsyncMock

import pytest

from micast.xiaomi.device_manager import DeviceManager


class RotatingAuth:
    def __init__(self):
        self.service = object()

    async def ensure_service(self):
        return self.service


async def test_device_manager_adopts_rotated_auth_service():
    auth = RotatingAuth()
    manager = DeviceManager(auth)

    assert await manager.refresh_service()
    first = manager._service
    auth.service = object()
    assert await manager.refresh_service()

    assert manager._service is auth.service
    assert manager._service is not first


async def test_close_cancels_and_joins_all_owned_tasks():
    manager = DeviceManager(RotatingAuth())

    async def forever():
        await asyncio.Event().wait()

    watchdog = asyncio.create_task(forever())
    retry = asyncio.create_task(forever())
    manager._watchdog_tasks["speaker"] = watchdog
    manager._error_retry_task = retry

    await manager.close()

    assert watchdog.cancelled()
    assert retry.cancelled()
    assert not manager._watchdog_tasks
    assert manager._error_retry_task is None


@pytest.mark.asyncio
async def test_stop_playback_retries_a_failing_cloud_command(monkeypatch):
    """A stop that never lands leaves the speaker playing a reachable stream.

    Field data (0.4.1): the cloud answered with "ubus server internal error ...
    Timed out waiting 2000.00ms" once and the speaker kept playing (silence)
    while the UI said nothing was wrong.
    """
    from micast.xiaomi import device_manager as module
    from micast.xiaomi.device_manager import DeviceManager

    manager = DeviceManager(RotatingAuth())
    manager._service = object()
    manager._playing.add("did")
    manager._stream_urls["did"] = "http://x/stream/r1"

    attempts = {"pause": 0, "stop": 0}

    class _API:
        def __init__(self, *_args, **_kwargs):
            pass

        async def pause(self):
            attempts["pause"] += 1
            if attempts["pause"] == 1:
                raise RuntimeError("ubus server internal error")

        async def stop(self):
            attempts["stop"] += 1

    monkeypatch.setattr(module, "MinaAPI", _API)
    monkeypatch.setattr(manager, "refresh_service", AsyncMock(return_value=True))
    monkeypatch.setattr(module.asyncio, "sleep", AsyncMock())

    await manager.stop_playback("did")

    assert attempts["pause"] == 2  # retried after the transient failure
    assert attempts["stop"] == 2
    assert "did" not in manager._stream_urls


@pytest.mark.asyncio
async def test_stop_playback_gives_up_after_the_attempt_budget(monkeypatch):
    from micast.xiaomi import device_manager as module
    from micast.xiaomi.device_manager import DeviceManager

    manager = DeviceManager(RotatingAuth())
    manager._service = object()
    manager._playing.add("did")
    attempts = {"n": 0}

    class _API:
        def __init__(self, *_args, **_kwargs):
            pass

        async def pause(self):
            attempts["n"] += 1
            raise RuntimeError("cloud down")

        async def stop(self):
            attempts["n"] += 1
            raise RuntimeError("cloud down")

    monkeypatch.setattr(module, "MinaAPI", _API)
    monkeypatch.setattr(manager, "refresh_service", AsyncMock(return_value=True))
    monkeypatch.setattr(module.asyncio, "sleep", AsyncMock())

    await manager.stop_playback("did")  # must not raise

    assert attempts["n"] == 2 * module.STOP_COMMAND_ATTEMPTS


def _isolated_manager(tmp_path, auth=None):
    """A manager whose capability file lives in the test's own directory.

    Without this the capability store is the developer's real config file, so a
    test can both read and pollute it (it did, once).
    """
    from micast.xiaomi.device_manager import DeviceManager as Manager

    manager = Manager(auth or RotatingAuth())
    manager._codec_capability_path = tmp_path / "codec-capabilities.json"
    manager._codec_capabilities.clear()
    manager._codec_capability_meta.clear()
    return manager


def test_codec_formats_are_canonical_and_legacy_alias_is_dropped(tmp_path):
    """One vocabulary: the app's own formats, and no WAV/PCM aliasing.

    Older builds recorded "PCM/WAV" from a WAV *file* probe and reused the same
    label for raw-PCM passthrough, so the two were indistinguishable — a
    speaker was shown as "PCM supported" and then went silent on the live
    stream (field data 0.4.2). The alias is dropped, not guessed at.
    """
    from micast.xiaomi import device_manager as module

    manager = _isolated_manager(tmp_path)
    manager.note_codec_capability("did", "FLAC", True, "active_probe")
    manager.note_codec_capability("did", "pcm", False, "stream_pull_confirmed")
    manager.note_codec_capability("did", "PCM/WAV", True, "active_probe")

    caps = manager.codec_capabilities("did")
    assert caps == {"flac": True, "pcm": False}
    assert manager.codec_capability_details("did")["pcm"]["label"] == module.CODEC_LABELS["pcm"]

    # A persisted legacy file is migrated the same way.
    manager2 = _isolated_manager(tmp_path)
    manager2._codec_capability_path.write_text(
        '{"did": {"MP3": {"status": "supported", "verified_at": 1},'
        ' "PCM/WAV": {"status": "supported", "verified_at": 1}}}',
        encoding="utf-8",
    )
    manager2._load_codec_capabilities()
    assert manager2.codec_capabilities("did") == {"mp3": True}


def test_group_compatibility_recommends_the_best_confirmed_format(tmp_path):
    from micast.xiaomi import device_manager as module

    manager = _isolated_manager(tmp_path)
    for did in ("a", "b"):
        manager.note_codec_capability(did, "flac", True, "stream_pull_confirmed")
        manager.note_codec_capability(did, "pcm", False, "stream_pull_confirmed")

    compat = manager.codec_compatibility(["a", "b"])
    assert compat["confirmed_common_formats"] == ["flac"]
    assert compat["recommended_format"] == "flac"  # never PCM by default
    assert "pcm" not in compat["possible_common_formats"]
    assert compat["status"] == "confirmed"
    assert compat["labels"]["pcm"] == module.CODEC_LABELS["pcm"]


def test_group_compatibility_ignores_a_stale_verdict_for_advice(tmp_path):
    import time as _time

    from micast.xiaomi.device_manager import CODEC_CAPABILITY_TTL_SECONDS as TTL

    manager = _isolated_manager(tmp_path)
    manager.note_codec_capability("a", "pcm", False, "active_probe")
    manager._codec_capability_meta["a"]["pcm"]["verified_at"] = int(_time.time() - TTL - 60)

    compat = manager.codec_compatibility(["a"])
    # The only verdict on pcm is expired: it stops disproving the format (a
    # firmware update may have fixed it) while still being reported as stale.
    assert "pcm" in compat["possible_common_formats"]
    assert compat["stale_formats"] == {"a": ["pcm"]}
    assert compat["status"] == "needs_check"
    # A confirmed verdict still drives the recommendation.
    manager.note_codec_capability("a", "flac", True, "stream_pull_confirmed")
    assert manager.codec_compatibility(["a"])["recommended_format"] == "flac"


def test_a_pcm_pull_is_recorded_as_unverified_never_as_support(tmp_path):
    """Bytes on the wire do not prove raw passthrough works.

    Field data (2026-09): 厨房小爱 (OH2P) pulled the live PCM stream at full
    rate and played nothing, while the probe called it PCM-capable on the
    strength of those bytes. A pcm pull therefore earns no boolean verdict.
    """
    from micast.xiaomi import device_manager as module

    manager = _isolated_manager(tmp_path)

    assert manager.note_codec_capability("did", "pcm", True, "stream_pull_confirmed") is None
    assert manager.codec_capabilities("did") == {}
    detail = manager.codec_capability_details("did")["pcm"]
    assert detail["status"] == "unverified"
    assert detail["reason"] == module.PCM_UNVERIFIED_REASON

    # Withheld, not inherited: an older "unsupported" must not survive a pull
    # either, or nothing could ever re-open the question.
    assert manager.note_codec_capability("did", "pcm", False, "no_stream_pull") is False
    manager.note_codec_capability("did", "wav", True, "stream_pull_confirmed")
    assert manager.note_codec_capability("did", "pcm", True, "stream_pull_confirmed") is None
    assert manager.codec_capabilities("did") == {"wav": True}

    # ...and it survives a reload (the file keeps the record without a verdict).
    reloaded = _isolated_manager(tmp_path)
    reloaded._load_codec_capabilities()
    assert reloaded.codec_capabilities("did") == {"wav": True}
    assert reloaded.codec_capability_details("did")["pcm"]["status"] == "unverified"


def test_a_persisted_pcm_supported_record_is_re_decided_on_load(tmp_path, monkeypatch):
    """The old false "✓" must not survive in an existing capability file."""
    from micast.config import SpeakerConfig, settings
    from micast.xiaomi import device_manager as module

    monkeypatch.setattr(
        settings,
        "speakers",
        [SpeakerConfig(did="kitchen", alias="厨房小爱", hardware="OH2P")],
    )
    manager = _isolated_manager(tmp_path)
    manager._codec_capability_path.write_text(
        '{"kitchen": {"pcm": {"status": "supported", "verified_at": 1},'
        ' "mp3": {"status": "supported", "verified_at": 1}},'
        ' "other": {"pcm": {"status": "supported", "verified_at": 1}}}',
        encoding="utf-8",
    )

    manager._load_codec_capabilities()

    assert manager.codec_capabilities("kitchen") == {"mp3": True, "pcm": False}
    assert manager.codec_capability_details("kitchen")["pcm"]["reason"] == (
        module.PCM_MODEL_UNSUPPORTED_REASON
    )
    # An unknown model keeps the record, but without a verdict.
    assert manager.codec_capabilities("other") == {}
    assert manager.codec_capability_details("other")["pcm"]["status"] == "unverified"


def test_a_model_known_to_discard_pcm_is_never_recorded_as_supporting_it(tmp_path, monkeypatch):
    """OH2P's firmware reads the stream and plays nothing, so no pull may flip it."""
    from micast.config import SpeakerConfig, settings
    from micast.xiaomi import device_manager as module

    monkeypatch.setattr(
        settings,
        "speakers",
        [
            SpeakerConfig(did="kitchen", alias="厨房小爱", hardware="OH2P"),
            SpeakerConfig(did="fourth", alias="四楼小爱", hardware="OH2"),
        ],
    )
    manager = _isolated_manager(tmp_path)

    assert manager.note_codec_capability("kitchen", "pcm", True, "active_probe") is False
    detail = manager.codec_capability_details("kitchen")["pcm"]
    assert detail["reason"] == module.PCM_MODEL_UNSUPPORTED_REASON

    # The same pull on a model whose decoder does handle raw pcm stays withheld.
    assert manager.note_codec_capability("fourth", "pcm", True, "active_probe") is None

    compat = manager.codec_compatibility(["kitchen", "fourth"])
    assert "pcm" not in compat["possible_common_formats"]
    assert compat["status"] == "needs_check"


@pytest.mark.asyncio
async def test_cloud_calls_are_bounded_while_the_cloud_is_slow():
    """Unbounded calls are what turn slow DNS into a dead account.

    aiohttp shields each DNS lookup, so a call that times out leaves its lookup
    holding a resolver thread. This pins the bound that keeps a degraded
    resolver from being handed more work than it can ever finish.
    """
    import asyncio

    from micast.xiaomi.device_manager import DeviceManager
    from micast.xiaomi.mina_api import CLOUD_CONCURRENCY

    manager = DeviceManager(auth=None)
    manager._service = object()
    live = {"now": 0, "peak": 0}

    class _Service:
        async def device_list(self):
            live["now"] += 1
            live["peak"] = max(live["peak"], live["now"])
            await asyncio.sleep(0.02)
            live["now"] -= 1
            return []

    manager._service = _Service()

    await asyncio.gather(*(manager.cloud_api("").device_list() for _ in range(6)))

    assert live["peak"] <= CLOUD_CONCURRENCY


@pytest.mark.asyncio
async def test_a_hand_built_manager_reports_no_degradation():
    """`DeviceManager.__new__` instances (tests) must not explode here."""
    from micast.xiaomi.device_manager import DeviceManager

    assert DeviceManager.__new__(DeviceManager).cloud_degraded() is False
