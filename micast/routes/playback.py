"""Playback control routes."""

import asyncio

from fastapi import APIRouter, HTTPException

from micast.audio_bridge import AudioBridge
from micast.config import settings
from micast.device_volume import DeviceVolume
from micast.playback_lifecycle import stop_output
from micast.volume import db_to_percent
from micast.xiaomi.device_manager import DeviceManager

router = APIRouter(prefix="/api/playback", tags=["playback"])


def install(bridge: AudioBridge, device_manager: DeviceManager) -> APIRouter:
    volume_lock = asyncio.Lock()
    volumes = DeviceVolume(device_manager, bridge)

    @router.post("/volume/levels")
    async def volume_levels(payload: dict):
        ids = list(dict.fromkeys(_target_ids(payload, device_manager)))
        if len(ids) > 100:
            raise HTTPException(status_code=400, detail="一次最多读取 100 台音箱")
        result = []
        for did in ids:
            try:
                value = await volumes.get_volume(did, refresh=True)
                if value is None:
                    raise ValueError("无法读取当前音量")
                result.append({"did": did, "ok": True, "volume": value})
            except Exception as exc:
                result.append({"did": did, "ok": False, "error": str(exc)})
        return {"devices": result}

    @router.post("/session/volume")
    async def receiver_volume(payload: dict):
        import hmac

        token = settings.orchestrator_token.strip()
        if not token or not hmac.compare_digest(str(payload.get("token", "")), token):
            raise HTTPException(status_code=403, detail="Invalid receiver callback token")
        receiver_id = payload.get("device_id")
        configured = {item.id for item in settings.airplay2_instances if item.enabled}
        if not receiver_id or receiver_id not in configured:
            raise HTTPException(status_code=404, detail="播放入口不存在")
        try:
            db = payload.get("db")
            if isinstance(db, bool) or not isinstance(db, (int, float)):
                raise ValueError("无效的投放音量")
            percent = db_to_percent(db)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await bridge._local_volume(receiver_id, percent)
        return {"ok": True, "volume": percent}

    @router.post("/start")
    async def session_start(payload: dict | None = None):
        _verify_receiver_callback(payload)
        device_id = _device_id_from_payload(payload)
        await bridge.session_start(device_id)
        return {"ok": True}

    @router.post("/session/stop")
    async def session_stop(payload: dict | None = None):
        _verify_receiver_callback(payload)
        device_id = _device_id_from_payload(payload)
        await bridge.session_stop(device_id)
        return {"ok": True}

    @router.post("/play")
    async def play():
        # Resume speakers paused from the UI first; nothing paused means
        # re-attaching the receivers that still have a live sender session.
        paused = [
            str(device.get("deviceID"))
            for device in device_manager.get_control_targets()
            if device.get("deviceID") and device_manager.is_paused(str(device.get("deviceID")))
        ]
        if paused:
            await asyncio.gather(*(device_manager.resume(did) for did in paused))
            return {"ok": True, "resumed": paused}
        # Receiver setups have no single "selected device": play back every entry
        # whose sender session is live (that is what the speaker was listening
        # to). Without this the button answered 400 "No device selected or not
        # logged in" while a receiver was casting — field report (0.4.0):
        # "控制就乱了".
        reissued = [
            entry_id
            for entry_id in settings.audio_entry_ids()
            if bridge.is_session_active(entry_id)
        ]
        for entry_id in reissued:
            await bridge.reissue_entry_play(entry_id)
        if reissued:
            return {"ok": True, "reissued": reissued}
        selected = settings.selected_device_id
        if not selected:
            raise HTTPException(status_code=400, detail="没有正在投放的会话")
        url = device_manager.stream_url_of(selected) or _stream_url_for_device(selected)
        if not url:
            raise HTTPException(status_code=400, detail="No receiver targets the selected device")
        ok = await device_manager.play_stream(selected, url)
        if not ok:
            raise HTTPException(status_code=400, detail="No device selected or not logged in")
        return {"ok": True, "url": url}

    @router.post("/pause")
    async def pause():
        targets = [
            str(device.get("deviceID"))
            for device in device_manager.get_control_targets()
            if device.get("deviceID") and device_manager.is_playing(str(device.get("deviceID")))
        ]
        owners = {did: device_manager.owner_of(did) for did in targets}
        await asyncio.gather(*(device_manager.stop(did) for did in targets))
        # A paused speaker keeps its URL and stops reading, which fills its
        # stream queue with chunks nobody takes: the diagnostics then showed
        # "丢弃 N 次" for audio that was never lost, and the ghost reaper kicked
        # the connection mid-pause anyway. Drop those connections now instead.
        for did, owner in owners.items():
            if not owner or bridge is None:
                continue
            for stream_id in bridge.entry_stream_ids(owner):
                bridge.stream_server.kick_clients(stream_id, sink=did)
        return {"ok": True, "paused": targets}

    @router.post("/stop")
    async def stop():
        result = await stop_output(bridge, device_manager)
        return {"ok": True, **result}

    @router.post("/volume")
    async def set_volume(payload: dict):
        volume = payload.get("volume")
        delta = payload.get("delta")
        if delta is not None and (
            type(delta) is not int or not -100 <= delta <= 100 or volume is not None
        ):
            raise HTTPException(status_code=400, detail="无效的音量增减值")
        if delta is None and (type(volume) is not int or not 0 <= volume <= 100):
            raise HTTPException(status_code=400, detail="volume must be 0-100")
        device_ids = list(dict.fromkeys(_target_ids(payload, device_manager)))
        if not device_ids:
            raise HTTPException(status_code=400, detail="No playback target selected")
        async with volume_lock:
            if delta is None:
                results = await _set_volumes(volumes, device_ids, volume)
            else:
                results = []
                for did in device_ids:
                    try:
                        current = await volumes.get_volume(did, refresh=True)
                        if current is None:
                            raise ValueError("无法读取当前音量")
                        value = await volumes.set_volume(did, max(0, min(100, current + delta)))
                        results.append({"did": did, "ok": True, "volume": value})
                    except Exception as exc:
                        results.append({"did": did, "ok": False, "error": str(exc)})
        return {"ok": all(item["ok"] for item in results), "devices": results}

    @router.post("/mute")
    async def set_mute(payload: dict):
        muted = payload.get("muted")
        if not isinstance(muted, bool):
            raise HTTPException(status_code=400, detail="muted must be boolean")
        device_ids = list(dict.fromkeys(_target_ids(payload, device_manager)))
        if not device_ids:
            raise HTTPException(status_code=400, detail="No playback target selected")
        results = []
        for did in device_ids:
            try:
                await volumes.set_mute(did, muted)
                results.append({"did": did, "ok": True, "muted": muted})
            except Exception as exc:
                results.append({"did": did, "ok": False, "error": str(exc)})
        return {"ok": all(item["ok"] for item in results), "muted": muted, "devices": results}

    @router.get("/state")
    async def playback_state(refresh: bool = False):
        return await build_playback_state(device_manager, refresh=refresh)

    return router


