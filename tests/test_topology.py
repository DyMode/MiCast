"""Topology snapshot builder: single target, mirror groups, stereo pairs."""

import pytest
from support.topology import FakeBridge, FakeDeviceManager, _latency, _raop_diag

from micast import topology
from micast.config import AirPlay2InstanceConfig, ReceiverConfig, Settings, SpeakerGroupConfig


@pytest.fixture(autouse=True)
def fake_settings(monkeypatch):
    monkeypatch.setattr(Settings, "save_to_file", lambda self: None)
    fake = Settings()
    monkeypatch.setattr(topology, "settings", fake)
    return fake


def _edges_by_direction(snapshot, direction):
    return [e for e in snapshot["edges"] if e.get("direction") == direction]


def test_single_speaker_path(fake_settings):
    fake_settings.receivers = [
        ReceiverConfig(id="r1", name="客厅", target_type="speaker", target_id="didA")
    ]
    bridge = FakeBridge(
        {
            "raop": _raop_diag(),
            "streams": {
                "r1": {
                    "clients": 1,
                    "flowing": True,
                    "bytes_sent": 100,
                    "dropped_chunks": 0,
                    "latency": _latency(),
                }
            },
        }
    )
    dm = FakeDeviceManager(playing=["didA"])

    snap = topology.build_topology(bridge, dm)

    ids = {n["id"] for n in snap["nodes"]}
    assert {"src:r1", "engine:r1", "pipe:r1", "stream:r1", "cloud:xiaomi", "spk:didA"} <= ids
    engine = next(n for n in snap["nodes"] if n["id"] == "engine:r1")
    assert engine["label"] == "客厅"  # the entry name lives on the engine node

    handoff = next(e for e in snap["edges"] if e["from"] == "engine:r1")
    assert handoff["to"] == "pipe:r1" and handoff["protocol"] == "PCM"
    encode = next(e for e in snap["edges"] if e["from"] == "pipe:r1")
    assert encode["to"] == "stream:r1"
    assert encode["protocol"] == "MP3"
    assert encode["latency_ms"] == 26  # encoding latency
    pull = _edges_by_direction(snap, "pull")
    assert len(pull) == 1
    assert pull[0]["from"] == "stream:r1" and pull[0]["to"] == "spk:didA"
    assert pull[0]["latency_ms"] == 254  # stream_buffer + send_queue
    assert pull[0]["active"] is True
    assert pull[0]["estimated"] is True

    push = _edges_by_direction(snap, "push")
    assert push[0]["latency_ms"] == 20  # RAOP input buffer

    speaker = next(n for n in snap["nodes"] if n["id"] == "spk:didA")
    assert speaker["status"] == "playing"
    control = [e for e in snap["edges"] if e["to"] == "spk:didA" and e["direction"] == "control"]
    assert control and control[0]["active"] is True


def test_shared_stream_does_not_claim_an_idle_target_is_receiving(fake_settings):
    fake_settings.receivers = [ReceiverConfig(id="r", name="r", target_type="group", target_id="g")]
    fake_settings.groups = [SpeakerGroupConfig(id="g", name="g", speaker_ids=["a", "b"])]
    bridge = FakeBridge({
        "streams": {"r": {"flowing": True, "clients": 2}},
        "sinks": {"r": {"a": {"flowing": True, "clients": 1},
                          "b": {"flowing": False, "clients": 1}}},
    })
    snapshot = topology.build_topology(bridge, FakeDeviceManager(playing=["a", "b"]))
    edges = {edge["to"]: edge for edge in snapshot["edges"] if edge["direction"] == "pull"}
    assert edges["spk:a"]["active"]
    assert not edges["spk:b"]["active"] and edges["spk:b"]["stalled"]


def test_mirror_group_fans_out_one_stream_to_many_speakers(fake_settings):
    fake_settings.groups = [
        SpeakerGroupConfig(
            id="g1", name="全屋", speaker_ids=["didA", "didB"], delays_ms={"didB": 30}
        )
    ]
    fake_settings.receivers = [
        ReceiverConfig(id="r1", name="全屋", target_type="group", target_id="g1")
    ]
    bridge = FakeBridge(
        {
            "raop": _raop_diag(),
            "streams": {
                "r1": {
                    "clients": 2,
                    "flowing": True,
                    "bytes_sent": 0,
                    "dropped_chunks": 0,
                    "latency": _latency(),
                }
            },
        }
    )
    dm = FakeDeviceManager(playing=["didA", "didB"])

    snap = topology.build_topology(bridge, dm)

    stream_nodes = [n for n in snap["nodes"] if n["kind"] == "stream"]
    assert len(stream_nodes) == 1  # mirror: shared stream

    pull = _edges_by_direction(snap, "pull")
    assert {e["to"] for e in pull} == {"spk:didA", "spk:didB"}
    didB_edge = next(e for e in pull if e["to"] == "spk:didB")
    assert didB_edge["compensation_ms"] == 30


