"""Configuration routes."""

import contextlib
import json
import logging
import os

from fastapi import APIRouter, HTTPException

from micast.access import AccessManager
from micast.audio_bridge import AudioBridge
from micast.config import (
    EDITABLE_PORTS,
    default_data_dir,
    default_log_dir,
    env_pinned,
    settings,
    storage_mode,
)
from micast.config_apply import apply_config_transaction
from micast.deployment import (
    airplay2_available,
    airplay2_mode,
    classic_ingress_available,
    deployment_mode,
)
from micast.dlna import DlnaService
from micast.protocol_runtime import (
    ProtocolRuntime,
    ProtocolUnavailable,
)
from micast.protocol_runtime import (
    protocol_status as _protocol_status,
)
from micast.raop import server as raop_server
from micast.ports import AIRPLAY2_RECEIVER_PORT
from micast.routes.models import AudioConfigResponse, ConfigResponse
from micast.xiaomi.auth import XiaomiAuth
from micast.xiaomi.device_manager import DeviceManager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/config", tags=["config"])

# Local state wiped by /reset (清空数据). Matches the migration file list in
# config.py — keep both in sync when adding new persisted files.
RESET_FILES = ("micast.json", "access.json", "xiaomi-account.json", "xiaomi-tokens.enc")


def _dlna_recast_required(dlna: DlnaService | None) -> bool:
    """Return whether an existing DLNA media session has settings latched."""

    # The mode is captured by SetAVTransportURI. A stopped or paused item can
    # still be resumed with the old mode, so an existing URI — not only the
    # PLAYING state — means the controller needs to re-cast the media.
    return bool(dlna and any(state.uri for state in dlna.states.values()))



def _port_mode(field: str) -> str:
    """auto (default) / custom (user-set) / env (pinned by a real env var)."""
    env_var, _ = EDITABLE_PORTS[field]
    if env_pinned(env_var):
        return "env"
    if field in settings.strict_ports:
        return "custom"
    return "auto"


def _preferred(field: str, fallback: int) -> int:
    if field in ("port", "stream_port"):
        return settings.preferred_port(field)
    return getattr(settings, field) or fallback


