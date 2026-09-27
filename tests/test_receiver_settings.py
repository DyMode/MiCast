from micast.config import (
    AirPlay2InstanceConfig,
    ReceiverConfig,
    Settings,
    SpeakerConfig,
    SpeakerGroupConfig,
)


def test_enabling_speaker_creates_independent_receiver(tmp_path, monkeypatch):
    instance = Settings()
    monkeypatch.setattr(
        type(instance), "config_path", property(lambda self: tmp_path / "micast.json")
    )
    instance.receivers = [ReceiverConfig(id="main", name="MiCast")]
    instance.speakers = [SpeakerConfig(did="speaker-1", alias="客厅")]

    instance.set_enabled("speaker-1", True)

    receiver = next(item for item in instance.receivers if item.id == "speaker-speaker-1")
    assert receiver.name == "客厅"
    assert receiver.target_id == "speaker-1"
    assert receiver.enabled is True

    instance.set_enabled("speaker-1", False)
    assert receiver.enabled is False


def test_speaker_alias_renames_its_airplay_receiver(tmp_path, monkeypatch):
    instance = Settings()
    monkeypatch.setattr(
        type(instance), "config_path", property(lambda self: tmp_path / "micast.json")
    )
    instance.speakers = [SpeakerConfig(did="speaker-1", alias="客厅")]
    instance.receivers = [
        ReceiverConfig(id="living", name="旧名称", target_type="speaker", target_id="speaker-1")
    ]

    instance.set_alias("speaker-1", "客厅小爱")

    assert instance.receivers[0].name == "客厅小爱"


def test_receiver_names_are_migrated_from_targets():
    instance = Settings()
    instance.speakers = [SpeakerConfig(did="speaker-1", alias="客厅小爱")]
    instance.receivers = [
        ReceiverConfig(id="living", name="旧名称", target_type="speaker", target_id="speaker-1")
    ]

    instance._migrate_receivers()

    assert instance.receivers[0].name == "客厅小爱"


def test_airplay2_instance_resolves_its_own_targets_without_classic_receiver():
    instance = Settings()
    instance.airplay2_instances = [
        AirPlay2InstanceConfig(
            id="ap2-living",
            name="客厅 AirPlay 2",
            target_type="speaker",
            target_id="speaker-1",
        ),
        AirPlay2InstanceConfig(
            id="ap2-home",
            name="全屋 AirPlay 2",
            target_type="group",
            target_id="group-1",
        ),
    ]
    instance.groups = [
        SpeakerGroupConfig(id="group-1", name="全屋", speaker_ids=["speaker-1", "speaker-2"])
    ]

    assert instance.receiver_targets("ap2-living") == ["speaker-1"]
    assert instance.receiver_targets("ap2-home") == ["speaker-1", "speaker-2"]


def test_airplay2_group_instance_resolves_group_for_channel_and_variants():
    """group_for_receiver must also resolve AirPlay 2 instances (their receiver
    identity lives in airplay2_instances) so the stereo channel/EQ/variant
    lookups work for the AirPlay 2 → Xiaomi path."""
    instance = Settings()
    instance.airplay2_instances = [
        AirPlay2InstanceConfig(
            id="ap2-home",
            name="全屋 AirPlay 2",
            target_type="group",
            target_id="group-1",
        )
    ]
    instance.groups = [
        SpeakerGroupConfig(
            id="group-1",
            name="全屋",
            speaker_ids=["speaker-1", "speaker-2"],
            mode="stereo",
            channels={"speaker-1": "left", "speaker-2": "right"},
        )
    ]
    assert instance.group_for_receiver("ap2-home") is instance.groups[0]
    assert instance.receiver_channel("ap2-home", "speaker-1") == "left"
    assert instance.channel_suffix("ap2-home", "speaker-2") == "-R"
    assert {v["suffix"] for v in instance.receiver_stream_variants("ap2-home")} == {"-L", "-R"}


