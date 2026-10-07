"""External DLNA renderer discovery routes."""

import asyncio
import contextlib
import secrets
from urllib.parse import quote
from xml.sax.saxutils import escape

from fastapi import APIRouter, HTTPException

from micast.audio_bridge import AudioBridge
from micast.config import settings


def install(bridge: AudioBridge) -> APIRouter:
    router = APIRouter(prefix="/api/dlna-devices", tags=["dlna-devices"])
    tests: dict[str, dict] = {}

    @router.get("")
    async def list_dlna_devices():
        discovery = bridge.dlna_discovery
        attached = {
            target_id: group.id for group in settings.groups for target_id in group.dlna_targets
        }
        mappings = {
            item.target_id: item
            for item in [*settings.receivers, *settings.airplay2_instances]
            if item.target_type == "dlna"
        }
        statuses = bridge.diagnostics.get("dlna_targets", {})
        runtime_by_id = {
            runtime["id"]: runtime for targets in statuses.values() for runtime in targets.values()
        }
        devices = []
        for device in discovery.devices() if discovery else []:
            runtime = runtime_by_id.get(device.id, {})
            devices.append(
                {
                    "id": device.id,
                    "name": device.name,
                    "model": device.model,
                    "kind": device.kind,
                    "online": device.online,
                    "supported": bool(device.control_url),
                    "unsupported_reason": "" if device.control_url else "不支持 AVTransport 投放",
                    "attached_group": attached.get(device.id),
                    "attached_receiver": mappings[device.id].id if device.id in mappings else None,
                    "test_result": tests.get(device.id),
                    "stream_status": runtime.get("status", ""),
                    "stream_detail": runtime.get("detail", ""),
                    "volume_control": bool(device.rendering_url) and device.online,
                    "volume_readback": bool(device.rendering_url) and device.online,
                }
            )
        known = {device["id"] for device in devices}
        for target_id in set(attached) | set(mappings):
            group_id = attached.get(target_id)
            if target_id in known:
                continue
            runtime = runtime_by_id.get(target_id, {})
            devices.append(
                {
                    "id": target_id,
                    "name": mappings[target_id].target_name
                    if target_id in mappings
                    else runtime.get("name") or target_id,
                    "model": mappings[target_id].target_model if target_id in mappings else "",
                    "kind": "speaker",
                    "online": False,
                    "supported": True,
                    "unsupported_reason": "",
                    "attached_group": group_id,
                    "attached_receiver": mappings[target_id].id if target_id in mappings else None,
                    "test_result": tests.get(target_id),
                    "stream_status": runtime.get("status", ""),
                    "stream_detail": runtime.get("detail", ""),
                }
            )
        return devices

    @router.post("/rescan")
    async def rescan():
        discovery = bridge.dlna_discovery
        if not settings.network_discovery_enabled or not discovery:
            raise HTTPException(status_code=409, detail="请先开启网络发现")
        await discovery.rescan()
        return {"ok": True}

    @router.post("/{device_id}/test")
    async def test_device(device_id: str, payload: dict):
        manager = bridge.dlna_target_manager
        device = bridge.dlna_discovery.resolve(device_id) if bridge.dlna_discovery else None
        if not manager or not device or not device.online:
            raise HTTPException(status_code=409, detail="请开启网络发现并选择在线设备")
        continuous = payload.get("mode") == "stream"
        token = secrets.token_urlsafe(12)
        owner = f"local-test:{device_id}"
        if bridge.sessions.current(owner):
            raise HTTPException(status_code=409, detail="此设备正在测试")
        lease = bridge.sessions.begin(owner, "diagnostic", token)
        server = bridge._stream_server
        sample = server.begin_delay_calibration(
            token, [device_id], "local-test" if continuous else None
        )
        sample["duration_seconds"] = 12
        url = (
            f"http://{settings.effective_stream_host}:{settings.stream_port}"
            f"/calibration/{token}/{quote(device_id, safe='')}.wav"
        )
        commanded = False

        async def stop():
            nonlocal commanded
            server.end_delay_calibration(token)
            if commanded:
                current = (
                    bridge.dlna_discovery.resolve(device_id) if bridge.dlna_discovery else None
                )
                if current is None:
                    raise RuntimeError("设备不可达，测试停止待重试")
                await manager._soap(current, "Stop", {"InstanceID": "0"})
                commanded = False

        async def start():
            nonlocal commanded
            from micast.dlna_client import _DIDL_METADATA

            metadata = _DIDL_METADATA.format(title="MiCast 测试", mime="audio/wav", url=escape(url))
            commanded = True
            await manager._soap(
                device,
                "SetAVTransportURI",
                {
                    "InstanceID": "0",
                    "CurrentURI": escape(url),
                    "CurrentURIMetaData": escape(metadata),
                },
            )
            if bridge.sessions.valid(lease.token):
                await manager._soap(device, "Play", {"InstanceID": "0", "Speed": "1"})

        try:
            acquired = await bridge.sessions.targets.acquire(
                f"dlna-target:{device_id}",
                lease.token,
                stop,
                steal=False,
                start=start,
            )
            if not acquired:
                raise HTTPException(status_code=409, detail="设备正在播放，请停止播放后测试")
            try:
                await asyncio.wait_for(sample["ready"].wait(), 5)
            except TimeoutError as exc:
                raise HTTPException(
                    status_code=504, detail="设备接受了命令，但未拉取测试音频"
                ) from exc
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(sample["stopped"].wait(), 8 if continuous else 3)
            if not bridge.sessions.valid(lease.token):
                raise HTTPException(status_code=409, detail="测试已停止或被新的播放接管")
            if continuous and sample.get("bytes_sent", 0) < 44100 * 4 * 5:
                raise HTTPException(
                    status_code=504, detail="持续流未完成拉取，请尝试实际 AirPlay 播放"
                )
            result = {
                "mode": "stream" if continuous else "sample",
                "status": "pulled",
                "detail": "持续流已传输，请确认出声"
                if continuous
                else "测试音频已拉取，请确认出声",
            }
            ledger = getattr(bridge, "capabilities", None)
            if ledger:
                key = f"dlna:{device_id}"
                item = ledger.identify(
                    key,
                    name=device.name,
                    model=device.model,
                    firmware=getattr(device, "firmware", ""),
                )
                proof = ledger.record(
                    key,
                    "dlna",
                    "play_file",
                    "pulled",
                    format="WAV",
                    source="finite_stream_test" if continuous else "explicit_test",
                )
                result["evidence"] = {
                    "device_id": key,
                    "action": proof.action,
                    "format": proof.format,
                    "pulled_at": proof.pulled_at,
                    "revision": item.revision,
                }
            tests[device_id] = result
            return result
        except HTTPException as exc:
            tests[device_id] = {
                "mode": "stream" if continuous else "sample",
                "status": "error",
                "detail": exc.detail,
            }
            raise
        except Exception as exc:
            tests[device_id] = {"status": "error", "detail": "设备拒绝了测试播放"}
            raise HTTPException(status_code=502, detail=f"测试失败：{exc}") from exc
        finally:
            server.end_delay_calibration(token)
            await bridge.sessions.close(lease.token, "test_finished")

    @router.delete("/{device_id}/test")
    async def stop_test(device_id: str):
        await bridge.sessions.close_all(f"local-test:{device_id}", reason="test_stopped")
        return {"ok": True}

    return router
