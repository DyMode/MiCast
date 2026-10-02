"""Versioned public projection of sessions, target ownership and capabilities."""

import json
import uuid

from micast.capabilities import output_kind, playback_capabilities


class RuntimeSnapshot:
    def __init__(self):
        self.revision = 0
        self.epoch = uuid.uuid4().hex
        self._fingerprint = ""
        self.sequence = 0
        self.target_capabilities = lambda target: {}

    def project(self, sessions):
        protocols = {item["owner"]: item["protocol"] for item in sessions.snapshot()}
        payload = {
            "sessions": [
                {**item, "capabilities": playback_capabilities(item["protocol"])}
                for item in sessions.snapshot()
            ],
            "targets": [
                {
                    **item,
                    "output": output_kind(item["target"]),
                    "capabilities": playback_capabilities(
                        protocols.get(item["owner"], "airplay"), output_kind(item["target"])
                    ) | self.target_capabilities(item["target"]),
                }
                for item in sessions.targets.snapshot()
            ],
        }
        signature = json.dumps(payload, sort_keys=True)
        if signature != self._fingerprint:
            self.revision += 1
            self._fingerprint = signature
        self.sequence += 1
        return {
            "epoch": self.epoch, "revision": self.revision,
            "sequence": self.sequence, **payload,
        }