def _shairport_actual_port() -> int | None:
    """The port the bundled shairport-sync actually bound (fnOS single mode)."""
    try:
        data = json.loads((default_data_dir() / "airplay2-ready.json").read_text(encoding="utf-8"))
        from micast.fnos_receiver import owns_listener
        port = int(data["port"])
        return port if port == AIRPLAY2_RECEIVER_PORT and owns_listener(int(data["pid"]), port) else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _ports_report(bridge: AudioBridge, dlna: DlnaService | None) -> list[dict]:
    """Per-service port status for the settings page, pruned by deployment."""
    unix_socket = os.environ.get("MICAST_UNIX_SOCKET", "").strip()
    ap2_mode = airplay2_mode()
    entries: list[dict] = []

    if unix_socket:
        entries.append(
            {
                "id": "port",
                "name": "管理界面",
                "protocol": "tcp",
                "mode": "fixed",
                "preferred": None,
                "actual": None,
                "status": "hosted",
                "detail": "由 fnOS 统一网关托管，无需配置",
                "editable": False,
            }
        )
        entries.append(
            {
                "id": "dlna_http",
                "name": "DLNA 控制端口",
                "protocol": "tcp",
                "mode": _port_mode("port"),
                "preferred": _preferred("port", 42300),
                "actual": settings.port if dlna and dlna.http_available else None,
                "status": "listening" if dlna and dlna.status == "running" else "off",
                "detail": dlna.http_detail
                if dlna and not dlna.http_available
                else "独立于飞牛管理入口，可修改后重试",
                "editable": not env_pinned("MICAST_PORT"),
            }
        )
    else:
        entries.append(
            {
                "id": "port",
                "name": "管理界面",
                "protocol": "tcp",
                "mode": _port_mode("port"),
                "preferred": _preferred("port", 42300),
                "actual": settings.port,
                "status": "listening",
                "detail": "浏览器访问端口；修改后需重启应用",
                "editable": not env_pinned("MICAST_PORT"),
            }
        )

    entries.append(
        {
            "id": "stream_port",
            "name": "音频流服务",
            "protocol": "tcp",
            "mode": _port_mode("stream_port"),
            "preferred": _preferred("stream_port", 42400),
            "actual": settings.stream_port,
            "status": "listening",
            "detail": "音箱拉取音频流；被占用时自动顺延",
            "editable": not env_pinned("MICAST_STREAM_PORT"),
        }
    )

    rtsp_ports = sorted(raop_server._reserved_rtsp_ports)
    entries.append(
        {
            "id": "airplay_rtsp_port",
            "name": "AirPlay RTSP",
            "protocol": "tcp",
            "mode": _port_mode("airplay_rtsp_port"),
            "preferred": _preferred("airplay_rtsp_port", 42500),
            "actual": rtsp_ports[0] if rtsp_ports else None,
            "status": "listening" if rtsp_ports else "off",
            "detail": "经典 AirPlay 会话端口；被占用时自动顺延",
            "editable": not env_pinned("MICAST_AIRPLAY_RTSP_PORT"),
        }
    )

    udp_base, udp_top = raop_server.udp_pool()
    udp_in_use = sorted(raop_server._reserved_udp_bases)
    entries.append(
        {
            "id": "airplay_udp_base",
            "name": "AirPlay 音频通道",
            "protocol": "udp",
            "mode": _port_mode("airplay_udp_base"),
            "preferred": _preferred("airplay_udp_base", 42600),
            "actual": udp_in_use or None,
            "status": "listening" if udp_in_use else "off",
            "detail": f"每个 AirPlay 会话占 3 个 UDP 端口，范围 {udp_base}-{udp_top}",
            "editable": not env_pinned("MICAST_AIRPLAY_UDP_BASE"),
        }
    )

    if airplay2_available():
        if ap2_mode == "single" and deployment_mode() == "fnos":
            sources = getattr(bridge, "_airplay2_sources", {})
            actual = _shairport_actual_port() if any(
                getattr(source, "alive", False) for source in sources.values()
            ) else None
            enabled = settings.airplay2_enabled
            failures = [item.get("detail", "接收器启动失败") for item in
                        getattr(bridge, "_airplay2_runtime", {}).values()
                        if item.get("status") in {"error", "failed", "blocked"}]
            entries.append(
                {
                    "id": "airplay2_port",
                    "name": "AirPlay 2",
                    "protocol": "tcp",
                    "mode": "fixed",
                    "preferred": AIRPLAY2_RECEIVER_PORT,
                    "actual": actual,
                    "status": "listening" if (enabled and actual) else "error" if enabled and failures else "off",
                    "detail": "；".join(failures) if enabled and failures else "当前接收器固定使用 TCP 7000；被占用时仅 AirPlay 2 不可用，释放后可重新启动",
                    "editable": False,
                }
            )
            entries.append(
                {
                    "id": "nqptp",
                    "name": "AirPlay 2 时钟同步",
                    "protocol": "udp",
                    "mode": "fixed",
                    "preferred": None,
                    "actual": [319, 320],
                    "status": "listening" if enabled and actual else "off",
                    "detail": "NQPTP PTP 时钟同步；由 AirPlay 2 使用",
                    "editable": False,
                }
            )
        else:
            entries.append(
                {
                    "id": "airplay2_port",
                    "name": "AirPlay 2",
                    "protocol": "tcp",
                    "mode": "fixed",
                    "preferred": AIRPLAY2_RECEIVER_PORT,
                    "actual": None,
                    "status": "hosted",
                    "detail": "独立接收器固定使用 TCP 7000；容器桥接网络可映射主机端口，端口冲突请检查部署配置",
                    "editable": False,
                }
            )

    provider = getattr(bridge, "_local_provider", None)
    mdns_running = bool(provider and provider.zeroconf is not None)
    sources = getattr(bridge, "_airplay2_sources", {})
    if deployment_mode() == "fnos" and any(
        getattr(source, "alive", False) for source in sources.values()
    ):
        mdns_running = mdns_running or _shairport_actual_port() is not None
    entries.append(
        {
            "id": "mdns",
            "name": "mDNS 服务发现",
            "protocol": "udp",
            "mode": "fixed",
            "preferred": None,
            "actual": 5353 if mdns_running else None,
            "status": "listening" if mdns_running else "off",
            "detail": "AirPlay 公告与设备发现，可与其他 mDNS 服务共存",
            "editable": False,
        }
    )

    dlna_status = dlna.status if dlna else "unavailable"
    entries.append(
        {
            "id": "ssdp",
            "name": "DLNA/SSDP 发现",
            "protocol": "udp",
            "mode": "fixed",
            "preferred": None,
            "actual": 1900,
            "status": {
                "running": "listening",
                "stopped": "off",
                "error": "error",
            }.get(dlna_status, "off"),
            "detail": dlna.detail if dlna else "DLNA 服务不可用",
            "editable": False,
        }
    )
    if not classic_ingress_available():
        entries = [entry for entry in entries if entry["id"] not in {
            "airplay_rtsp_port", "airplay_udp_base", "mdns", "ssdp"
        }]
    return entries