def test_stereo_group_splits_into_channel_streams(fake_settings):
    fake_settings.groups = [
        SpeakerGroupConfig(
            id="g1",
            name="立体声",
            speaker_ids=["didA", "didB"],
            mode="stereo",
            channels={"didA": "left", "didB": "right"},
        )
    ]
    fake_settings.receivers = [
        ReceiverConfig(id="r1", name="立体声", target_type="group", target_id="g1")
    ]
    bridge = FakeBridge(
        {
            "raop": _raop_diag(),
            "streams": {
                "r1-L": {
                    "clients": 1,
                    "flowing": True,
                    "bytes_sent": 0,
                    "dropped_chunks": 0,
                    "latency": _latency(),
                },
                "r1-R": {
                    "clients": 1,
                    "flowing": True,
                    "bytes_sent": 0,
                    "dropped_chunks": 0,
                    "latency": _latency(),
                },
            },
        }
    )
    dm = FakeDeviceManager(playing=["didA", "didB"])

    snap = topology.build_topology(bridge, dm)

    stream_ids = {n["id"] for n in snap["nodes"] if n["kind"] == "stream"}
    # One stream per channel: the un-split base mix has no consumer here, so
    # it is not published (see Settings.needs_plain_base).
    assert stream_ids == {"stream:r1-L", "stream:r1-R"}

    pull = _edges_by_direction(snap, "pull")
    routes = {(e["from"], e["to"]) for e in pull}
    assert routes == {("stream:r1-L", "spk:didA"), ("stream:r1-R", "spk:didB")}


def test_inactive_when_no_source_session_and_no_clients(fake_settings):
    fake_settings.receivers = [
        ReceiverConfig(id="r1", name="客厅", target_type="speaker", target_id="didA")
    ]
    bridge = FakeBridge(
        {
            "raop": _raop_diag(sessions=0),
            "streams": {
                "r1": {
                    "clients": 0,
                    "flowing": False,
                    "bytes_sent": 0,
                    "dropped_chunks": 0,
                    "latency": _latency(),
                }
            },
        }
    )
    dm = FakeDeviceManager()

    snap = topology.build_topology(bridge, dm)

    assert all(not e.get("active", True) for e in _edges_by_direction(snap, "pull"))
    assert all(not e.get("active", True) for e in _edges_by_direction(snap, "push"))
    speaker = next(n for n in snap["nodes"] if n["id"] == "spk:didA")
    assert speaker["status"] == "idle"


def test_passthrough_skips_the_transcoder(fake_settings):
    """Raw PCM output: no pipeline node, engine links straight to the stream."""
    fake_settings.audio.auto_transcode = False
    fake_settings.receivers = [
        ReceiverConfig(id="r1", name="客厅", target_type="speaker", target_id="didA")
    ]
    bridge = FakeBridge(
        {
            "raop": _raop_diag(),
            "streams": {
                "r1": {
                    "clients": 1,
                    "flowing": True,
                    "bytes_sent": 0,
                    "dropped_chunks": 0,
                    "latency": _latency(),
                }
            },
        }
    )
    snap = topology.build_topology(bridge, FakeDeviceManager(playing=["didA"]))

    ids = {n["id"] for n in snap["nodes"]}
    assert "pipe:r1" not in ids
    encode = next(e for e in snap["edges"] if e["from"] == "engine:r1")
    assert encode["to"] == "stream:r1"
    assert encode["protocol"] == "PCM 直出"


def test_no_speakers_no_cloud_node(fake_settings):
    fake_settings.receivers = [ReceiverConfig(id="r1", name="未绑定")]
    bridge = FakeBridge({"raop": _raop_diag(sessions=0), "streams": {}})
    snap = topology.build_topology(bridge, FakeDeviceManager())
    assert "cloud:xiaomi" not in {n["id"] for n in snap["nodes"]}


def test_unmapped_airplay2_target_is_not_rendered_as_speaker(fake_settings):
    fake_settings.airplay2_instances = [
        AirPlay2InstanceConfig(
            id="airplay2",
            name="MiCast",
            target_type="speaker",
            target_id="unmapped",
            enabled=True,
        )
    ]
    bridge = FakeBridge({"raop": {}, "streams": {}})
    snap = topology.build_topology(bridge, FakeDeviceManager())
    assert "spk:unmapped" not in {node["id"] for node in snap["nodes"]}


def test_airplay2_path_uses_live_stream_and_owner_as_session(fake_settings):
    fake_settings.airplay2_instances = [
        AirPlay2InstanceConfig(
            id="airplay2", name="MiCast", target_type="speaker", target_id="didA"
        )
    ]
    bridge = FakeBridge(
        {
            "raop": {},
            "streams": {
                "airplay2": {
                    "clients": 1,
                    "flowing": True,
                    "bytes_sent": 100,
                    "dropped_chunks": 0,
                    "latency": _latency(),
                }
            },
        },
        airplay2_instances=[{"id": "airplay2", "status": "running", "detail": "运行正常"}],
    )
    dm = FakeDeviceManager(playing=["didA"], owners={"didA": "airplay2"})

    snap = topology.build_topology(bridge, dm)

    ids = {node["id"] for node in snap["nodes"]}
    assert {"src:airplay2", "engine:airplay2", "stream:airplay2", "spk:didA"} <= ids
    source = next(node for node in snap["nodes"] if node["id"] == "src:airplay2")
    assert source["protocol"] == "AirPlay 2"
    assert source["active"] is True
