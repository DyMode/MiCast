"""Choose a playback route once, before commands, from explicit associations."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ControlRoute:
    channel: str
    target_id: str | None
    reason: str

    def snapshot(self):
        return {"channel": self.channel, "target_id": self.target_id, "reason": self.reason}


def select_route(entry, discovery, ledger, format):
    if entry is None or entry.target_type not in ("speaker", "dlna"):
        return ControlRoute("legacy", None, "沿用原播放配置")
    if entry.target_type == "dlna":
        return ControlRoute("dlna", entry.target_id, "直接本地 DLNA 绑定")
    policy = entry.control_policy
    target = entry.local_target_id
    if policy in ("legacy", "cloud"):
        return ControlRoute("cloud", entry.target_id, "保持米家云端控制")
    if policy == "local":
        return ControlRoute(
            "dlna" if target else "blocked",
            target,
            "用户选择仅本地" if target else "仅本地策略尚未关联 DLNA 设备",
        )
    device = discovery.resolve(target) if discovery and target else None
    if device and ledger:
        ledger.identify(
            f"dlna:{target}",
            name=device.name,
            model=device.model,
            firmware=getattr(device, "firmware", ""),
        )
    if (
        device
        and device.online
        and ledger
        and ledger.confirmed(f"dlna:{target}", "play_stream", format)
    ):
        return ControlRoute("dlna", target, "本地持续流已确认出声，优先使用本地")
    return ControlRoute("cloud", entry.target_id, "本地当前格式尚未确认或设备离线，使用云端")
