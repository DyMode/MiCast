"""Restricted Docker control plane for dynamically managed MiCast receivers."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import threading
import uuid
from typing import Literal

from docker.errors import DockerException, NotFound
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field, field_validator

import docker

MANAGED_LABEL = "com.micast.receiver.managed"
SPEC_LABEL = "com.micast.receiver.spec"
KEY_LABEL = "com.micast.receiver.key"
_lock = threading.Lock()


class ReceiverSpec(BaseModel):
    key: str = Field(min_length=1, max_length=160)
    device_id: str = Field(min_length=1, max_length=160)
    name: str = Field(min_length=1, max_length=50)
    protocol: Literal["auto", "classic", "airplay2"] = "auto"

    @field_validator("key", "device_id")
    @classmethod
    def validate_identifier(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9._:-]+", value):
            raise ValueError("identifier contains unsupported characters")
        return value

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        value = value.strip()
        if not value or any(char in value for char in ('"', "\\", "\r", "\n")):
            raise ValueError("name contains unsupported characters")
        return value


class ReconcileRequest(BaseModel):
    receivers: list[ReceiverSpec] = Field(max_length=32)


class ReceiverResult(BaseModel):
    key: str
    device_id: str
    name: str
    status: str
    pcm_host: str = ""
    pcm_port: int = 42800
    error: str = ""
    epoch: str = ""


app = FastAPI(title="MiCast Receiver Orchestrator", docs_url=None, redoc_url=None)


def require_token(authorization: str | None = Header(default=None)) -> None:
    expected = os.environ.get("MICAST_ORCHESTRATOR_TOKEN", "")
    if not expected:
        raise HTTPException(status_code=503, detail="MICAST_ORCHESTRATOR_TOKEN is required")
    if authorization != f"Bearer {expected}":
        raise HTTPException(status_code=401, detail="Invalid orchestrator token")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/v1/capabilities", dependencies=[Depends(require_token)])
async def capabilities() -> dict[str, object]:
    return {
        "api_version": "1",
        "protocols": ["airplay2"],
        "instance_mode": "dynamic",
        "max_instances": 32,
        "features": {
            "create_instance": True,
            "start_stop": True,
            "delete_instance": True,
            "rename_instance": True,
            "pcm_output": True,
            "mdns_publish": True,
            "disconnect_sender": True,
        },
    }


@app.post(
    "/v1/receivers/reconcile",
    response_model=dict[str, list[ReceiverResult]],
    dependencies=[Depends(require_token)],
)
async def reconcile(request: ReconcileRequest) -> dict[str, list[ReceiverResult]]:
    return {"receivers": await asyncio.to_thread(_reconcile_sync, request.receivers)}


@app.post("/v1/receivers/{key}/disconnect", dependencies=[Depends(require_token)])
async def disconnect_receiver(key: str) -> dict:
    return await asyncio.to_thread(_disconnect_receiver_sync, key)


def _disconnect_receiver_sync(key: str) -> dict:
    # The managed/key label pair is the only authority; never accept arbitrary
    # container IDs or invoke an unrestricted Docker action from this endpoint.
    with _lock:
        client = docker.from_env()
        try:
            containers = client.containers.list(
                all=True, filters={"label": f"{MANAGED_LABEL}=true"}
            )
            desired = []
            target = None
            for container in containers:
                container.reload()
                env = dict(item.split("=", 1) for item in container.attrs["Config"]["Env"])
                desired.append(ReceiverSpec(
                    key=container.labels[KEY_LABEL], device_id=env["MICAST_DEVICE_ID"],
                    name=env["MICAST_AIRPLAY_NAME"], protocol=env["MICAST_AIRPLAY_PROTOCOL"],
                ))
                if container.labels[KEY_LABEL] == key:
                    target = container
            if target is None:
                raise HTTPException(status_code=404, detail="Managed receiver not found")
            target.remove(force=True)
            results = _reconcile_locked(client, desired)
            result = next(item for item in results if item.key == key)
            if result.status != "running":
                raise HTTPException(status_code=503, detail=result.error or result.status)
            return {"ok": True, "epoch": result.epoch}
        finally:
            client.close()


def _reconcile_sync(desired: list[ReceiverSpec]) -> list[ReceiverResult]:
    with _lock:
        client = docker.from_env()
        try:
            return _reconcile_locked(client, desired)
        finally:
            client.close()


def _reconcile_locked(client, desired: list[ReceiverSpec]) -> list[ReceiverResult]:
    image = os.environ.get("MICAST_RECEIVER_IMAGE", "micast-receiver:latest")
    lan_network_name = os.environ.get("MICAST_RECEIVER_LAN_NETWORK", "micast-airplay")
    internal_network_name = os.environ.get("MICAST_INTERNAL_NETWORK", "micast-internal")
    callback_base = os.environ.get("MICAST_CALLBACK_BASE", "http://micast:42300")
    callback_token = os.environ.get("MICAST_ORCHESTRATOR_TOKEN", "")
    pcm_host_mode = os.environ.get("MICAST_PCM_HOST_MODE", "internal").strip().lower()

    try:
        lan_network = client.networks.get(lan_network_name)
        internal_network = client.networks.get(internal_network_name)
    except NotFound as exc:
        raise HTTPException(
            status_code=503, detail=f"Required Docker network is missing: {exc}"
        ) from exc
    except DockerException as exc:
        raise HTTPException(status_code=503, detail=f"Docker is unavailable: {exc}") from exc

    existing = {
        container.labels.get(KEY_LABEL, ""): container
        for container in client.containers.list(
            all=True, filters={"label": f"{MANAGED_LABEL}=true"}
        )
    }
    desired_keys = {spec.key for spec in desired}
    for key, container in existing.items():
        if key not in desired_keys:
            container.remove(force=True)

    results: list[ReceiverResult] = []
    for spec in desired:
        container = existing.get(spec.key)
        spec_hash = _spec_hash(spec)
        try:
            if container and container.labels.get(SPEC_LABEL) != spec_hash:
                container.remove(force=True)
                container = None
            if container is None:
                name = _container_name(spec.key)
                container = client.containers.create(
                    image=image,
                    name=name,
                    hostname=name,
                    network=lan_network.name,
                    environment={
                        "MICAST_DEVICE_ID": spec.device_id,
                        "MICAST_AIRPLAY_NAME": spec.name,
                        "MICAST_AIRPLAY_PROTOCOL": spec.protocol,
                        "MICAST_CALLBACK_BASE": callback_base,
                        "MICAST_CALLBACK_TOKEN": callback_token,
                        "MICAST_PCM_PORT": "42800",
                        "MICAST_RECEIVER_EPOCH": uuid.uuid4().hex,
                    },
                    labels={MANAGED_LABEL: "true", KEY_LABEL: spec.key, SPEC_LABEL: spec_hash},
                    restart_policy={"Name": "unless-stopped"},
                    cap_add=["SYS_NICE", "NET_BIND_SERVICE"],
                )
                internal_network.connect(container, aliases=[name])
            container.reload()
            if container.status != "running":
                container.start()
                container.reload()
            results.append(
                ReceiverResult(
                    key=spec.key,
                    device_id=spec.device_id,
                    name=spec.name,
                    status="running" if container.status == "running" else container.status,
                    pcm_host=(
                        container.attrs.get("NetworkSettings", {})
                        .get("Networks", {})
                        .get(lan_network_name, {})
                        .get("IPAddress", "")
                        if pcm_host_mode == "lan"
                        else container.name
                    ),
                    epoch=next((item.split("=", 1)[1]
                                for item in container.attrs.get("Config", {}).get("Env", [])
                                if item.startswith("MICAST_RECEIVER_EPOCH=")), ""),
                )
            )
        except DockerException as exc:
            results.append(
                ReceiverResult(
                    key=spec.key,
                    device_id=spec.device_id,
                    name=spec.name,
                    status="error",
                    error=str(exc),
                )
            )
    return results


def _spec_hash(spec: ReceiverSpec) -> str:
    # Updating the receiver callback contract must replace existing instances,
    # not leave containers running an old image behind an unchanged spec.
    payload = json.dumps(
        {**spec.model_dump(), "receiver_contract": "session-leases-v2"},
        ensure_ascii=False,
        sort_keys=True,
    ).encode()
    return hashlib.sha256(payload).hexdigest()[:16]


def _container_name(key: str) -> str:
    readable = re.sub(r"[^a-z0-9]+", "-", key.lower()).strip("-")[:28] or "receiver"
    suffix = hashlib.sha256(key.encode()).hexdigest()[:8]
    return f"micast-receiver-{readable}-{suffix}"
