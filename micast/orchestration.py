"""Client and value objects for the restricted Receiver orchestrator."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote, urljoin, urlparse

import httpx

from micast.config import settings


@dataclass(frozen=True)
class DesiredReceiver:
    """The deliberately small receiver configuration accepted by the orchestrator."""

    key: str
    device_id: str
    name: str
    protocol: str

    def as_payload(self) -> dict[str, str]:
        return {
            "key": self.key,
            "device_id": self.device_id,
            "name": self.name,
            "protocol": self.protocol,
        }


@dataclass(frozen=True)
class OrchestratedReceiver:
    key: str
    device_id: str
    name: str
    status: str
    pcm_host: str
    pcm_port: int
    error: str = ""
    epoch: str = ""


class OrchestratorClient:
    """HTTP client that can only reconcile MiCast Receiver resources."""

    def __init__(self, base_url: str | None = None, token: str | None = None):
        configured_url = base_url if base_url is not None else settings.orchestrator_url
        self.base_url = configured_url.rstrip("/")
        self.token = token if token is not None else settings.orchestrator_token

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.token)

    async def health(self) -> dict[str, str]:
        """Verify that the configured endpoint is a reachable Receiver service."""
        if not self.base_url:
            raise RuntimeError("Receiver 服务地址未配置")
        async with httpx.AsyncClient(timeout=5.0, follow_redirects=False) as client:
            response = await client.get(f"{self.base_url}/health")
            response.raise_for_status()
            data = response.json()
        if data.get("status") != "ok":
            raise RuntimeError("Receiver 服务返回了异常状态")
        return {"status": "ok"}

    async def capabilities(self) -> dict[str, object]:
        """Read optional service capabilities; old services remain single-entry compatible."""
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        async with httpx.AsyncClient(timeout=5.0, follow_redirects=False) as client:
            response = await client.get(f"{self.base_url}/v1/capabilities", headers=headers)
        if response.status_code == 404:
            return {
                "api_version": "unknown",
                "protocols": [],
                "instance_mode": "unknown",
                "max_instances": 1,
                "features": {},
                "verified": False,
                "status": "unreported",
                "reason": "capability_endpoint_missing",
            }
        response.raise_for_status()
        data = response.json()
        data["verified"] = True
        return data

    async def reconcile(self, desired: list[DesiredReceiver]) -> list[OrchestratedReceiver]:
        if not self.configured:
            raise RuntimeError("Receiver 编排服务未配置")

        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        payload = {"receivers": [item.as_payload() for item in desired]}
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
            response = await client.post(
                f"{self.base_url}/v1/receivers/reconcile", json=payload, headers=headers
            )
            if response.is_redirect:
                location = response.headers.get("location", "")
                source_host = urlparse(self.base_url).hostname
                target = urljoin(str(response.request.url), location)
                if urlparse(target).hostname != source_host:
                    raise RuntimeError("Receiver 服务重定向到了其他主机，已拒绝发送连接令牌")
                response = await client.post(target, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()

        return [
            OrchestratedReceiver(
                key=item["key"],
                device_id=item["device_id"],
                name=item["name"],
                status=item.get("status", "error"),
                pcm_host=item.get("pcm_host", ""),
                pcm_port=int(item.get("pcm_port", 42800)),
                error=item.get("error", ""),
                epoch=item.get("epoch", ""),
            )
            for item in data.get("receivers", [])
        ]

    async def disconnect_receiver(self, key: str) -> None:
        """Reset only this managed receiver's ingress, leaving it advertised."""
        async with httpx.AsyncClient(timeout=12.0, follow_redirects=False) as client:
            result = await client.post(
                f"{self.base_url}/v1/receivers/{quote(key, safe='')}/disconnect",
                headers={"Authorization": f"Bearer {self.token}"},
            )
            result.raise_for_status()
