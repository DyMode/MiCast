"""Playback control routes."""

import asyncio

from fastapi import APIRouter, HTTPException, Response
from fastapi.responses import RedirectResponse

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
        if not _verify_callback_epoch(bridge, payload):
            return {"ok": True, "ignored": True}
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
        if not _verify_callback_epoch(bridge, payload):
            return {"ok": True, "ignored": True}
        device_id = _device_id_from_payload(payload)
        await bridge.session_start(device_id)
        return {"ok": True}

    @router.post("/session/stop")
    async def session_stop(payload: dict | None = None):
        _verify_receiver_callback(payload)
        if not _verify_callback_epoch(bridge, payload):
            return {"ok": True, "ignored": True}
        device_id = _device_id_from_payload(payload)
        await bridge.session_stop(device_id)
        return {"ok": True}

    @router.post("/play")
    async def play():
        resumed = []
        media_service = getattr(bridge, "dlna_service", None)
        if media_service is not None:
            resumed_media = [
                rid
                for rid, state in media_service.states.items()
                if state.state == "PAUSED_PLAYBACK"
            ]
            if resumed_media:
                await asyncio.gather(*(media_service.play(rid) for rid in resumed_media))
                resumed.extend(resumed_media)
        registry = getattr(bridge, "sessions", None)
        if registry is not None:
            for item in list(registry.snapshot()):
                if item["state"] != "paused" or item["protocol"] == "dlna":
                    continue
                lease = registry.current(item["owner"])
                registry.begin(item["owner"], item["protocol"], lease.identity)
                await bridge.reissue_entry_play(item["owner"])
                await bridge._start_entry_targets(item["owner"], resume=True)
                resumed.append(item["owner"])
        # Resume speakers paused from the UI first; nothing paused means
        # re-attaching the receivers that still have a live sender session.
        paused = [
            str(device.get("deviceID"))
            for device in device_manager.get_control_targets()
            if device.get("deviceID") and device_manager.is_paused(str(device.get("deviceID")))
        ]
        if paused:
            await asyncio.gather(*(device_manager.resume(did) for did in paused))
            resumed.extend(paused)
        if resumed:
            return {"ok": True, "resumed": resumed}
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
        media_service = getattr(bridge, "dlna_service", None)
        if media_service is not None:
            await asyncio.gather(
                *(
                    media_service.pause(rid)
                    for rid, state in list(media_service.states.items())
                    if state.state == "PLAYING"
                    or (
                        f"dlna:{rid}"
                        in getattr(getattr(media_service, "media", None), "_preparing", {})
                    )
                )
            )
        targets = [
            str(device.get("deviceID"))
            for device in device_manager.get_control_targets()
            if device.get("deviceID") and device_manager.is_playing(str(device.get("deviceID")))
        ]
        owners = {did: device_manager.owner_of(did) for did in targets}
        await asyncio.gather(*(device_manager.stop(did) for did in targets))
        registry = getattr(bridge, "sessions", None)
        if registry is not None:
            for item in list(registry.snapshot()):
                if item["state"] == "active" and item["protocol"] in ("airplay", "airplay2"):
                    registry.pause(registry.current(item["owner"]).token)
            await registry.tick()
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
        async with volume_lock:
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

    @router.get("/cover/{receiver_id}")
    async def cover(receiver_id: str):
        """Now-playing cover art: sender-pushed bytes first, library redirect
        second, honest 404 when neither exists (the UI hides the slot)."""
        server = bridge.local_server(receiver_id)
        artwork = getattr(server, "artwork_bytes", b"") if server else b""
        if artwork:
            media_type = getattr(server, "artwork_content_type", "") or "image/jpeg"
            return Response(
                content=artwork,
                media_type=media_type,
                headers={"Cache-Control": "no-cache"},
            )
        registry = getattr(bridge, "track_metadata", None)
        cover_url = registry.enrichment_for(receiver_id).cover_url if registry is not None else ""
        if cover_url:
            return RedirectResponse(cover_url, status_code=302)
        raise HTTPException(status_code=404, detail="没有封面")

    return router


# Owners whose playback is MiCast measuring something by itself (the silent
# format probe). Their sessions are real at the speaker but must stay out of
# every user-facing "someone is playing" surface.
BACKGROUND_PROBE_OWNER_PREFIX = "auto-probe:"


