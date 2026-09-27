"""AirPlay 2 variant-plan tests: single and orchestrated deployments share the
same (channel, EQ) split, so a stereo-group target must fan out everywhere."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from micast.audio_bridge import AudioBridge
from micast.config import AirPlay2InstanceConfig, Settings, SpeakerGroupConfig


def _settings_with(
    instance_cfg: AirPlay2InstanceConfig, groups: list[SpeakerGroupConfig]
) -> Settings:
    instance = Settings()
    instance.airplay2_instances = [instance_cfg]
    instance.groups = groups
    return instance


def test_variant_plan_stereo_group_splits_channels(monkeypatch):
    cfg = AirPlay2InstanceConfig(id="ap2", name="MiCast", target_type="group", target_id="g1")
    monkeypatch.setattr(
        "micast.audio_bridge.settings",
        _settings_with(
            cfg,
            [
                SpeakerGroupConfig(
                    id="g1",
                    name="立体声",
                    speaker_ids=["a", "b"],
                    mode="stereo",
                    channels={"a": "left", "b": "right"},
                )
            ],
        ),
    )
    bridge = object.__new__(AudioBridge)

    group, stereo, variants = bridge._airplay2_variant_plan(cfg)

    assert stereo and group is not None and group.id == "g1"
    # Two members, one channel each, nothing external: the un-split base mix
    # has no consumer, so it is not published (it used to be a third encoder).
    assert {v["suffix"] for v in variants} == {"-L", "-R"}


def test_variant_plan_mirror_group_stays_single_stream(monkeypatch):
    cfg = AirPlay2InstanceConfig(id="ap2", name="MiCast", target_type="group", target_id="g1")
    monkeypatch.setattr(
        "micast.audio_bridge.settings",
        _settings_with(cfg, [SpeakerGroupConfig(id="g1", name="全屋", speaker_ids=["a", "b"])]),
    )
    bridge = object.__new__(AudioBridge)

    _, stereo, variants = bridge._airplay2_variant_plan(cfg)

    assert not stereo
    assert [v["suffix"] for v in variants] == [""]


def test_variant_plan_speaker_target_stays_single_stream(monkeypatch):
    cfg = AirPlay2InstanceConfig(id="ap2", name="MiCast", target_type="speaker", target_id="a")
    monkeypatch.setattr("micast.audio_bridge.settings", _settings_with(cfg, []))
    bridge = object.__new__(AudioBridge)

    group, stereo, variants = bridge._airplay2_variant_plan(cfg)

    assert group is None and not stereo
    assert [v["suffix"] for v in variants] == [""]


@pytest.mark.asyncio
async def test_group_topology_change_restarts_only_mapped_airplay2(monkeypatch):
    mapped = AirPlay2InstanceConfig(
        id="ap2-group", name="MiCast", target_type="group", target_id="g1"
    )
    other = AirPlay2InstanceConfig(
        id="ap2-other", name="书房", target_type="speaker", target_id="c"
    )
    cfg = _settings_with(mapped, [SpeakerGroupConfig(id="g1", name="组合", speaker_ids=["a", "b"])])
    cfg.airplay2_enabled = True
    cfg.airplay2_instances.append(other)
    monkeypatch.setattr("micast.audio_bridge.settings", cfg)

    bridge = object.__new__(AudioBridge)
    bridge._restart_lock = asyncio.Lock()
    bridge._stop_airplay2_pipeline = AsyncMock()
    bridge._start_airplay2_pipelines = AsyncMock()

    await bridge.rebuild_airplay2_group("g1")

    bridge._stop_airplay2_pipeline.assert_awaited_once_with("ap2-group", keep_source=False)
    bridge._start_airplay2_pipelines.assert_awaited_once()


@pytest.mark.asyncio
async def test_eq_change_can_rebuild_multiple_mapped_groups_in_one_pass(monkeypatch):
    cfg = Settings()
    cfg.airplay2_enabled = True
    cfg.groups = [
        SpeakerGroupConfig(id="g1", name="一组", speaker_ids=["a", "b"]),
        SpeakerGroupConfig(id="g2", name="二组", speaker_ids=["a", "c"]),
    ]
    cfg.airplay2_instances = [
        AirPlay2InstanceConfig(id="ap2-a", name="一组", target_type="group", target_id="g1"),
        AirPlay2InstanceConfig(id="ap2-b", name="二组", target_type="group", target_id="g2"),
        AirPlay2InstanceConfig(id="ap2-c", name="单箱", target_type="speaker", target_id="a"),
    ]
    monkeypatch.setattr("micast.audio_bridge.settings", cfg)

    bridge = object.__new__(AudioBridge)
    bridge._restart_lock = asyncio.Lock()
    bridge._stop_airplay2_pipeline = AsyncMock()
    bridge._start_airplay2_pipelines = AsyncMock()

    await bridge.rebuild_airplay2_groups({"g1", "g2"})

    stopped = {call.args[0] for call in bridge._stop_airplay2_pipeline.await_args_list}
    assert stopped == {"ap2-a", "ap2-b"}
    bridge._start_airplay2_pipelines.assert_awaited_once()