def test_device_id_change_migrates_every_speaker_reference(tmp_path, monkeypatch):
    instance = Settings()
    monkeypatch.setattr(
        type(instance), "config_path", property(lambda self: tmp_path / "micast.json")
    )
    instance.selected_device_id = "old-device-id"
    instance.speakers = [
        SpeakerConfig(
            did="old-device-id",
            alias="四楼小爱",
            enabled=True,
            miot_did="978430386",
            hardware="OH2",
        )
    ]
    instance.receivers = [
        ReceiverConfig(
            id="fourth",
            name="四楼小爱",
            target_type="speaker",
            target_id="old-device-id",
        )
    ]
    instance.groups = [
        SpeakerGroupConfig(
            id="all",
            name="组合",
            speaker_ids=["old-device-id", "other-device-id"],
            delays_ms={"old-device-id": 80},
            channels={"old-device-id": "left"},
            gains_db={"old-device-id": -1.0},
        )
    ]
    instance.airplay2_instances = [
        AirPlay2InstanceConfig(
            id="ap2",
            name="四楼 AirPlay 2",
            target_type="speaker",
            target_id="old-device-id",
        )
    ]

    instance.merge_speakers(
        [
            {
                "deviceID": "new-device-id",
                "miotDID": "978430386",
                "name": "四楼小爱",
                "hardware": "OH2",
            }
        ]
    )

    assert [item.did for item in instance.speakers] == ["new-device-id"]
    assert instance.selected_device_id == "new-device-id"
    assert instance.receivers[0].target_id == "new-device-id"
    assert instance.airplay2_instances[0].target_id == "new-device-id"
    assert instance.groups[0].speaker_ids == ["new-device-id", "other-device-id"]
    assert instance.groups[0].delays_ms == {"new-device-id": 80}
    assert instance.groups[0].channels == {"new-device-id": "left"}
    assert instance.groups[0].gains_db == {"new-device-id": -1.0}


def test_legacy_same_name_records_are_consolidated(tmp_path, monkeypatch):
    instance = Settings()
    monkeypatch.setattr(
        type(instance), "config_path", property(lambda self: tmp_path / "micast.json")
    )
    instance.selected_device_id = "stale-2"
    instance.speakers = [
        SpeakerConfig(did="current", alias="四楼小爱", enabled=False),
        SpeakerConfig(did="stale-1", alias="四楼小爱", enabled=False),
        SpeakerConfig(did="stale-2", alias="四楼小爱", enabled=True),
    ]
    instance.receivers = [
        ReceiverConfig(
            id="fourth",
            name="四楼小爱",
            target_type="speaker",
            target_id="stale-2",
        )
    ]

    instance.merge_speakers(
        [
            {
                "deviceID": "current",
                "miotDID": "978430386",
                "name": "四楼小爱",
                "hardware": "OH2",
            }
        ]
    )

    assert len(instance.speakers) == 1
    assert instance.speakers[0].did == "current"
    assert instance.speakers[0].enabled is True
    assert instance.speakers[0].miot_did == "978430386"
    assert instance.selected_device_id == "current"
    assert instance.receivers[0].target_id == "current"


def test_account_switch_clears_only_provider_owned_topology(tmp_path, monkeypatch):
    instance = Settings()
    monkeypatch.setattr(
        type(instance), "config_path", property(lambda self: tmp_path / "micast.json")
    )
    instance.provider_account_id = "old-account"
    instance.selected_device_id = "speaker-1"
    instance.speakers = [SpeakerConfig(did="speaker-1", alias="旧音箱", enabled=True)]
    instance.receivers = [
        ReceiverConfig(
            id="speaker",
            name="旧音箱",
            target_type="speaker",
            target_id="speaker-1",
        )
    ]
    instance.groups = [SpeakerGroupConfig(id="group", name="旧组合", speaker_ids=["speaker-1"])]
    instance.airplay2_instances = [
        AirPlay2InstanceConfig(
            id="ap2",
            name="旧入口",
            target_type="speaker",
            target_id="speaker-1",
        )
    ]

    changed = instance.bind_provider_account("new-account")

    assert changed is True
    assert instance.provider_account_id == "new-account"
    assert instance.selected_device_id is None
    assert instance.speakers == []
    assert instance.groups == []
    assert instance.airplay2_instances == []
    assert instance.receivers == []


def test_logout_or_temporary_disconnect_keeps_account_devices(tmp_path, monkeypatch):
    instance = Settings()
    monkeypatch.setattr(
        type(instance), "config_path", property(lambda self: tmp_path / "micast.json")
    )
    instance.provider_account_id = "same-account"
    instance.selected_device_id = "speaker-1"
    instance.speakers = [SpeakerConfig(did="speaker-1", alias="客厅", enabled=True)]
    instance.receivers = [
        ReceiverConfig(
            id="living",
            name="客厅",
            target_type="speaker",
            target_id="speaker-1",
        )
    ]

    assert instance.bind_provider_account(None) is False
    assert instance.provider_account_id == "same-account"
    assert instance.selected_device_id == "speaker-1"
    assert [item.did for item in instance.speakers] == ["speaker-1"]
    assert instance.receivers[0].target_id == "speaker-1"

    # A successful but temporarily empty discovery result also keeps persisted
    # devices; only positive identity matches are allowed to migrate records.
    instance.merge_speakers([])
    assert [item.did for item in instance.speakers] == ["speaker-1"]