def _is_background_probe(device_manager: DeviceManager, device_id: str) -> bool:
    # getattr: hand-built doubles (tests) may not model ownership at all.
    owner_of = getattr(device_manager, "owner_of", None)
    if owner_of is None:
        return False
    owner = owner_of(device_id)
    return bool(owner) and str(owner).startswith(BACKGROUND_PROBE_OWNER_PREFIX)


async def build_playback_state(device_manager: DeviceManager, refresh: bool = False) -> dict:
    """Playback snapshot for the UI — shared by GET /state and the WS push."""
    bridge = getattr(device_manager, "bridge", None)
    projection = getattr(bridge, "runtime_snapshot", None)
    # Reserve before any hardware reads: a slow response must remain older
    # than a later WebSocket observation, even if it completes last.
    runtime = projection.project(bridge.sessions) if projection is not None else None
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
                # The background format probe plays a silent test stream on an
                # idle speaker; that is our own measurement, not something the
                # user started, so it must not pop the playback bar or draw a
                # link from the account to a speaker (field report: "切换功能栏
                # 弹出播放框，实际没播放").
                "playing": playing and not _is_background_probe(device_manager, did),
                "paused": paused,
                "muted": device_manager.is_muted(did),
                "state": (
                    "playing"
                    if playing and not _is_background_probe(device_manager, did)
                    else "paused"
                    if paused
                    else "idle"
                ),
                "probing": _is_background_probe(device_manager, did),
            }
        )
    if bridge is not None:
        volumes = DeviceVolume(device_manager, bridge)
        for name, prefix in (("_airplay_targets", "airplay:"), ("_dlna_targets", "dlna:")):
            adapter = getattr(bridge, name, None)
            if adapter is None:
                continue
            for owner, targets in adapter.statuses().items():
                session = bridge.sessions.current(owner)
                if session is None or session.state.value not in ("active", "paused"):
                    continue
                for target, info in targets.items():
                    did = prefix + target
                    if any(item["did"] == did for item in devices):
                        continue
                    playing = session.state.value == "active" and info.get("status") in (
                        "playing",
                        "streaming",
                    )
                    paused = session.state.value == "paused"
                    try:
                        volume = await volumes.get_volume(did, refresh=False)
                    except Exception:
                        volume = None
                    devices.append(
                        {
                            "did": did,
                            "name": info.get("name", target),
                            "volume": volume,
                            "playing": playing,
                            "paused": paused,
                            "muted": volumes.is_muted(did),
                            "state": "playing" if playing else "paused" if paused else "idle",
                            "probing": False,
                        }
                    )
        for session in bridge.sessions.snapshot():
            if session["state"] != "paused":
                continue
            owner = session["owner"]
            for prefix, key_prefix, targets in (
                ("airplay:", "airplay:", settings.receiver_airplay_targets(owner)),
                ("dlna:", "dlna-target:", settings.receiver_dlna_targets(owner)),
            ):
                for target in targets:
                    did = prefix + target
                    current = bridge.sessions.targets.current(key_prefix + target)
                    if any(item["did"] == did for item in devices) or (
                        current is not None and current.token.owner != owner
                    ):
                        continue
                    try:
                        volume = await volumes.get_volume(did, refresh=False)
                    except Exception:
                        volume = None
                    devices.append(
                        {
                            "did": did,
                            "name": target,
                            "volume": volume,
                            "playing": False,
                            "paused": True,
                            "muted": volumes.is_muted(did),
                            "state": "paused",
                            "probing": False,
                        }
                    )
    known = [item["volume"] for item in devices if item["volume"] is not None]
    return {
        **({"runtime": runtime} if runtime is not None else {}),
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
            # stream_url_for appends the sink's channel + EQ/loudness variant:
            # a bare channel suffix names a stream nobody publishes once the
            # receiver's variants are split per tuning.
            return settings.stream_url_for(receiver.id, device_id)
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


def _verify_callback_epoch(bridge, payload):
    source = (
        bridge.ingress_source(payload.get("device_id"))
        if hasattr(bridge, "ingress_source")
        else getattr(bridge, "_airplay2_sources", {}).get(payload.get("device_id"))
    )
    epoch = getattr(source, "epoch", None)
    if isinstance(epoch, str) and epoch and payload.get("epoch") != epoch:
        raise HTTPException(status_code=409, detail="Receiver process was replaced")
    sequence = payload.get("event_seq")
    if source is not None and type(sequence) is int:
        previous = getattr(source, "last_callback_sequence", 0)
        if isinstance(previous, int) and sequence <= previous:
            return False
        source.last_callback_sequence = sequence
    return True
