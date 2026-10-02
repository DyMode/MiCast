"""Topology snapshots and device doubles shared by EQ and topology tests."""

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