async def build_playback_state(device_manager: DeviceManager, refresh: bool = False) -> dict:
    """Playback snapshot for the UI — shared by GET /state and the WS push."""
    device_ids = [
        str(item.get("deviceID"))
        for item in device_manager.get_control_targets()
        if item.get("deviceID")
    ]
    devices = []
    for did in device_ids:
        playing = device_manager.is_playing(did)
        paused = device_manager.is_paused(did)
        devices.append(
            {
                "did": did,
                "name": device_manager.get_alias(did),
                "volume": await device_manager.get_volume(did, refresh=refresh),
                "playing": playing,
                "paused": paused,
                "muted": device_manager.is_muted(did),
                "state": "playing" if playing else "paused" if paused else "idle",
            }
        )
    known = [item["volume"] for item in devices if item["volume"] is not None]
    return {
        "playing": any(item["playing"] for item in devices),
        "paused": any(item["paused"] for item in devices),
        "volume": round(sum(known) / len(known)) if known else None,
        "mixed_volume": len(set(known)) > 1,
        "muted": bool(devices) and all(item["muted"] for item in devices),
        "devices": devices,
    }


def _stream_url_for_device(device_id: str) -> str | None:
    """Resolve the live stream URL of the receiver that targets a speaker."""
    for receiver in settings.receivers:
        if device_id in settings.receiver_targets(receiver.id):
            return (
                f"http://{settings.effective_stream_host}:{settings.stream_port}"
                f"/stream/{receiver.id}{settings.channel_suffix(receiver.id, device_id)}"
            )
    return None


async def _set_volumes(device_manager: DeviceManager, device_ids: list[str], volume: int):
    import asyncio

    values = await asyncio.gather(
        *(device_manager.set_volume(did, volume) for did in device_ids),
        return_exceptions=True,
    )
    result = []
    failures = 0
    for did, value in zip(device_ids, values, strict=True):
        if isinstance(value, Exception):
            failures += 1
            result.append({"did": did, "ok": False, "error": str(value)})
        else:
            result.append({"did": did, "ok": True, "volume": value})
    return result


def _target_ids(payload: dict, device_manager: DeviceManager) -> list[str]:
    requested = payload.get("device_ids")
    if isinstance(requested, list):
        return [str(did) for did in requested if did]
    device_id = payload.get("device_id")
    if device_id:
        return [str(device_id)]
    return [
        str(item.get("deviceID"))
        for item in device_manager.get_control_targets()
        if item.get("deviceID")
    ]


def _device_id_from_payload(payload: dict | None) -> str | None:
    if not payload:
        return None
    device_id = payload.get("device_id")
    if device_id:
        return str(device_id)
    return None


def _verify_receiver_callback(payload: dict | None) -> None:
    """Require the shared secret for callbacks from orchestrated receivers."""
    callback_token = settings.orchestrator_token.strip()
    if not payload or not payload.get("device_id"):
        raise HTTPException(status_code=400, detail="Missing receiver identity")
    if not callback_token or payload.get("token") != callback_token:
        raise HTTPException(status_code=403, detail="Invalid receiver callback token")
