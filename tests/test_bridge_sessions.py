"""Live sender sessions in bridge diagnostics, split by ingress.

AirPlay 2 runs on shairport + PCM sources and never opens a RAOP session, so the
RAOP counters alone made the diagnostics page report an idle input while an
AirPlay 2 sender was playing.
"""

from micast.audio_bridge import AudioBridge
from micast.config import AirPlay2InstanceConfig, ReceiverConfig, settings


def _bridge(monkeypatch, receivers: list[str], instances: list[str]) -> AudioBridge:
    monkeypatch.setattr(
        settings, "receivers", [ReceiverConfig(id=rid, name=rid) for rid in receivers]
    )
    monkeypatch.setattr(
        settings,
        "airplay2_instances",
        [
            AirPlay2InstanceConfig(id=i, name=i, target_type="speaker", target_id="r1")
            for i in instances
        ],
    )
    return AudioBridge()


def test_diagnostics_split_classic_and_airplay2_sessions(monkeypatch):
    bridge = _bridge(monkeypatch, ["r1"], ["ap2"])
    bridge._active_sessions.update({"r1", "ap2"})

    assert bridge.diagnostics["sessions"] == {
        "active": ["ap2", "r1"],
        "classic": ["r1"],
        "airplay2": ["ap2"],
    }


def test_diagnostics_report_no_sessions_while_idle(monkeypatch):
    bridge = _bridge(monkeypatch, ["r1"], ["ap2"])

    assert bridge.diagnostics["sessions"] == {"active": [], "classic": [], "airplay2": []}


def test_airplay2_session_outliving_its_config_still_reads_as_airplay2(monkeypatch):
    """A removed or disabled instance keeps its running session classified by the
    runtime maps, so the input card does not fall back to an idle reading."""
    bridge = _bridge(monkeypatch, ["r1"], [])
    bridge._airplay2_runtime["ap2"] = {"id": "ap2", "status": "running", "detail": ""}
    bridge._active_sessions.add("ap2")

    sessions = bridge.diagnostics["sessions"]

    assert sessions["airplay2"] == ["ap2"] and sessions["classic"] == []


def test_status_payload_carries_the_session_split(monkeypatch):
    """The UI (poll and WebSocket) reads diagnostics straight off bridge.status."""
    bridge = _bridge(monkeypatch, ["r1"], ["ap2"])
    bridge._active_sessions.add("ap2")

    assert bridge.status["diagnostics"]["sessions"]["airplay2"] == ["ap2"]
