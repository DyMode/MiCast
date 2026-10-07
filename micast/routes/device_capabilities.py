"""Persistent evidence and explicitly associated playback control channels."""

import asyncio

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from micast.config import settings
from micast.config_apply import apply_config_transaction
from micast.device_capabilities import ACTIONS


class Confirmation(BaseModel):
    device_id: str
    action: str
    format: str
    pulled_at: float
    revision: int


class Policy(BaseModel):
    policy: str
    local_target_id: str | None = None


def install(bridge, device_manager=None):
    router = APIRouter(prefix="/api/capabilities", tags=["capabilities"])

    @router.get("")
    async def capabilities():
        ledger = bridge.capabilities
        discovery = bridge.dlna_discovery
        devices = discovery.devices() if discovery else []
        online = {device.id: device for device in devices}
        for device in devices:
            key = f"dlna:{device.id}"
            ledger.identify(key, name=device.name, model=device.model, firmware=device.firmware)
            if device.control_url:
                for action in ("play_file", "play_stream", "stop", "status"):
                    ledger.record(key, "dlna", action, "declared", source="AVTransport")
            if device.rendering_url:
                for action in ("get_volume", "set_volume"):
                    ledger.record(key, "dlna", action, "declared", source="RenderingControl")
        # Use existing account cache only: opening this panel must not log in,
        # refresh account credentials, or send status probes to speakers.
        cloud_ids = set()
        for device in getattr(device_manager, "_devices", []):
            did = device.get("deviceID")
            if not did:
                continue
            key = f"xiaomi:{did}"
            cloud_ids.add(key)
            ledger.identify(key, name=device.get("name", ""), model=device.get("hardware", ""))
            for action in (
                "play_file",
                "play_stream",
                "stop",
                "status",
                "get_volume",
                "set_volume",
            ):
                ledger.record(key, "cloud", action, "declared", source="MiNA_adapter")
        entries = []
        for kind, definitions in (
            ("classic", settings.receivers),
            ("airplay2", settings.airplay2_instances),
        ):
            for entry in definitions:
                if entry.target_type not in ("speaker", "dlna"):
                    continue
                entries.append(
                    {
                        "id": entry.id,
                        "kind": kind,
                        "name": entry.name,
                        "target_type": entry.target_type,
                        "policy": entry.control_policy,
                        "local_target_id": entry.local_target_id,
                        "route": bridge.resolve_control_route(entry.id).snapshot(),
                    }
                )
        result = {
            "devices": [
                ledger.describe(
                    key,
                    online=bool(
                        online.get(key.removeprefix("dlna:"))
                        and online[key.removeprefix("dlna:")].online
                    ),
                    discovery_enabled=settings.network_discovery_enabled,
                )
                for key in ledger.devices
                if not key.startswith("xiaomi:") or key in cloud_ids
            ],
            "entries": entries,
            "runtime": bridge.diagnostics.get("dlna_targets", {}),
        }
        for device in result["devices"]:
            if device["id"].startswith("xiaomi:"):
                device["availability"] = "unknown"
            present = {record["action"] for record in device["records"]}
            for action in ACTIONS:
                if action not in present:
                    device["records"].append(
                        {
                            "action": action,
                            "format": "",
                            "level": "unknown",
                            "stale": False,
                            "can_confirm": False,
                            "pulled_at": 0,
                            "verified_at": 0,
                        }
                    )
        return result

    @router.post("/confirm")
    async def confirm(payload: Confirmation):
        device = (
            bridge.dlna_discovery.resolve(payload.device_id.removeprefix("dlna:"))
            if bridge.dlna_discovery and payload.device_id.startswith("dlna:")
            else None
        )
        if device:
            bridge.capabilities.identify(
                payload.device_id, name=device.name, model=device.model, firmware=device.firmware
            )
        try:
            bridge.capabilities.confirm_audio(**payload.model_dump())
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"ok": True}

    @router.post("/retry/{entry_id}")
    async def retry(entry_id: str):
        if (
            bridge.resolve_control_route(entry_id).channel != "dlna"
            or not bridge.dlna_target_manager
        ):
            raise HTTPException(409, "当前入口没有本地播放")
        session = bridge.sessions.current(entry_id)
        if session is None or not bridge.sessions.valid(session.token):
            raise HTTPException(409, "当前入口未播放，请重新连接 AirPlay")
        if bridge.recovery.busy(entry_id):
            raise HTTPException(409, "当前入口正在恢复，请稍后重试")

        async def work():
            await bridge.dlna_target_manager.retry_owned(entry_id)
            return True

        try:
            if await bridge.recovery.run(entry_id, "dlna_stream", work) is not True:
                raise HTTPException(409, "当前入口已停止或正在恢复，请稍后重试")
        except asyncio.CancelledError as exc:
            if not bridge.sessions.valid(session.token):
                raise HTTPException(409, "播放已停止或被新的播放接管") from exc
            raise
        except HTTPException:
            raise
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(502, "本地重试失败，请检查设备连接") from exc
        return {"ok": True}

    @router.patch("/{kind}/{entry_id}")
    async def policy(kind: str, entry_id: str, payload: Policy):
        if kind not in ("classic", "airplay2") or payload.policy not in (
            "legacy",
            "auto",
            "local",
            "cloud",
        ):
            raise HTTPException(400, "无效控制策略")

        def mutate():
            definitions = settings.receivers if kind == "classic" else settings.airplay2_instances
            entry = next((entry for entry in definitions if entry.id == entry_id), None)
            if entry is None:
                raise HTTPException(404, "入口不存在")
            if entry.target_type != "speaker":
                raise HTTPException(409, "直接桥接入口始终使用本地控制")
            if bridge.sessions.current(entry_id) or bridge.sessions.current(f"dlna:{entry_id}"):
                raise HTTPException(409, "请先停止该入口播放，再修改控制策略")
            target = payload.local_target_id
            if payload.policy == "local" and not target:
                raise HTTPException(400, "仅本地需要明确关联 DLNA 设备")
            device = (
                bridge.dlna_discovery.resolve(target) if target and bridge.dlna_discovery else None
            )
            if target and (device is None or not device.control_url):
                raise HTTPException(409, "请从已发现的 DLNA 设备中明确选择目标")
            entry.control_policy, entry.local_target_id = payload.policy, target
            settings.save_to_file()

        await apply_config_transaction(mutate, bridge.apply_config_change)
        return {"ok": True}

    return router
