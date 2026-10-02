"""Resolve desired AirPlay targets into local or orchestrated PCM sources."""

import asyncio
import logging
from dataclasses import dataclass

from micast.config import settings
from micast.orchestration import DesiredReceiver, OrchestratorClient
from micast.pcm_source import PCMSource, create_pcm_source

logger = logging.getLogger(__name__)


@dataclass
class Receiver:
    device_id: str
    name: str
    pcm_source: PCMSource
    status: str = "idle"
    detail: str = ""


class ReceiverManager:
    """Reconcile persisted choices without exposing Docker to the Web API."""

    def __init__(self, orchestrator: OrchestratorClient | None = None):
        self._receivers: dict[str, Receiver] = {}
        self._orchestrator = orchestrator or OrchestratorClient()
        self._dynamic_orchestrator = orchestrator is None
        self._orchestration_status = "unconfigured"
        self._orchestration_detail = ""

    @property
    def receivers(self) -> list[Receiver]:
        return list(self._receivers.values())

    @property
    def orchestration_status(self) -> dict[str, str | bool]:
        return {
            "configured": self._orchestrator.configured,
            "status": self._orchestration_status,
            "detail": self._orchestration_detail,
        }

    async def start(self) -> None:
        await self.stop()
        if self._dynamic_orchestrator:
            self._orchestrator = OrchestratorClient()
        if self._orchestrator.configured:
            await self._start_orchestrated()
        elif settings.receiver_mode == "single":
            self._start_legacy_single()
        else:
            self._start_unavailable_multi()

    async def stop(self) -> None:
        for receiver in list(self._receivers.values()):
            try:
                await asyncio.wait_for(receiver.pcm_source.stop(), timeout=5.0)
            except TimeoutError:
                logger.error("PCM source %s did not stop within 5s", receiver.device_id)
            except Exception:
                logger.exception("Error stopping PCM source for %s", receiver.device_id)
        self._receivers.clear()

    def _desired(self) -> list[DesiredReceiver]:
        return [
            DesiredReceiver(
                key=receiver.id,
                device_id=receiver.id,
                name=receiver.name,
                protocol="airplay2",
            )
            for receiver in settings.active_receivers()
        ]

    async def _start_orchestrated(self) -> None:
        self._orchestration_status = "reconciling"
        self._orchestration_detail = "正在同步 Receiver 容器"
        try:
            actual = await self._orchestrator.reconcile(self._desired())
        except Exception as exc:
            logger.exception("Receiver orchestration failed")
            self._orchestration_status = "error"
            self._orchestration_detail = str(exc)
            self._start_error_placeholders(str(exc))
            return

        for item in actual:
            source = (
                create_pcm_source(f"tcp:{item.pcm_host}:{item.pcm_port}")
                if item.status == "running" and item.pcm_host
                else create_pcm_source("mock")
            )
            source.epoch = item.epoch
            self._receivers[item.device_id] = Receiver(
                device_id=item.device_id,
                name=item.name,
                pcm_source=source,
                status=item.status,
                detail=item.error,
            )

        failures = [item for item in actual if item.status != "running"]
        self._orchestration_status = "degraded" if failures else "running"
        self._orchestration_detail = (
            f"{len(failures)} 个接收器启动失败" if failures else f"{len(actual)} 个接收器已同步"
        )

    async def reset_receiver(self, device_id: str) -> None:
        """Release/recreate one ingress, without restarting unrelated receivers."""
        receiver = self._receivers.get(device_id)
        if receiver is None:
            return
        await receiver.pcm_source.stop()
        if self._orchestrator.configured:
            await self._orchestrator.disconnect_receiver(device_id)
            results = await self._orchestrator.reconcile(self._desired())
            result = next(item for item in results if item.device_id == device_id)
            if result.status != "running":
                raise RuntimeError(result.error or result.status)
            source = create_pcm_source(f"tcp:{result.pcm_host}:{result.pcm_port}")
            source.epoch = result.epoch
        else:
            source = create_pcm_source(settings.pcm_source, env={"MICAST_DEVICE_ID": device_id})
        receiver.pcm_source = source

    def _start_legacy_single(self) -> None:
        definition = next(iter(settings.active_receivers()), None)
        device_id = definition.id if definition else "main"
        name = definition.name if definition else settings.app.name
        self._receivers[device_id] = Receiver(
            device_id=device_id,
            name=name,
            pcm_source=create_pcm_source(settings.pcm_source, env={"MICAST_DEVICE_ID": device_id}),
            status="running",
            detail="使用外部 PCM 来源；协议由外部接收器决定",
        )
        self._orchestration_status = "legacy"
        self._orchestration_detail = "当前使用兼容开发模式"

    def _start_unavailable_multi(self) -> None:
        message = "实验性多音箱需要 Docker Receiver 编排服务"
        self._orchestration_status = "unconfigured"
        self._orchestration_detail = message
        self._start_error_placeholders(message)

    def _start_error_placeholders(self, message: str) -> None:
        for desired in self._desired():
            self._receivers[desired.device_id] = Receiver(
                device_id=desired.device_id,
                name=desired.name,
                pcm_source=create_pcm_source("mock"),
                status="error",
                detail=message,
            )
