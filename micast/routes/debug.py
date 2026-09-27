"""Debug routes for troubleshooting."""

import asyncio
import contextlib
import json
import logging
import mimetypes
import secrets
import shutil
import tempfile
import time
from pathlib import Path
from typing import Annotated, Literal

import av
from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, Response

from micast.audio_bridge import AudioBridge
from micast.audio_encoder import transcode_file_to_wav
from micast.codec_probe import build_fixtures, probe_device_formats
from micast.config import settings
from micast.diagnostics import build_log_text, build_report, collect_state, log_payload
from micast.playback_lifecycle import stop_output
from micast.runtime_log import LogQuery, resolve_query, runtime_logs
from micast.test_tone import test_tone_wav
from micast.url_safety import validate_http_url
from micast.xiaomi.device_manager import CODEC_FORMATS, DeviceManager

logger = logging.getLogger(__name__)

# Log selection axes, shared by the three endpoints that hand records out. Named
# types keep a typo in a query string a 422 instead of a silently empty file.
LogSource = Literal["app", "all"]
LogLevel = Literal["all", "warn"]
# Duration presets the export endpoints accept. "session" means the whole buffer.
LogWindow = Literal["5m", "15m", "30m", "1h", "session"]

# Xiaomi firmwares expose spoken text through different MIoT actions. MiNA's
# mibrain endpoint may still return success on these models while only flashing
# the activity light, so prefer the device-specific action when known.
_MIOT_TTS_ACTIONS: dict[str, tuple[int, int]] = {
    "OH2": (5, 3),
    "OH2P": (7, 3),
    "LX06": (5, 1),
}

_DIRECT_FORMATS = {".mp3", ".m4a", ".aac", ".wav", ".flac"}
_UPLOAD_FORMATS = _DIRECT_FORMATS | {".ogg", ".ape"}
_MAX_UPLOAD_BYTES = 50 * 1024 * 1024
_MAX_PLAYABLE_BYTES = 200 * 1024 * 1024


def _mina_command_accepted(result: object) -> bool:
    """MiNA uses both an outer API code and an inner device command code."""
    if not isinstance(result, dict) or result.get("code") != 0:
        return False
    data = result.get("data")
    return not isinstance(data, dict) or data.get("code", 0) == 0


def _request_offsets(arrivals: dict[str, float], anchor: str, members: list[str]) -> dict[str, int]:
    """Convert test-tone request skew into a coarse magnitude reference.

    Request order cannot determine audible direction because each speaker has
    its own opaque decoder buffer.  Keep the signed request delta only as
    diagnostic context; the guided UI asks the listener for audible direction.
    """
    anchor_at = arrivals[anchor]
    result: dict[str, int] = {}
    for did in members:
        if did == anchor or did not in arrivals:
            continue
        offset = round((arrivals[did] - anchor_at) * 1000 / 50) * 50
        result[did] = max(-20000, min(20000, offset))
    return {did: value for did, value in result.items() if value != 0}


async def _restore_streams(
    device_manager: DeviceManager,
    previous: dict[str, tuple[str | None, str | None]],
    *,
    attempts: int = 3,
    retry_delay: float = 0.4,
) -> list[str]:
    """Restore every pre-calibration stream and report speakers still failed.

    Xiaomi's cloud command can transiently accept one member and reject the
    next. Retrying only failed members prevents a calibration from silently
    leaving half a group stopped. Persistent failures enter the existing
    background recovery loop as well as being reported to the caller.
    """
    pending = {did for did, (url, _owner) in previous.items() if url}
    for attempt in range(max(1, attempts)):
        if not pending:
            break

        async def restore(did: str) -> tuple[str, bool]:
            url, owner = previous[did]
            try:
                result = await asyncio.wait_for(
                    device_manager.play_stream(did, url, owner=owner, force=True),
                    timeout=8,
                )
                return did, result is not False
            except Exception:
                logger.exception("Failed to restore speaker %s after calibration", did)
                return did, False

        results = await asyncio.gather(*(restore(did) for did in pending))
        pending = {did for did, ok in results if not ok}
        if pending and attempt + 1 < attempts:
            await asyncio.sleep(retry_delay)

    for did in pending:
        url, owner = previous[did]
        if url:
            device_manager.note_play_error(did, owner or "calibration-restore", "恢复播放失败", url)
    return sorted(pending)


