"""Protocol status and lifecycle coordination shared by management operations."""

from micast.config import settings
from micast.config_apply import run_config_runtime
from micast.deployment import airplay2_available, classic_ingress_available


def protocol_status(bridge, dlna) -> dict:
    def summarize(enabled, entries, supported=True):
        if not supported:
            return {"status": "unsupported", "detail": "当前安装方式不支持"}
        if not enabled:
            return {"status": "disabled", "detail": "用户已关闭"}
        if not entries:
            return {"status": "idle", "detail": "尚未配置播放入口"}
        errors = [
            item.get("detail", "启动失败")
            for item in entries
            if item.get("status") in {"error", "failed", "blocked"}
        ]
        ready = any(
            item.get("status") in {"running", "ready", "idle", "streaming"} for item in entries
        )
        return {
            "status": "degraded" if errors and ready else "ready" if ready else "blocked",
            "detail": "；".join(errors) if errors else "可连接" if ready else "接收入口尚未就绪",
        }

    provider = getattr(bridge, "_local_provider", None)
    classic = (
        [{"status": item.status, "detail": item.detail} for item in provider.receivers.values()]
        if provider
        else []
    )
    runtime = getattr(bridge, "_airplay2_runtime", {})
    return {
        "airplay": summarize(settings.airplay_enabled, classic, classic_ingress_available()),
        "airplay2": summarize(
            settings.airplay2_enabled, list(runtime.values()), airplay2_available()
        ),
        "dlna": {
            "status": "unsupported"
            if not classic_ingress_available()
            else "disabled"
            if not settings.dlna_enabled
            else "ready"
            if dlna and dlna.status == "running"
            else "blocked",
            "detail": dlna.detail if dlna else "DLNA 服务不可用",
        },
    }


class ProtocolUnavailable(ValueError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class ProtocolRuntime:
    def __init__(self, bridge, dlna=None):
        self.bridge = bridge
        self.dlna = dlna

    async def reconcile_dlna(self):
        if self.dlna is None:
            return
        listener = getattr(self.dlna, "http_listener", None)
        if listener:
            if settings.dlna_enabled and classic_ingress_available():
                await listener.start()
            else:
                await self.dlna.stop()
                await listener.stop()
        await self.dlna.reconcile()

    async def restart_dlna_listener(self):
        if self.dlna is None:
            return
        listener = getattr(self.dlna, "http_listener", None)
        await self.dlna.stop()
        if listener:
            await listener.stop()
        await self.reconcile_dlna()

    async def retry(self, protocol):
        async def recover():
            if protocol not in {"airplay", "airplay2", "dlna"}:
                raise ProtocolUnavailable(400, "未知投送协议")
            enabled = getattr(settings, f"{protocol}_enabled")
            supported = (
                airplay2_available() if protocol == "airplay2" else classic_ingress_available()
            )
            if not enabled or not supported or (protocol == "dlna" and not self.dlna):
                raise ProtocolUnavailable(409, "请先开启该功能；当前安装方式也需支持")
            if protocol == "airplay":
                await self.bridge.reconcile_classic_feature()
            elif protocol == "airplay2":
                await self.bridge.reconcile_airplay2()
            else:
                await self.reconcile_dlna()
            return protocol_status(self.bridge, self.dlna)

        return await run_config_runtime(recover)
