"""AirPlay receiver status routes."""

from fastapi import APIRouter, HTTPException

from micast.audio_bridge import AudioBridge
from micast.config import settings
from micast.config_apply import apply_config_transaction
from micast.dlna import DlnaService


def install(
    bridge: AudioBridge,
    dlna: DlnaService | None = None,
    device_manager=None,
) -> APIRouter:
    # The router is created per install() (not module-level) so tests mounting
    # a second instance never share accumulated handlers with the real app.
    router = APIRouter(prefix="/api/receivers", tags=["receivers"])

    async def apply_runtime() -> None:
        await bridge.apply_config_change()
        if dlna:
            await dlna.reconcile()

    @router.get("")
    async def get_receivers():
        return bridge.status.get("receivers", [])

    @router.get("/definitions")
    async def get_receiver_definitions():
        return [item.model_dump() for item in settings.receivers]

    @router.post("/definitions")
    async def create_receiver(payload: dict):
        name = str(payload.get("name", "")).strip()
        target_type = payload.get("target_type", "selected")
        target_id = payload.get("target_id")
        if not name:
            raise HTTPException(status_code=400, detail="name required")
        if target_type not in ("selected", "speaker", "group", "dlna"):
            raise HTTPException(status_code=400, detail="invalid target_type")
        if len(name) > 64:
            raise HTTPException(status_code=400, detail="入口名称最多 64 个字符")
        if any(item.name == name for item in settings.receivers):
            raise HTTPException(status_code=409, detail="已有同名播放入口，请更换名称")
        device = None
        if target_type == "dlna":
            device = bridge.dlna_discovery.resolve(target_id) if bridge.dlna_discovery else None
            if not device or not device.online:
                raise HTTPException(status_code=409, detail="请开启网络发现并选择在线 DLNA 设备")
            if any(
                item.target_type == "dlna" and item.target_id == target_id
                for item in settings.receivers
            ):
                raise HTTPException(status_code=409, detail="此设备已有 AirPlay 入口")

        def create():
            # Recheck uniqueness under the configuration transaction lock.
            if any(item.name == name for item in settings.receivers):
                raise HTTPException(status_code=409, detail="已有同名播放入口，请更换名称")
            if target_type == "dlna" and any(
                item.target_type == "dlna" and item.target_id == target_id
                for item in settings.receivers
            ):
                raise HTTPException(status_code=409, detail="此设备已有 AirPlay 入口")
            return settings.add_receiver(
                name,
                target_type,
                target_id,
                target_name=device.name[:128] if device else "",
                target_model=device.model[:128] if device else "",
            )

        receiver = await apply_config_transaction(create, apply_runtime)
        return receiver.model_dump()

    @router.delete("/definitions/{receiver_id}")
    async def delete_receiver(receiver_id: str):
        if not any(item.id == receiver_id for item in settings.receivers):
            raise HTTPException(status_code=404, detail="receiver not found")
        await apply_config_transaction(lambda: settings.remove_receiver(receiver_id), apply_runtime)
        return {"ok": True}

    @router.patch("/definitions/{receiver_id}")
    async def update_receiver(receiver_id: str, payload: dict):
        # Legacy speaker/group mappings stay fixed. A local bridge can select
        # a replacement LAN renderer without recreating its AirPlay identity.
        current = next((item for item in settings.receivers if item.id == receiver_id), None)
        if current is None:
            raise HTTPException(status_code=404, detail="receiver not found")
        name = payload.get("name")
        if name is not None:
            name = str(name).strip()
            if not name or len(name) > 64:
                raise HTTPException(status_code=400, detail="请输入 1–64 个字符的入口名称")
            if any(item.id != receiver_id and item.name == name for item in settings.receivers):
                raise HTTPException(status_code=409, detail="已有同名播放入口，请更换名称")
        device = None
        if "target_type" in payload or "target_id" in payload:
            if current.target_type == "dlna" and payload.get("target_type", "dlna") == "dlna":
                device = (
                    bridge.dlna_discovery.resolve(payload.get("target_id"))
                    if bridge.dlna_discovery
                    else None
                )
                if not device or not device.online:
                    raise HTTPException(status_code=409, detail="请选择在线 DLNA 设备")
                if any(
                    item.id != receiver_id
                    and item.target_type == "dlna"
                    and item.target_id == device.id
                    for item in settings.receivers
                ):
                    raise HTTPException(status_code=409, detail="此设备已有 AirPlay 入口")
            else:
                raise HTTPException(status_code=400, detail="经典 AirPlay 入口不支持修改播放目标")

        def update():
            if name is not None and any(
                item.id != receiver_id and item.name == name for item in settings.receivers
            ):
                raise HTTPException(status_code=409, detail="已有同名播放入口，请更换名称")
            if device is not None and any(
                item.id != receiver_id
                and item.target_type == "dlna"
                and item.target_id == device.id
                for item in settings.receivers
            ):
                raise HTTPException(status_code=409, detail="此设备已有 AirPlay 入口")
            return settings.update_receiver(
                receiver_id,
                name=name,
                enabled=payload.get("enabled"),
                target_id=device.id if device else None,
                target_name=device.name[:128] if device else None,
                target_model=device.model[:128] if device else None,
            )

        receiver = await apply_config_transaction(update, apply_runtime)
        if receiver is None:
            raise HTTPException(status_code=404, detail="receiver not found")
        return receiver.model_dump()

    @router.post("/groups")
    async def create_group(payload: dict):
        name = str(payload.get("name", "")).strip()
        speaker_ids = payload.get("speaker_ids", [])
        airplay_targets = payload.get("airplay_targets") or []
        dlna_targets = payload.get("dlna_targets") or []
        member_count = (
            (len(speaker_ids) if isinstance(speaker_ids, list) else 0)
            + len(airplay_targets)
            + len(dlna_targets)
        )
        if not name or not isinstance(speaker_ids, list) or member_count < 2:
            raise HTTPException(status_code=400, detail="组合至少需要两个成员（音箱或网络设备）")
        try:

            def mutate_group():
                group = settings.add_group(
                    name,
                    [str(item) for item in speaker_ids],
                    airplay_targets=airplay_targets,
                    dlna_targets=dlna_targets,
                )
                settings.add_receiver(name, "group", group.id)
                return group

            group = await apply_config_transaction(mutate_group, apply_runtime)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        result = group.model_dump()
        if device_manager:
            result["codec_compatibility"] = device_manager.codec_compatibility(group.speaker_ids)
        return result

    @router.post("/groups/compatibility")
    async def group_compatibility(payload: dict):
        speaker_ids = payload.get("speaker_ids", [])
        if not isinstance(speaker_ids, list):
            raise HTTPException(status_code=400, detail="speaker_ids must be a list")
        if device_manager:
            return device_manager.codec_compatibility([str(item) for item in speaker_ids])
        return {
            "members": {},
            "possible_common_formats": ["MP3"],
            "confirmed_common_formats": [],
            "recommended_format": "MP3",
            "status": "needs_check",
            "unknown_members": [],
        }

    @router.patch("/groups/{group_id}")
    async def update_group(group_id: str, payload: dict):
        try:
            group = await apply_config_transaction(
                lambda: settings.update_group(
                    group_id,
                    name=payload.get("name"),
                    speaker_ids=payload.get("speaker_ids"),
                    delays_ms=payload.get("delays_ms"),
                    mode=payload.get("mode"),
                    channels=payload.get("channels"),
                    gains_db=payload.get("gains_db"),
                    airplay_targets=payload.get("airplay_targets"),
                    dlna_targets=payload.get("dlna_targets"),
                    network_channels=payload.get("network_channels"),
                    anchor_did=payload.get("anchor_did"),
                ),
                apply_runtime,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if group is None:
            raise HTTPException(status_code=404, detail="group not found")
        # One funnel for every field: the plan diff decides what to rebuild,
        # live-apply (delays), or replay (membership) — no per-key dispatch here.
        return group.model_dump()

    @router.delete("/groups/{group_id}")
    async def delete_group(group_id: str):
        # Uniform pre-delete reference check: classic receivers AND AirPlay 2
        # instances. AirPlay 2 references refuse the delete — the instance
        # would dangle, silently collapsing its stream plan.
        refs = settings.target_references("group", group_id)
        airplay2_refs = [ref for ref in refs if ref["kind"] == "airplay2"]
        if airplay2_refs:
            names = "、".join(f"「{ref['name']}」" for ref in airplay2_refs)
            raise HTTPException(
                status_code=409,
                detail=f"组合正被 AirPlay 2 入口 {names} 使用，请先删除或修改这些入口",
            )
        if not any(item.id == group_id for item in settings.groups):
            raise HTTPException(status_code=404, detail="分组不存在")

        def remove() -> None:
            for ref in refs:
                settings.remove_receiver(ref["id"])
            settings.remove_group(group_id)

        await apply_config_transaction(remove, apply_runtime)
        return {"ok": True}

    return router