def install(bridge: AudioBridge, device_manager: DeviceManager) -> APIRouter:
    # A router per install, not one module-level router for every app that ever
    # called this: building a second app (a test, an embedded server) used to
    # register a second copy of every diagnostics route on the same object, and
    # the *first* copy — with the first app's bridge and device manager — won
    # every request.
    router = APIRouter(prefix="/api/debug", tags=["debug"])
    live_calibrations: dict[str, dict] = {}
    upload_dir = Path(tempfile.gettempdir()) / "micast-test-audio"
    shutil.rmtree(upload_dir, ignore_errors=True)
    upload_dir.mkdir(parents=True, exist_ok=True)
    uploaded_media: dict[str, dict] = {}
    active_tests: dict[str, dict] = {}
    codec_fixtures: dict[str, tuple[str, Path, str]] = {}

    async def cloud_devices() -> list[dict]:
        """Device list for a diagnostics action, with a plain message on failure.

        These actions run *because* something is wrong, so a cloud outage must
        come back as a sentence the page can show, not as an unhandled
        exception with a technical detail.
        """
        try:
            return await device_manager.list_devices()
        except Exception as exc:
            raise HTTPException(
                status_code=503, detail=f"无法读取音箱列表：{exc}"
            ) from exc

    def ensure_codec_fixtures() -> dict[str, tuple[str, Path, str]]:
        if codec_fixtures:
            return codec_fixtures
        # The audible arpeggio, one fixture per capability the app can serve, at
        # the app's own sample rate so the probe matches the real stream. The
        # user asked for this test, so it is meant to be heard — the background
        # format detection uses silent fixtures instead (see codec_probe).
        codec_fixtures.update(
            build_fixtures(bridge._stream_server, test_tone_wav(), upload_dir)
        )
        return codec_fixtures


    def media_info(path: Path) -> tuple[float | None, str]:
        try:
            with av.open(str(path)) as container:
                audio = container.streams.audio[0]
                duration = float(audio.duration * audio.time_base) if audio.duration else None
                return duration, audio.codec_context.name or path.suffix.removeprefix(".")
        except Exception:
            return None, path.suffix.removeprefix(".")

    async def stop_test(session_id: str, restore: bool = True) -> dict:
        active = active_tests.pop(session_id, None)
        if not active:
            return {"ok": True, "restored": 0}
        owner = active["owner"]
        await asyncio.gather(
            *(device_manager.stop_playback(did, owner=owner) for did in active["members"]),
            return_exceptions=True,
        )
        restored = 0
        if restore:
            results = await asyncio.gather(
                *(
                    device_manager.play_stream(did, url, owner=previous_owner, force=True)
                    for did, (url, previous_owner) in active["previous"].items()
                    if url
                ),
                return_exceptions=True,
            )
            restored = sum(item is True for item in results)
            await asyncio.gather(
                *(
                    device_manager.set_volume(did, volume)
                    for did, volume in active["volumes"].items()
                    if volume is not None
                ),
                return_exceptions=True,
            )
        return {"ok": True, "restored": restored}

    @router.post("/media")
    async def upload_test_media(file: Annotated[UploadFile, File()]):
        suffix = Path(file.filename or "").suffix.lower()
        if suffix not in _UPLOAD_FORMATS:
            raise HTTPException(status_code=415, detail="支持 MP3、AAC、M4A、FLAC、WAV、OGG 和 APE")
        token = secrets.token_urlsafe(18)
        source = upload_dir / f"{token}{suffix}"
        size = 0
        try:
            with source.open("wb") as output:
                while chunk := await file.read(1024 * 1024):
                    size += len(chunk)
                    if size > _MAX_UPLOAD_BYTES:
                        raise HTTPException(status_code=413, detail="音频文件不能超过 50 MB")
                    output.write(chunk)
            playable = source
            media_type = mimetypes.guess_type(source.name)[0] or "audio/mpeg"
            converted = suffix in {".ape", ".ogg"}
            if converted:
                wav = await asyncio.to_thread(transcode_file_to_wav, source)
                if len(wav) > _MAX_PLAYABLE_BYTES:
                    raise HTTPException(
                        status_code=413, detail="转换后的音频过大，请选择更短的文件"
                    )
                playable = upload_dir / f"{token}.wav"
                playable.write_bytes(wav)
                media_type = "audio/wav"
                source.unlink(missing_ok=True)
            duration, codec = media_info(playable)
            uploaded_media[token] = {
                "path": playable,
                "name": Path(file.filename or "测试音频").name,
                "size": size,
                "duration": duration,
                "codec": codec,
                "media_type": media_type,
                "converted": converted,
            }
            bridge._stream_server.register_diagnostic_media(token, playable, media_type)
            visible = {k: v for k, v in uploaded_media[token].items() if k != "path"}
            return {"token": token, **visible}
        except Exception:
            source.unlink(missing_ok=True)
            (upload_dir / f"{token}.wav").unlink(missing_ok=True)
            raise
        finally:
            await file.close()

    @router.get("/media/{token}")
    async def serve_test_media(token: str):
        media = uploaded_media.get(token)
        if not media or not media["path"].is_file():
            raise HTTPException(status_code=404, detail="测试音频已失效，请重新上传")
        return FileResponse(
            media["path"],
            media_type=media["media_type"],
            headers={"Cache-Control": "no-store"},
        )

    @router.delete("/media/{token}")
    async def delete_test_media(token: str):
        media = uploaded_media.pop(token, None)
        bridge._stream_server.unregister_diagnostic_media(token)
        if media:
            # A speaker may still be closing its HTTP range request on Windows.
            # Revoke the token immediately; the process temp directory owns any
            # briefly locked file and removes it on the next service restart.
            with contextlib.suppress(OSError):
                media["path"].unlink(missing_ok=True)
        return {"ok": True}

    @router.post("/test/start")
    async def start_test(payload: dict):
        members = [str(item) for item in payload.get("device_ids", []) if item]
        known = {str(item.get("deviceID")) for item in await cloud_devices()}
        if not members or any(did not in known for did in members):
            raise HTTPException(status_code=400, detail="请选择可用的测试音箱")
        source = payload.get("source", "builtin")
        media_base = f"http://{settings.effective_stream_host}:{settings.stream_port}"
        if source == "builtin":
            url = f"{media_base}/diagnostic/builtin.wav"
        elif source == "upload":
            token = str(payload.get("media_token", ""))
            if token not in uploaded_media:
                raise HTTPException(status_code=404, detail="请重新上传测试音频")
            url = f"{media_base}/diagnostic/media/{token}"
        elif source == "url":
            try:
                url = await validate_http_url(str(payload.get("url", "")))
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        else:
            raise HTTPException(status_code=400, detail="未知测试音源")
        session_id = secrets.token_urlsafe(12)
        owner = f"debug:{session_id}"
        previous = {
            did: (device_manager.stream_url_of(did), device_manager.owner_of(did))
            for did in members
        }
        volume_results = await asyncio.gather(
            *(device_manager.get_volume(did, refresh=True) for did in members),
            return_exceptions=True,
        )
        volumes = {
            did: (value if isinstance(value, int) else None)
            for did, value in zip(members, volume_results, strict=True)
        }
        active_tests[session_id] = {
            "owner": owner,
            "members": members,
            "previous": previous,
            "source": source,
            "volumes": volumes,
        }
        results = await asyncio.gather(
            *(
                device_manager.play_stream(
                    did, url, owner=owner, force=True, audio_id=str(time.time_ns())
                )
                for did in members
            ),
            return_exceptions=True,
        )
        failed = sum(isinstance(item, Exception) or item is False for item in results)
        if failed:
            await stop_test(session_id)
            raise HTTPException(status_code=502, detail=f"{failed} 台音箱未能开始测试")
        return {"ok": True, "session_id": session_id, "members": members, "source": source}

    @router.post("/test/stop")
    async def stop_active_test(payload: dict):
        return await stop_test(
            str(payload.get("session_id", "")), bool(payload.get("restore", True))
        )

    @router.post("/codec-test")
    async def codec_test(payload: dict):
        members = [str(item) for item in payload.get("device_ids", []) if item]
        known = {str(item.get("deviceID")) for item in await cloud_devices()}
        if not members or any(did not in known for did in members):
            raise HTTPException(status_code=400, detail="请选择可用的检测音箱")
        fixtures = await asyncio.to_thread(ensure_codec_fixtures)
        previous = {
            did: (device_manager.stream_url_of(did), device_manager.owner_of(did))
            for did in members
        }
        results: dict[str, dict[str, bool | None]] = {did: {} for did in members}
        owner = f"codec-test:{secrets.token_urlsafe(8)}"
        try:
            for did in members:
                results[did] = await probe_device_formats(
                    bridge._stream_server,
                    device_manager,
                    did,
                    fixtures,
                    owner=owner,
                )
        finally:
            failed_restore = await _restore_streams(device_manager, previous)
        common = [
            fmt
            for fmt in CODEC_FORMATS
            if all(item.get(fmt) is True for item in results.values())
        ]
        return {
            "ok": True,
            "results": results,
            "common_formats": common,
            "restore_failed": failed_restore,
        }

    async def stop_live_calibration(group_id: str) -> list[str]:
        active = live_calibrations.pop(group_id, None)
        if not active:
            return []
        bridge._stream_server.end_delay_calibration(active["token"])

        async def bounded(operation):
            try:
                return await asyncio.wait_for(operation, timeout=8)
            except TimeoutError:
                return False

        await asyncio.gather(
            *(
                bounded(device_manager.stop_playback(did, owner=active["owner"]))
                for did in active["members"]
            ),
            return_exceptions=True,
        )
        return await _restore_streams(device_manager, active["previous"])

    @router.post("/groups/{group_id}/calibration/start")
    async def start_live_calibration(group_id: str, payload: dict | None = None):
        group = next((item for item in settings.groups if item.id == group_id), None)
        if group is None or len(group.speaker_ids) < 2 or not group.anchor_did:
            raise HTTPException(status_code=400, detail="请先设置包含两台音箱的时间基准")
        await stop_live_calibration(group_id)
        members = list(group.speaker_ids)
        token = secrets.token_urlsafe(12)
        calibration_pcm = None
        media_token = str((payload or {}).get("media_token", ""))
        if media_token:
            media = uploaded_media.get(media_token)
            if not media:
                raise HTTPException(status_code=404, detail="自定义测试音频已失效，请重新上传")
            wav = await asyncio.to_thread(transcode_file_to_wav, media["path"])
            calibration_pcm = wav[44:]
        session = bridge._stream_server.begin_delay_calibration(
            token, members, group.id, calibration_pcm
        )
        previous = {
            did: (device_manager.stream_url_of(did), device_manager.owner_of(did))
            for did in members
        }
        owner = f"calibration:{token}"
        live_calibrations[group_id] = {
            "token": token,
            "owner": owner,
            "members": members,
            "previous": previous,
        }
        base = f"http://{settings.effective_stream_host}:{settings.stream_port}"

        async def start_member(did: str):
            return await asyncio.wait_for(
                device_manager.play_stream(
                    did,
                    f"{base}/calibration/{token}/{did}.wav",
                    owner=owner,
                    force=True,
                    audio_id=str(time.time_ns()),
                ),
                timeout=8,
            )

        results = await asyncio.gather(
            *(start_member(did) for did in members), return_exceptions=True
        )
        if any(isinstance(item, Exception) or item is False for item in results):
            await stop_live_calibration(group_id)
            raise HTTPException(status_code=502, detail="部分音箱未能开始播放测试音")
        try:
            await asyncio.wait_for(session["ready"].wait(), timeout=5)
        except TimeoutError as exc:
            await stop_live_calibration(group_id)
            raise HTTPException(status_code=504, detail="部分音箱没有接入测试音") from exc
        return {"ok": True, "group": group.model_dump()}

    @router.post("/groups/{group_id}/calibration/stop")
    async def stop_calibration(group_id: str):
        failed = await stop_live_calibration(group_id)
        if failed:
            raise HTTPException(
                status_code=502,
                detail=f"校准已结束，但有 {len(failed)} 台音箱暂未恢复播放，系统正在自动重试",
            )
        return {"ok": True}

    @router.get("/device-status/{device_id}")
    async def device_status(device_id: str):
        """Read the speaker's real player state, including non-MiCast playback."""
        service = await device_manager.auth.ensure_service()
        if not service:
            raise HTTPException(status_code=401, detail="小米账号未登录")
        devices = await cloud_devices()
        if not any(str(item.get("deviceID")) == device_id for item in devices):
            raise HTTPException(status_code=404, detail="未找到音箱")
        status = await device_manager.cloud_api(device_id).get_status()
        return {
            "device_id": device_id,
            "name": device_manager.get_alias(device_id),
            "managed_by_micast": bool(device_manager.owner_of(device_id)),
            "owner": device_manager.owner_of(device_id),
            "stream_url": device_manager.stream_url_of(device_id),
            "status": status,
        }

    @router.get("/test-tone")
    async def test_tone():
        """Built-in test audio, served locally so the check needs no internet."""
        return Response(
            await asyncio.to_thread(test_tone_wav),
            media_type="audio/wav",
            headers={"Cache-Control": "no-store"},
        )

    @router.post("/groups/{group_id}/calibrate-delay")
    async def calibrate_group_delay(group_id: str):
        """Run an explicit, one-shot coarse delay calibration.

        Every Xiaomi speaker fetches the same local test tone. Request skew is
        returned only as a rough magnitude reference: it cannot reveal the
        audible direction hidden inside each speaker's decoder buffer. The UI
        asks the listener to confirm that direction before changing the normal
        editable delay. Previous live URLs are restored after the short sample.
        """
        group = next((item for item in settings.groups if item.id == group_id), None)
        if group is None:
            raise HTTPException(status_code=404, detail="未找到音箱组合")
        members = list(group.speaker_ids)
        if len(members) < 2 or not group.anchor_did or group.anchor_did not in members:
            raise HTTPException(status_code=400, detail="请先为组合设置时间基准")

        token = secrets.token_urlsafe(12)
        session = bridge._stream_server.begin_delay_calibration(token, members, group.id)
        previous = {
            did: (device_manager.stream_url_of(did), device_manager.owner_of(did))
            for did in members
        }
        base = f"http://{settings.effective_stream_host}:{settings.stream_port}"
        calibration_owner = f"calibration:{token}"
        sample_started = time.monotonic()
        try:
            started = await asyncio.gather(
                *(
                    device_manager.play_stream(
                        did,
                        f"{base}/calibration/{token}/{did}.wav",
                        owner=calibration_owner,
                        force=True,
                        audio_id=str(time.time_ns()),
                    )
                    for did in members
                ),
                return_exceptions=True,
            )
            if any(isinstance(item, Exception) or item is False for item in started):
                raise HTTPException(status_code=502, detail="部分音箱未能开始播放测试音")
            try:
                await asyncio.wait_for(session["ready"].wait(), timeout=3.5)
            except TimeoutError as exc:
                missing = session["expected"] - set(session["arrivals"])
                raise HTTPException(
                    status_code=504,
                    detail=f"未收到 {len(missing)} 台音箱的测试连接，请确认音箱在线",
                ) from exc

            # Let the recognizable sample play briefly before restoring the
            # content that was active when calibration began.
            await asyncio.sleep(max(0.0, 3.0 - (time.monotonic() - sample_started)))
            request_offsets = _request_offsets(session["arrivals"], group.anchor_did, members)
            spread_ms = round(
                (max(session["arrivals"].values()) - min(session["arrivals"].values())) * 1000
            )
            return {
                "ok": True,
                "group": group.model_dump(),
                "measured_spread_ms": spread_ms,
                "request_offsets_ms": request_offsets,
            }
        finally:
            bridge._stream_server.end_delay_calibration(token)
            # Stop the finite test explicitly.  A failed measurement must not
            # leave a cached/queued test item playing beyond the promised
            # sample window.
            await asyncio.gather(
                *(device_manager.stop_playback(did, owner=calibration_owner) for did in members),
                return_exceptions=True,
            )
            await _restore_streams(device_manager, previous)

    @router.get("/state")
    async def debug_state(
        log_source: LogSource = "app",
        log_level: LogLevel = "all",
        log_since: Annotated[float | None, Query()] = None,
        log_until: Annotated[float | None, Query()] = None,
    ):
        """Live state plus the log tail the panel draws.

        The interval bounds are absolute epoch seconds and optional: without
        them the panel follows the whole buffer, and the page freezes a window
        by sending the pair it resolved against `server_time`.
        """
        state = await collect_state(bridge, device_manager)
        state["logs"] = log_payload(LogQuery(log_source, log_level, log_since, log_until))
        return state

    @router.get("/report")
    async def download_report(
        log_source: LogSource = "app",
        log_level: LogLevel = "all",
        log_window: LogWindow = "15m",
        log_since: Annotated[float | None, Query()] = None,
        log_until: Annotated[float | None, Query()] = None,
    ):
        """Sanitized diagnostic bundle: settings + state + the selected logs.

        The log section is exactly the interval asked for — a duration preset
        resolved now, or an absolute pair — bounded by what the buffer still
        holds, which `log_scope.truncated` reports.
        """
        query = resolve_query(
            source=log_source, level=log_level, window=log_window, since=log_since, until=log_until
        )
        report = await build_report(bridge, device_manager, query=query)
        filename = f"micast-diagnostic-{time.strftime('%Y%m%d-%H%M%S')}.json"
        return Response(
            json.dumps(report, ensure_ascii=False, indent=2),
            media_type="application/json",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                # So the toast can say how many records it just handed over
                # without parsing the file back out of the blob.
                "X-MiCast-Log-Count": str(report["log_scope"]["count"]),
            },
        )

    @router.get("/logs.txt")
    async def copy_logs(
        log_source: LogSource = "app",
        log_level: LogLevel = "all",
        log_window: LogWindow = "15m",
        log_since: Annotated[float | None, Query()] = None,
        log_until: Annotated[float | None, Query()] = None,
    ):
        """The same selection as the report, as plain text for the clipboard."""
        query = resolve_query(
            source=log_source, level=log_level, window=log_window, since=log_since, until=log_until
        )
        selection = runtime_logs.select(query)
        return Response(
            build_log_text(query, selection),
            media_type="text/plain; charset=utf-8",
            headers={"X-MiCast-Log-Count": str(selection.total)},
        )

    @router.post("/logs/clear")
    async def clear_logs():
        """Drop the in-memory log buffer so the next run records a clean window."""
        return {"cleared": runtime_logs.clear()}

    @router.post("/tts")
    async def debug_tts(payload: dict):
        text = payload.get("text", "调试测试")
        device_id = payload.get("device_id") or device_manager.selected_device_id
        service = await device_manager.auth.ensure_service()
        if not service or not device_id:
            raise HTTPException(status_code=400, detail="No device selected or not logged in")

        devices = await cloud_devices()
        device = next((item for item in devices if item.get("deviceID") == device_id), None)
        if not device:
            raise HTTPException(status_code=404, detail="没有找到所选音箱")

        # Cloud TTS is silently ignored by several Xiaomi firmwares while the
        # media player is active. Pause it before either command path.
        status = await service.player_get_status(device_id)
        try:
            info = json.loads((status.get("data") or {}).get("info") or "{}")
        except (AttributeError, TypeError, ValueError):
            info = {}
        was_playing = info.get("status") == 2
        if was_playing:
            pause_result = await service.player_pause(device_id)
            if not _mina_command_accepted(pause_result):
                raise HTTPException(status_code=502, detail="音箱没有接受暂停命令，请稍后重试")
            await asyncio.sleep(0.35)

        hardware = str(device.get("hardware") or "").upper()
        miot_action = _MIOT_TTS_ACTIONS.get(hardware)
        accepted = False
        command_path = "MiNA"
        if miot_action and device.get("miotDID"):
            try:
                miot = await device_manager.auth.ensure_miot_service()
                code = (
                    await miot.miot_action(str(device["miotDID"]), miot_action, [text])
                    if miot
                    else -1
                )
                accepted = code == 0
                command_path = f"MIoT {miot_action[0]}-{miot_action[1]}"
                if not accepted:
                    logger.warning(
                        "Xiaomi MIoT TTS rejected for %s (%s): code=%s",
                        device_id,
                        hardware,
                        code,
                    )
            except Exception:
                logger.exception("Xiaomi MIoT TTS failed for %s (%s)", device_id, hardware)

        if not accepted:
            result = await service.text_to_speech(device_id, text)
            accepted = _mina_command_accepted(result)
            command_path = "MiNA"
            if not accepted:
                logger.warning("Xiaomi TTS rejected for %s: %r", device_id, result)
                raise HTTPException(status_code=502, detail="音箱没有接受语音命令，请确认音箱在线")
        logger.info(
            "Xiaomi TTS accepted for %s via %s (paused_media=%s)",
            device_id,
            command_path,
            was_playing,
        )
        if was_playing:
            # The phrase is intentionally short. Give it room to finish, then
            # return the prior media player to its previous state.
            await asyncio.sleep(2.5)
            resume_result = await service.player_play(device_id)
            if not _mina_command_accepted(resume_result):
                logger.warning("Could not resume media after Xiaomi TTS for %s", device_id)
        return {"ok": True, "paused_media": was_playing}

    @router.post("/play_url")
    async def debug_play_url(payload: dict):
        url = payload.get("url")
        method = payload.get("method", "music_url")
        if not url:
            raise HTTPException(status_code=400, detail="url required")
        try:
            url = await validate_http_url(str(url))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        service = await device_manager.auth.ensure_service()
        if not service or not device_manager.selected_device_id:
            raise HTTPException(status_code=400, detail="No device selected or not logged in")
        if method == "music_url":
            result = await service.play_by_music_url(device_manager.selected_device_id, url)
        else:
            result = await service.play_by_url(device_manager.selected_device_id, url, _type=1)
        # Let status surfaces (topology, playback bar) see playback started
        # outside the stream pipeline; the watchdog clears it when it ends.
        device_manager.note_manual_play(device_manager.selected_device_id, url)
        return {"ok": True, "result": result}

    @router.post("/action")
    async def debug_action(payload: dict):
        action = payload.get("action")
        service = await device_manager.auth.ensure_service()
        if not service or not device_manager.selected_device_id:
            raise HTTPException(status_code=400, detail="No device selected or not logged in")
        did = device_manager.selected_device_id
        if action == "pause":
            result = await device_manager.stop(did)
        elif action == "play":
            result = await device_manager.resume(did)
        elif action == "stop":
            # Full stop: speaker stops and MiCast forgets the stream state,
            # so the playback bar disappears instead of sticking on 已暂停.
            await device_manager.stop_playback(did)
            result = {"stopped": did, "name": device_manager.get_alias(did)}
        elif action == "volume":
            volume = payload.get("volume", 50)
            result = await device_manager.set_volume(did, volume)
        else:
            raise HTTPException(status_code=400, detail="unknown action")
        return {"ok": True, "result": result}

    @router.post("/pipelines/refresh")
    async def refresh_pipelines():
        """Full engine teardown + rebuild — the same rebuild an AirPlay 2
        toggle triggers. Clears stuck sessions, stale pipelines and other
        half-broken states; playback drops briefly."""
        await bridge.restart()
        return {"ok": True, "status": bridge.status.get("status", "unknown")}

    @router.post("/stream/{receiver_id}/kick")
    async def kick_stream(receiver_id: str):
        result = await stop_output(bridge, device_manager, receiver_id)
        return {"ok": True, "sender_sessions": result["disconnected"], **result}

    return router
