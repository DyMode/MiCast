"""Device evidence, separate from protocol defaults and runtime availability."""

import logging
import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from micast.config_store import read_json, write_json

logger = logging.getLogger(__name__)
LEVELS = {"unknown": 0, "declared": 1, "accepted": 2, "pulled": 3, "confirmed": 4}
VERIFICATION_AGE = 30 * 86400
ACTIONS = (
    "play_file",
    "play_stream",
    "pause",
    "resume",
    "stop",
    "get_volume",
    "set_volume",
    "status",
)


class Evidence(BaseModel):
    channel: Literal["dlna", "cloud", "miio"]
    action: str
    format: str = ""
    level: Literal["unknown", "declared", "accepted", "pulled", "confirmed", "unsupported"]
    source: str = ""
    verified_at: float = 0
    pulled_at: float = 0
    confirmed_at: float = 0


class DeviceEvidence(BaseModel):
    id: str
    name: str = ""
    model: str = ""
    firmware: str = ""
    revision: int = 0
    records: dict[str, Evidence] = Field(default_factory=dict)


class CapabilityLedger:
    def __init__(self, path: Path | None = None, clock=time.time):
        self.path = path
        self.clock = clock
        self.devices: dict[str, DeviceEvidence] = {}
        if path is not None:
            data = read_json(path) or {}
            if (
                isinstance(data, dict)
                and data.get("version") in (1, 2)
                and isinstance(data.get("devices"), dict)
            ):
                for key, value in list(data.get("devices", {}).items())[:512]:
                    try:
                        item = DeviceEvidence.model_validate(value)
                        if item.id == key:
                            if data.get("version") == 1:
                                # v1 mixed length-bounded WAV tests with live streams.
                                # Their confirmations cannot prove endless WAV support.
                                item.records.pop(self.key("dlna", "play_stream", "WAV"), None)
                            self.devices[key] = item
                    except (ValidationError, TypeError):
                        continue

    def save(self):
        if self.path is None:
            return
        try:
            write_json(
                self.path,
                {
                    "version": 2,
                    "devices": {key: item.model_dump() for key, item in self.devices.items()},
                },
            )
        except OSError:
            # Evidence persistence must never break an otherwise healthy cast.
            logger.warning("设备能力记录保存失败，当前播放不受影响")

    def identify(self, device_id: str, *, name="", model="", firmware=""):
        name, model, firmware = (
            str(value)[:128] if value else "" for value in (name, model, firmware)
        )
        item = self.devices.get(device_id)
        before = item.model_dump() if item else None
        if item is None:
            item = self.devices[device_id] = DeviceEvidence(id=device_id)
        changed = (model and item.model and model != item.model) or (
            firmware and item.firmware and firmware != item.firmware
        )
        if changed:
            item.records.clear()
            item.revision += 1
        for key, value in (("name", name), ("model", model), ("firmware", firmware)):
            if value:
                setattr(item, key, str(value)[:128])
        if before != item.model_dump():
            self.save()
        return item

    @staticmethod
    def key(channel, action, format=""):
        return f"{channel}:{action}:{format.upper()}"

    def record(self, device_id, channel, action, level, *, format="", source=""):
        if action not in ACTIONS:
            raise ValueError("未知设备动作")
        item = self.devices.get(device_id) or self.identify(device_id)
        key = self.key(channel, action, format)
        previous = item.records.get(key)
        now = self.clock()
        if level == "declared" and previous is not None:
            return previous
        if (
            level == "accepted"
            and previous
            and previous.level in LEVELS
            and LEVELS[previous.level] >= LEVELS[level]
            and now - previous.verified_at < 60
        ):
            return previous
        if (
            previous is None
            or level == "unsupported"
            or (previous.level != "unsupported" and LEVELS[level] >= LEVELS[previous.level])
        ):
            item.records[key] = Evidence(
                channel=channel,
                action=action,
                format=format.upper(),
                level=level,
                source=source,
                verified_at=now,
                pulled_at=previous.pulled_at if previous else 0,
                confirmed_at=previous.confirmed_at if previous else 0,
            )
        elif previous.level == "unsupported" and level in ("accepted", "pulled", "confirmed"):
            previous.level, previous.source, previous.verified_at = level, source, now
        result = item.records[key]
        if level == "unsupported":
            result.pulled_at = result.confirmed_at = 0
        if level == "pulled":
            result.pulled_at = now
        if level == "confirmed":
            result.confirmed_at = now
        self.save()
        return result

    def confirmed(self, device_id, action, format=""):
        item = self.devices.get(device_id)
        record = item.records.get(self.key("dlna", action, format)) if item else None
        return bool(
            record
            and record.level == "confirmed"
            and record.confirmed_at > 0
            and 0 <= self.clock() - record.confirmed_at <= VERIFICATION_AGE
        )

    def confirm_audio(self, device_id, action, format, pulled_at, revision):
        item = self.devices.get(device_id)
        record = item.records.get(self.key("dlna", action, format)) if item else None
        if (
            item is None
            or item.revision != revision
            or record is None
            or record.level == "unsupported"
            or not record.pulled_at
            or record.pulled_at != pulled_at
            or not 0 <= self.clock() - record.pulled_at <= 86400
            or action not in ("play_file", "play_stream")
        ):
            raise ValueError("测试结果已变化或过期，请重新播放后确认")
        return self.record(
            device_id, "dlna", action, "confirmed", format=format, source="user_confirmation"
        )

    def describe(self, device_id, *, online=False, discovery_enabled=True):
        item = self.devices.get(device_id) or DeviceEvidence(id=device_id)
        data = item.model_dump()
        records = []
        for record in item.records.values():
            value = record.model_dump()
            verified_at = record.confirmed_at if record.level == "confirmed" else record.verified_at
            value["stale"] = bool(
                verified_at and not 0 <= self.clock() - verified_at <= VERIFICATION_AGE
            )
            value["can_confirm"] = bool(
                record.action in ("play_file", "play_stream")
                and record.level != "unsupported"
                and record.pulled_at > record.confirmed_at
                and 0 <= self.clock() - record.pulled_at <= 86400
            )
            records.append(value)
        data["records"] = records
        data["availability"] = "online" if online else "offline" if discovery_enabled else "paused"
        return data
