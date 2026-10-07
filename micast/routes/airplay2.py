"""AirPlay 2 management model and API."""

import logging

from fastapi import APIRouter, HTTPException

from micast.audio_bridge import AudioBridge
from micast.config import settings
from micast.config_apply import apply_config_transaction
from micast.deployment import airplay2_available, airplay2_mode
from micast.orchestration import OrchestratorClient

router = APIRouter(prefix="/api/airplay2", tags=["airplay2"])
logger = logging.getLogger(__name__)


def install(bridge: AudioBridge) -> APIRouter:
    def target_name(target_type: str, target_id: str | None) -> str:
        if target_type == "speaker":
            speaker = next((item for item in settings.speakers if item.did == target_id), None)
            return (speaker.alias if speaker else "") or "未找到音箱"
        if target_type == "group":
            group = next((item for item in settings.groups if item.id == target_id), None)
            return group.name if group else "未找到音箱组合"
        if target_type == "dlna":
            device = bridge.dlna_discovery.resolve(target_id) if bridge.dlna_discovery else None
            saved = next(
                (
                    item
                    for item in [*settings.receivers, *settings.airplay2_instances]
                    if item.target_type == "dlna" and item.target_id == target_id
                ),
                None,
            )
            return device.name if device else (saved.target_name if saved else "离线 DLNA 设备")
        return "当前选择的音箱"

    @router.get("")
    async def get_airplay2_state():
        runtime = {item["id"]: item for item in bridge.status.get("airplay2_instances", [])}
        instances = []
        for instance in settings.airplay2_instances:
            live = runtime.get(instance.id, {})
            live_status = live.get("status", "configured" if instance.enabled else "stopped")
            if live_status == "error":
                live_detail = "播放入口启动失败，请检查内部编排服务"
            else:
                live_detail = live.get("detail") or (
                    "可连接"
                    if live_status == "running"
                    else "正在启动"
                    if instance.enabled
                    else "已停用"
                )
            instances.append(
                {
                    "id": instance.id,
                    "name": instance.name,
                    "enabled": instance.enabled,
                    "status": live_status,
                    "detail": live_detail,
                    "target_type": instance.target_type,
                    "target_id": instance.target_id,
                    "target_name": target_name(instance.target_type, instance.target_id),
                }
            )

        mode = airplay2_mode()
        if not airplay2_available():
            orchestration = {
                "available": False,
                "status": "unavailable",
                "detail": "当前安装方式不支持 AirPlay 2",
            }
        elif not settings.airplay2_enabled:
            orchestration = {
                "available": True,
                "status": "disabled",
                "detail": "已关闭",
            }
        elif mode == "single":
            live = next(iter(runtime.values()), {})
            live_status = live.get("status", "starting")
            orchestration = {
                "available": True,
                "status": (
                    "running"
                    if live_status == "running"
                    else "error"
                    if live_status == "error"
                    else "disabled"
                ),
                "detail": live.get("detail")
                or ("运行正常" if live_status == "running" else "正在启动"),
            }
        elif settings.orchestrator_url and settings.orchestrator_token:
            try:
                await OrchestratorClient(
                    settings.orchestrator_url, settings.orchestrator_token
                ).health()
                orchestration = {
                    "available": True,
                    "status": "running",
                    "detail": "运行正常",
                }
            except Exception as exc:
                orchestration = {
                    "available": True,
                    "status": "error",
                    "detail": f"连接失败：{exc}",
                }
        else:
            orchestration = {
                "available": True,
                "status": "error",
                "detail": "AirPlay 2 服务尚未准备好",
            }
        return {
            "enabled": settings.airplay2_enabled,
            "mode": mode,
            "can_add_instances": mode == "multi",
            "orchestration": orchestration,
            "instances": instances,
            "summary": {
                "instances_running": sum(item["status"] == "running" for item in instances),
                "instances_total": len(instances),
                "mappings_healthy": sum(bool(item["target_id"]) for item in instances),
                "mappings_total": len(instances),
            },
            "targets": [
                *[
                    {"type": "speaker", "id": item.did, "name": item.alias or item.did}
                    for item in settings.speakers
                ],
                *[
                    {"type": "dlna", "id": item.id, "name": item.name}
                    for item in (bridge.dlna_discovery.devices() if bridge.dlna_discovery else [])
                    if item.online and item.control_url
                ],
                *[
                    {"type": "dlna", "id": item.target_id, "name": item.target_name or item.name}
                    for item in [*settings.receivers, *settings.airplay2_instances]
                    if item.target_type == "dlna"
                    and not (
                        bridge.dlna_discovery and bridge.dlna_discovery.resolve(item.target_id)
                    )
                ],
                *[{"type": "group", "id": item.id, "name": item.name} for item in settings.groups],
            ],
        }

    @router.post("/instances")
    async def upsert_instance(payload: dict):
        mode = airplay2_mode()
        if not airplay2_available():
            raise HTTPException(status_code=409, detail="当前安装方式不支持 AirPlay 2")
        instance_id = str(payload.get("id")) if payload.get("id") else None
        if mode == "single" and instance_id not in {"airplay2"}:
            raise HTTPException(status_code=409, detail="当前安装方式仅提供一个 AirPlay 2 入口")
        name = str(payload.get("name", "")).strip()
        target_type = str(payload.get("target_type", "")).strip()
        target_id = str(payload.get("target_id", "")).strip()
        if not name or len(name) > 50:
            raise HTTPException(status_code=400, detail="请输入 1–50 个字符的实例名称")
        if len([item for item in settings.airplay2_instances if item.id != instance_id]) >= 32:
            raise HTTPException(status_code=409, detail="最多支持 32 个播放入口")
        if target_type not in ("speaker", "group", "dlna"):
            raise HTTPException(status_code=400, detail="请选择播放目标")
        valid_target = (
            any(item.did == target_id for item in settings.speakers)
            if target_type == "speaker"
            else any(item.id == target_id for item in settings.groups)
        )
        device = None
        if target_type == "dlna":
            device = bridge.dlna_discovery.resolve(target_id) if bridge.dlna_discovery else None
            valid_target = bool(device and device.online) or any(
                item.target_type == "dlna" and item.target_id == target_id
                for item in [*settings.receivers, *settings.airplay2_instances]
            )
        if not valid_target:
            raise HTTPException(status_code=400, detail="播放目标不存在")
        item = await apply_config_transaction(
            lambda: settings.upsert_airplay2_instance(
                instance_id=instance_id,
                name=name,
                target_type=target_type,
                target_id=target_id,
                enabled=bool(payload.get("enabled", True)),
                target_name=device.name if device else None,
                target_model=device.model if device else None,
            ),
            bridge.apply_config_change,
        )
        # Plan diff: a retarget/rename with an unchanged stream-variant set is
        # still rebuilt — the old suffix-set reuse check missed exactly that.
        return item.model_dump()

    @router.post("/instances/{instance_id}/enabled")
    async def set_instance_enabled(instance_id: str, payload: dict):
        if airplay2_mode() == "single":
            raise HTTPException(status_code=409, detail="请在设置中开启或关闭 AirPlay 2")
        item = next(
            (entry for entry in settings.airplay2_instances if entry.id == instance_id), None
        )
        if item is None:
            raise HTTPException(status_code=404, detail="未找到播放入口")
        enabled = bool(payload.get("enabled"))
        is_last_enabled = not enabled and not any(
            entry.id != item.id and entry.enabled for entry in settings.airplay2_instances
        )
        if is_last_enabled:
            try:
                await bridge.stop_airplay2()
            except Exception as exc:
                raise HTTPException(status_code=502, detail=f"停止播放入口失败：{exc}") from exc
        updated = await apply_config_transaction(
            lambda: settings.upsert_airplay2_instance(
                instance_id=item.id,
                name=item.name,
                target_type=item.target_type,
                target_id=item.target_id,
                enabled=enabled,
            ),
            bridge.apply_config_change,
        )
        return updated.model_dump()

    @router.delete("/instances/{instance_id}")
    async def delete_instance(instance_id: str):
        if airplay2_mode() == "single":
            raise HTTPException(status_code=409, detail="固定的 AirPlay 2 入口不能删除")
        item = next(
            (entry for entry in settings.airplay2_instances if entry.id == instance_id), None
        )
        if item is None:
            raise HTTPException(status_code=404, detail="未找到播放入口")
        is_last_enabled = item.enabled and not any(
            entry.id != item.id and entry.enabled for entry in settings.airplay2_instances
        )
        cleanup_warning = ""
        if is_last_enabled:
            try:
                await bridge.stop_airplay2()
            except Exception as exc:
                cleanup_warning = "内部编排服务不可达，远端可能仍有残留入口"
                logger.warning(
                    "Deleting local AirPlay 2 instance %s after remote stop failed: %s",
                    instance_id,
                    exc,
                )
        try:
            await apply_config_transaction(
                lambda: settings.remove_airplay2_instance(instance_id),
                bridge.apply_config_change,
            )
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"删除播放入口失败：{exc}") from exc
        return {"ok": True, "warning": cleanup_warning or None}

    return router
