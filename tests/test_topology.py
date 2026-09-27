"""Topology snapshot builder: single target, mirror groups, stereo pairs."""

import pytest

from micast import topology
from micast.config import AirPlay2InstanceConfig, ReceiverConfig, Settings, SpeakerGroupConfig


class FakeBridge:
    def __init__(
        self,
        diagnostics: dict,
        receivers: list[dict] | None = None,
        airplay2_instances: list[dict] | None = None,
    ):
        self._diagnostics = diagnostics
        self._receivers = receivers or []
        self._airplay2_instances = airplay2_instances or []

    @property
    def diagnostics(self):
        return self._diagnostics

    @property
    def status(self):
        return {
            "status": "running",
            "receivers": self._receivers,
            "airplay2_instances": self._airplay2_instances,
        }


class FakeDeviceManager:
    def __init__(self, playing=(), paused=(), owners=None):
        self._playing = set(playing)
        self._paused = set(paused)
        self._owners = owners or {}

    def is_playing(self, did):
        return did in self._playing

    def is_paused(self, did):
        return did in self._paused

    def is_enabled(self, did):
        return True

    def get_alias(self, did):
        return f"音箱-{did}"

    def owner_of(self, did):
        return self._owners.get(did)


def _latency(encoding=26, buffer=250, queue=4):
    return {
        "encoding_ms": encoding,
        "stream_buffer_ms": buffer,
        "send_queue_ms": queue,
        "estimated_ms": encoding + buffer + queue,
    }


def _raop_diag(rid="r1", sessions=1, input_buffer_ms=20):
    return {
        rid: {
            "active_sessions": sessions,
            "input_buffer_ms": input_buffer_ms,
            "dropped_packets": 0,
            "decode_errors": 0,
            "resend_requests": 0,
        }
    }


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