def install(
    bridge: AudioBridge,
    dlna: DlnaService | None = None,
    auth: XiaomiAuth | None = None,
    access: AccessManager | None = None,
    device_manager: DeviceManager | None = None,
) -> APIRouter:
    protocols = ProtocolRuntime(bridge, dlna)

    async def persist_only() -> None:
        return None

    @router.get("/audio", response_model=AudioConfigResponse)
    async def get_audio_config():
        return settings.audio.model_dump()

    @router.post("/audio")
    async def set_audio_config(payload: dict):
        allowed = {"format", "bitrate", "sample_rate", "auto_transcode"}
        updates = {k: v for k, v in payload.items() if k in allowed}
        try:
            await apply_config_transaction(
                lambda: settings.update_audio(**updates), bridge.apply_config_change
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="音频编码设置无效") from exc
        # The plan diff routes this to per-pipeline encoder restarts for
        # classic entries AND rebuilds of AirPlay 2 pipelines (which used to
        # keep the old format until a full restart).
        return settings.audio.model_dump()

    @router.get("", response_model=ConfigResponse)
    async def get_config():
        runtime_epoch = getattr(getattr(bridge, "runtime_snapshot", None), "epoch", None)
        return {
            "config_revision": settings.config_revision,
            "runtime_epoch": runtime_epoch if isinstance(runtime_epoch, str) else None,
            "deployment": deployment_mode(),
            "audio": settings.audio.model_dump(),
            "app": settings.app.model_dump(),
            "receiver_mode": settings.receiver_mode,
            "airplay_protocol": settings.airplay_protocol,
            "airplay_engine": settings.airplay_engine,
            "dlna_enabled": settings.dlna_enabled,
            "airplay_enabled": settings.airplay_enabled,
            "protocol_status": _protocol_status(bridge, dlna),
            "sync_groups_enabled": settings.sync_groups_enabled,
            "large_delay_enabled": settings.large_delay_enabled,
            "touchscreen_lyrics": settings.touchscreen_lyrics,
            "default_volume": settings.default_volume,
            "default_volume_enabled": settings.default_volume_enabled,
            "sender_volume_mode": settings.sender_volume_mode,
            "notify_webhook_url": settings.notify_webhook_url,
            "airplay2_enabled": settings.airplay2_enabled,
            "network_discovery_enabled": settings.network_discovery_enabled,
            "airplay2_available": airplay2_available(),
            "airplay2_mode": airplay2_mode(),
            "airplay2_can_add_instances": airplay2_mode() == "multi",
            "storage": {
                "mode": storage_mode(),
                "data_dir": str(default_data_dir()),
                "log_dir": str(default_log_dir()),
                "shared_dir": os.environ.get("MICAST_SHARED_DIR"),
            },
            "dlna_status": {
                "status": dlna.status if dlna else "unavailable",
                "detail": dlna.detail if dlna else "DLNA 服务不可用",
            },
            "selected_device_id": settings.selected_device_id,
            "ports": _ports_report(bridge, dlna),
            "receivers": [item.model_dump() for item in settings.receivers],
            "groups": [item.model_dump() for item in settings.groups],
            # Persisted aliases let the UI name speakers before the live
            # device list finishes loading.
            "speaker_names": {
                speaker.did: speaker.alias for speaker in settings.speakers if speaker.alias
            },
        }

    @router.post("/app-name")
    async def set_app_name(payload: dict):
        name = payload.get("name")
        if not name or not isinstance(name, str):
            raise HTTPException(status_code=400, detail="name required")
        await apply_config_transaction(lambda: settings.update_app_name(name), persist_only)
        return settings.app.model_dump()

    @router.post("/receiver-mode")
    async def set_receiver_mode(payload: dict):
        mode = payload.get("mode")
        if mode not in ("single", "multi"):
            raise HTTPException(status_code=400, detail="mode must be 'single' or 'multi'")
        await apply_config_transaction(lambda: settings.set_receiver_mode(mode), bridge.restart)
        return {"receiver_mode": mode}

    @router.post("/airplay-protocol")
    async def set_airplay_protocol(payload: dict):
        protocol = payload.get("protocol")
        if protocol not in ("auto", "classic", "airplay2"):
            raise HTTPException(
                status_code=400, detail="protocol must be auto, classic, or airplay2"
            )
        await apply_config_transaction(
            lambda: settings.set_airplay_protocol(protocol), bridge.restart
        )
        return {"airplay_protocol": protocol}

    @router.post("/airplay")
    async def set_airplay(payload: dict):
        enabled = payload.get("enabled")
        if not isinstance(enabled, bool):
            raise HTTPException(status_code=400, detail="enabled boolean required")
        if enabled and not classic_ingress_available():
            raise HTTPException(status_code=409, detail="当前隔离网络部署不支持经典 AirPlay 接收")
        await apply_config_transaction(lambda: settings.set_airplay_enabled(enabled),
                                       bridge.reconcile_classic_feature)
        return {"airplay_enabled": enabled, "protocol_status": _protocol_status(bridge, dlna)}

    @router.post("/protocols/retry")
    async def retry_protocol(payload: dict):
        try:
            status = await protocols.retry(payload.get("protocol"))
        except ProtocolUnavailable as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
        return {"protocol_status": status}

    @router.post("/dlna")
    async def set_dlna(payload: dict):
        enabled = payload.get("enabled")
        if not isinstance(enabled, bool):
            raise HTTPException(status_code=400, detail="enabled boolean required")

        await apply_config_transaction(
            lambda: settings.set_dlna_enabled(enabled), protocols.reconcile_dlna
        )
        return {"dlna_enabled": enabled}

    @router.post("/sync-groups")
    async def set_sync_groups(payload: dict):
        enabled = payload.get("enabled")
        if not isinstance(enabled, bool):
            raise HTTPException(status_code=400, detail="enabled boolean required")

        async def apply_groups() -> None:
            await bridge.apply_config_change()
            if dlna:
                await dlna.reconcile()

        await apply_config_transaction(
            lambda: settings.set_sync_groups_enabled(enabled), apply_groups
        )
        return {"sync_groups_enabled": enabled}

    @router.post("/large-delay")
    async def set_large_delay(payload: dict):
        enabled = payload.get("enabled")
        if not isinstance(enabled, bool):
            raise HTTPException(status_code=400, detail="enabled boolean required")
        await apply_config_transaction(
            lambda: settings.set_large_delay_enabled(enabled), persist_only
        )
        return {"large_delay_enabled": enabled}

    @router.post("/touchscreen-lyrics")
    async def set_touchscreen_lyrics(payload: dict):
        enabled = payload.get("enabled")
        if not isinstance(enabled, bool):
            raise HTTPException(status_code=400, detail="enabled boolean required")
        await apply_config_transaction(
            lambda: settings.set_touchscreen_lyrics(enabled), persist_only
        )
        return {"touchscreen_lyrics": enabled}

    @router.post("/default-volume")
    async def set_default_volume(payload: dict):
        try:
            volume = max(0, min(100, int(payload.get("volume", 0))))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="volume must be 0-100") from None
        enabled = payload.get("enabled", settings.default_volume_enabled)
        if not isinstance(enabled, bool):
            raise HTTPException(status_code=400, detail="enabled boolean required")
        def mutate_default_volume() -> None:
            settings.default_volume_enabled = enabled
            settings.set_default_volume(volume)

        await apply_config_transaction(mutate_default_volume, persist_only)
        return {"default_volume": volume, "default_volume_enabled": enabled}

    @router.post("/stale-session-timeout")
    async def set_stale_session_timeout(payload: dict):
        """Seconds a paused AirPlay session may idle before the sweeper ends
        it; 0 disables the expiry. The sweeper reads settings every pass, so
        this hot-applies without an engine restart."""
        raw = payload.get("seconds", payload.get("timeout"))
        try:
            seconds = int(raw)
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=400, detail="timeout must be a non-negative integer"
            ) from None
        if seconds < 0:
            raise HTTPException(status_code=400, detail="timeout must be a non-negative integer")
        try:
            await apply_config_transaction(
                lambda: settings.set_stale_session_timeout(seconds), persist_only
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"stale_session_timeout": settings.stale_session_timeout}

    @router.post("/client-max-lag")
    async def set_client_max_lag(payload: dict):
        """Speaker clock-drift tolerance in seconds: how far a pulled stream
        may lead real time before the delay line trims it back to live. The
        stream server reads it per chunk, so this hot-applies."""
        raw = payload.get("seconds")
        try:
            seconds = float(raw)
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=400, detail="seconds must be a number between 0.5 and 30"
            ) from None
        try:
            await apply_config_transaction(
                lambda: settings.set_client_max_lag_seconds(seconds), persist_only
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"client_max_lag_seconds": settings.client_max_lag_seconds}

    @router.post("/sender-volume")
    async def set_sender_volume(payload: dict):
        mode = payload.get("mode")
        if mode not in ("independent", "linked"):
            raise HTTPException(status_code=400, detail="无效的音量控制方式")
        await apply_config_transaction(
            lambda: (setattr(settings, "sender_volume_mode", mode), settings.save_to_file()),
            persist_only,
        )
        return {
            "sender_volume_mode": mode,
            "dlna_recast_required": _dlna_recast_required(dlna),
        }

    @router.post("/notify-webhook")
    async def set_notify_webhook(payload: dict):
        url = str(payload.get("url", "")).strip()
        if url and not url.startswith(("http://", "https://")):
            raise HTTPException(status_code=400, detail="url must be http(s)")
        await apply_config_transaction(lambda: settings.set_notify_webhook(url), persist_only)
        return {"notify_webhook_url": url}

    @router.post("/network-discovery")
    async def set_network_discovery(payload: dict):
        enabled = payload.get("enabled")
        if not isinstance(enabled, bool):
            raise HTTPException(status_code=400, detail="enabled boolean required")

        async def apply_discovery() -> None:
            await bridge.set_network_discovery(settings.network_discovery_enabled)

        await apply_config_transaction(
            lambda: settings.set_network_discovery_enabled(enabled), apply_discovery
        )
        return {"network_discovery_enabled": enabled}

    @router.post("/airplay2")
    async def set_airplay2_enabled(payload: dict):
        enabled = payload.get("enabled")
        if not isinstance(enabled, bool):
            raise HTTPException(status_code=400, detail="enabled boolean required")
        if enabled and not airplay2_available():
            raise HTTPException(status_code=409, detail="当前安装方式不支持 AirPlay 2")

        class AirPlay2ApplyError(Exception):
            """Expected apply failure; surfaced to the client as a clean 502."""

        async def reconcile_airplay2_setting() -> None:
            if not settings.airplay2_enabled:
                try:
                    await bridge.shutdown_airplay2()
                except Exception as exc:
                    raise AirPlay2ApplyError(
                        f"AirPlay 2 实例尚未全部停止：{exc}"
                    ) from exc
            else:
                await bridge.reconcile_airplay2()
                if airplay2_mode() == "single":
                    runtime = bridge.status.get("airplay2_instances", [])
                    live = next(iter(runtime), {})
                    if live.get("status") != "running":
                        detail = str(live.get("detail") or "原生 AirPlay 2 接收器启动失败")
                        raise AirPlay2ApplyError(detail)
            # Sync the plan snapshot with the explicit start/stop above.
            await bridge.apply_config_change()

        async def rollback_airplay2() -> None:
            # Only tear down what is actually still running: if the apply
            # failed mid-shutdown there is nothing left to stop, and a second
            # shutdown call would just repeat the same failure.
            runtime = bridge.status.get("airplay2_instances", [])
            if any(item.get("status") == "running" for item in runtime):
                with contextlib.suppress(Exception):
                    await bridge.shutdown_airplay2()
            with contextlib.suppress(Exception):
                await bridge.apply_config_change()

        try:
            await apply_config_transaction(
                lambda: settings.set_airplay2_enabled(enabled),
                reconcile_airplay2_setting,
                rollback_airplay2,
            )
        except AirPlay2ApplyError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=502, detail="AirPlay 2 设置未能生效，请稍后重试"
            ) from exc
        return {"airplay2_enabled": enabled}

    @router.post("/ports")
    async def set_port(payload: dict):
        key = str(payload.get("id") or "")
        if key == "dlna_http" and os.environ.get("MICAST_UNIX_SOCKET", "").strip():
            key = "port"
        if key not in EDITABLE_PORTS:
            raise HTTPException(status_code=400, detail=f"未知端口项: {key or '(空)'}")
        if key == "airplay2_port":
            raise HTTPException(status_code=409, detail="当前 AirPlay 2 接收器固定使用 TCP 7000，不能自动分配或修改；释放端口后可重新启动")
        env_var = EDITABLE_PORTS[key][0]
        if env_pinned(env_var):
            raise HTTPException(
                status_code=409, detail=f"{env_var} 环境变量已固定该端口，无法在此修改"
            )
        raw = payload.get("port")
        if raw in ("", None):
            value = None
        else:
            try:
                value = int(raw)
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="端口必须是数字") from None
        restart_required = False

        async def apply_port() -> None:
            nonlocal restart_required
            if key == "port":
                listener = getattr(dlna, "http_listener", None) if dlna else None
                if listener and settings.dlna_enabled:
                    await protocols.restart_dlna_listener()
                    return
                # uvicorn can't rebind a running listener; restart to apply.
                restart_required = True
            elif key == "stream_port":
                await bridge.restart_stream_server()
            elif key in ("airplay_rtsp_port", "airplay_udp_base"):
                raop_server.configure_ports(settings.airplay_rtsp_port, settings.airplay_udp_base)
                await bridge.restart()
            elif key == "airplay2_port" and settings.airplay2_enabled:
                await bridge.restart()

        try:
            await apply_config_transaction(
                lambda: settings.set_ports({key: value}), apply_port
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "ok": True,
            "restart_required": restart_required,
            "ports": _ports_report(bridge, dlna),
        }

    @router.post("/reset")
    async def reset_all():
        """清空数据：删除本地配置、米家登录与管理账号，回到初始引导页。"""
        logger.warning("Resetting all local data on user request")
        await bridge.stop()
        if dlna:
            await dlna.stop()
        if auth:
            auth.logout()
        for name in RESET_FILES:
            with contextlib.suppress(OSError):
                (default_data_dir() / name).unlink()
        settings.reset_runtime()
        settings.configure_airplay2_deployment(airplay2_mode())
        if access:
            access.reset()
        if device_manager:
            device_manager.reset()
        await bridge.start()
        if dlna:
            await dlna.start()
        return {"ok": True}

    return router
