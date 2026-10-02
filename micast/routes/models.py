"""Lightweight response models for the core read endpoints.

Fields mirror the dicts the routes already return; ``extra="allow"`` keeps
the contract forward-compatible when a route gains a key before the model
does. Nested config payloads (receivers, groups, ports, storage) stay
schemaless on purpose — their shape is defined by the config models and
would be tedious and brittle to duplicate here.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict


class AudioConfigResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    format: str
    bitrate: str
    sample_rate: int
    auto_transcode: bool


class ConfigResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    config_revision: int = 0
    runtime_epoch: str | None = None

    deployment: str
    audio: dict[str, Any]
    app: dict[str, Any]
    receiver_mode: str
    airplay_protocol: str
    airplay_engine: str
    dlna_enabled: bool
    sync_groups_enabled: bool
    large_delay_enabled: bool
    touchscreen_lyrics: bool
    default_volume: int
    default_volume_enabled: bool
    sender_volume_mode: str
    notify_webhook_url: str
    airplay2_enabled: bool
    network_discovery_enabled: bool
    airplay2_available: bool
    airplay2_mode: str
    airplay2_can_add_instances: bool
    storage: dict[str, Any]
    dlna_status: dict[str, Any]
    selected_device_id: str | None
    ports: list[dict[str, Any]]
    receivers: list[dict[str, Any]]
    groups: list[dict[str, Any]]
    speaker_names: dict[str, str]


class TuningStateResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    did: str
    enabled: bool
    # int | float preserves the wire shape: the editor posts ints and the
    # endpoints echo them without a float coercion pass.
    points: list[list[int | float]]
    preset: str
    target: str
    night_mode: bool
    loudness_comp_enabled: bool
    content_profile: str
    revision: int
    undo_available: bool
    profiles: dict[str, list[list[int | float]]]
    freq_range: list[int | float]
    gain_range: list[int | float]
