"""Application configuration with layered loading and hot reload."""

import json
import os
import re
import shutil
import socket
import sys
import tempfile
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from pydantic import BaseModel, Field, PrivateAttr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from micast.curve_fit import (
    CURVE_FREQ_RANGE,
    CURVE_GAIN_RANGE,
    NIGHT_ATTENUATION,
    TARGET_CURVES,
    add_curve,
    curve_signature,
    legacy_bands_to_points,
    normalize_points,
)

# EQ gain clamp kept under its historical name.
EQ_GAIN_RANGE = CURVE_GAIN_RANGE

# Named per-speaker scenes a user can save their current curve into. Switching
# a scene copies its saved curve into the active EQ; drawing a custom curve
# clears the active-scene label (content_profile back to "").
CONTENT_PROFILES: tuple[str, ...] = ("music", "movie", "voice")

# Snapshot the real environment before .env loading: a port set in .env is a
# config default, not a deliberate pin — only true env vars make a busy port
# fatal instead of sliding to a free one.
_ENV_PINNED: frozenset[str] = frozenset(os.environ)

# Make .env values visible to os.environ so file-persistence checks below can
# respect environment overrides correctly.
load_dotenv(".env", override=False)


def _default_route_ip() -> str:
    """Return the IP address of the default outgoing interface."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            # Connecting a UDP socket lets the OS pick the right interface.
            s.connect(("192.168.0.12", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


def port_in_use(port: int, host: str = "0.0.0.0") -> bool:
    """True if something already listens on the port.

    Connect first (catches listeners bound to a specific interface), then bind
    without SO_REUSEADDR — on Windows that flag would let us "successfully"
    hijack a port another process is actively listening on.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.3)
        if probe.connect_ex(("127.0.0.1", port)) == 0:
            return True
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind((host, port))
        return False
    except OSError:
        return True


def resolve_port(preferred: int, env_var: str, attempts: int = 32) -> int:
    """Pick a listen port: the preferred one, or the next free one.

    Users can't be expected to keep 3000/8080 free, so a busy preferred port
    silently slides to the next available one — unless the port was pinned
    via env var, which is a deliberate choice worth failing loudly about.
    """
    if not port_in_use(preferred):
        return preferred
    if env_var in _ENV_PINNED:
        raise RuntimeError(f"端口 {preferred} 已被占用（{env_var} 显式指定，不会自动更换）")
    for candidate in range(preferred + 1, preferred + 1 + attempts):
        if not port_in_use(candidate):
            return candidate
    raise RuntimeError(f"端口 {preferred}-{preferred + attempts} 全部被占用")


def env_pinned(env_var: str) -> bool:
    """True when a real environment variable (not .env) pins this setting."""
    return env_var in _ENV_PINNED


# UI-editable ports: field name -> (env var, default preferred value).
# A None default means "unset -> the service's built-in default applies".
EDITABLE_PORTS: dict[str, tuple[str, int | None]] = {
    "port": ("MICAST_PORT", 3000),
    "stream_port": ("MICAST_STREAM_PORT", 8080),
    "airplay_rtsp_port": ("MICAST_AIRPLAY_RTSP_PORT", None),
    "airplay_udp_base": ("MICAST_AIRPLAY_UDP_BASE", None),
    "airplay2_port": ("MICAST_AIRPLAY2_PORT", None),
}


def storage_mode() -> str:
    """Current persistence profile exposed to diagnostics and the UI."""
    if os.environ.get("MICAST_DATA_DIR", "").strip():
        return "managed"
    if getattr(sys, "frozen", False):
        executable_dir = Path(sys.executable).resolve().parent
        if (
            os.environ.get("MICAST_PORTABLE", "").strip() == "1"
            or (executable_dir / "portable.flag").exists()
        ):
            return "portable"
        return "installed"
    return "development"


def _migrate_source_data(target: Path) -> None:
    """Copy the old repo-local profile once; never remove the source copy."""
    legacy = Path(__file__).resolve().parent.parent / "config"
    if target.exists() or not legacy.is_dir():
        return
    files = ("micast.json", "access.json", "xiaomi-account.json", "xiaomi-tokens.enc")
    if not any((legacy / name).is_file() for name in files):
        return
    target.mkdir(parents=True, exist_ok=True)
    for name in files:
        source = legacy / name
        if source.is_file():
            shutil.copy2(source, target / name)


def default_data_dir() -> Path:
    """Where micast.json and the encrypted Xiaomi tokens live.

    Priority:
    1. MICAST_DATA_DIR env (Docker mounts, portable installs)
    2. A portable build's adjacent data/ directory
    3. Per-user directories, isolated between development and installed builds
       %APPDATA%/MiCast, ~/Library/Application Support/MiCast,
       $XDG_DATA_HOME/micast.
    """
    override = os.environ.get("MICAST_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    mode = storage_mode()
    if mode == "portable":
        return Path(sys.executable).resolve().parent / "data"
    if sys.platform == "win32":
        base = os.environ.get("APPDATA")
        name = "MiCast-Dev" if mode == "development" else "MiCast"
        target = Path(base) / name if base else Path.home() / "AppData" / "Roaming" / name
    elif sys.platform == "darwin":
        target = Path.home() / "Library" / "Application Support" / "MiCast"
    else:
        xdg = os.environ.get("XDG_DATA_HOME", "").strip()
        target = (Path(xdg) if xdg else Path.home() / ".local" / "share") / "micast"
    if mode == "development":
        _migrate_source_data(target)
    return target


def default_log_dir() -> Path:
    """Logs are removable local state for installed builds."""
    mode = storage_mode()
    if mode == "managed":
        return default_data_dir()
    if mode == "installed" and sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / "MiCast" / "logs"
    return default_data_dir() / "logs"


def default_runtime_dir() -> Path:
    """Locks and other disposable process state."""
    mode = storage_mode()
    if mode == "managed":
        return default_data_dir()
    if mode == "installed" and sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / "MiCast" / "runtime"
    return default_data_dir() / "runtime"


class AudioConfig(BaseSettings):
    """Audio encoding configuration."""

    model_config = SettingsConfigDict(env_prefix="MI_AUDIO_", extra="ignore")

    format: str = Field(default="mp3", pattern=r"^(mp3|flac|wav)$")
    bitrate: str = Field(default="320k", pattern=r"^(128k|192k|320k)$")
    sample_rate: int = Field(default=48000, ge=44100, le=48000)
    auto_transcode: bool = Field(default=True)

    @field_validator("format", "bitrate", "sample_rate", mode="before")
    @classmethod
    def _blank_to_default(cls, v, info):
        if v is None or v == "":
            return cls.model_fields[info.field_name].default
        return v


# Per-speaker equalizer: a user-drawn response curve persisted as sparse
# control points (freq Hz, gain dB). A speaker with a flat curve (or EQ
# disabled) shares the receiver's base stream; distinct non-flat signatures
# each get their own split stream (…-q1, …-q2).
#
# EQ_BANDS_HZ / EQ_PRESETS are the legacy 10-band layout, kept only to
# migrate old configs into control points.
EQ_BANDS_HZ: tuple[int, ...] = (31, 62, 125, 250, 500, 1000, 2000, 4000, 8000, 16000)
EQ_BAND_COUNT = len(EQ_BANDS_HZ)

# Built-in presets, key -> band gains in dB (31/62/125/250/500/1k/2k/4k/8k/16k).
EQ_PRESETS: dict[str, list[float]] = {
    "flat": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
    "bass": [4.0, 5.0, 4.0, 2.0, 1.0, 0.0, 0.0, -1.0, 0.0, 0.0],
    "vocal": [-2.0, -1.0, 0.0, 0.0, 1.0, 3.0, 2.0, 2.0, 1.0, 0.0],
    "night": [-4.0, -4.0, -3.0, -2.0, -1.0, 0.0, 0.0, -1.0, -2.0, -3.0],
    "live": [2.0, 1.0, 0.0, 0.0, 0.0, 1.0, 1.0, 2.0, 3.0, 3.0],
}

# Presets expressed as control points (what new configs and the curve editor
# actually consume).
EQ_PRESET_POINTS: dict[str, list[tuple[float, float]]] = {
    key: [(float(hz), g) for hz, g in zip(EQ_BANDS_HZ, bands, strict=True)]
    for key, bands in EQ_PRESETS.items()
}
# The Harman target doubles as a preset so it is one tap away, not only a
# reference overlay. It is a real curve, not a 10-band migration artifact.
EQ_PRESET_POINTS["harman"] = list(TARGET_CURVES["harman"])


class EqPoint(BaseModel):
    """One EQ curve control point."""

    freq: float = Field(ge=CURVE_FREQ_RANGE[0], le=CURVE_FREQ_RANGE[1])
    gain_db: float = Field(ge=CURVE_GAIN_RANGE[0], le=CURVE_GAIN_RANGE[1])


class SpeakerEqUndo(BaseModel):
    """One server-side undo checkpoint for all audible per-speaker tuning."""

    enabled: bool = False
    points: list[EqPoint] = Field(default_factory=list)
    preset: str = ""
    target: str = ""
    night_mode: bool = False
    loudness_comp_enabled: bool = False
    content_profile: str = ""


class AppConfig(BaseModel):
    """Application-level settings."""

    name: str = Field(default="MiCast", min_length=1, max_length=64)


class SpeakerConfig(BaseModel):
    """Persisted configuration for a single Xiaomi speaker."""

    did: str = Field(min_length=1)
    alias: str = ""
    enabled: bool = False
    # Xiaomi's deviceID may change after an account/device rebind.  miotDID is
    # persisted separately so discovery can re-associate the same physical
    # speaker and atomically rewrite every reference to its current deviceID.
    miot_did: str = ""
    hardware: str = ""
    # Per-speaker EQ: a drawn response curve as control points. EQ is a
    # property of the physical speaker (its room/placement), so it lives here
    # and follows the speaker into any group.
    eq_enabled: bool = False
    eq_points: list[EqPoint] = Field(default_factory=list)
    eq_preset: str = ""
    # Named target response the calibration wizard aims for ("" = flat).
    eq_target: str = ""
    # Night mode: a fixed bass-attenuation shelf layered onto the active curve.
    # Independent of eq_enabled so it also works on a flat curve.
    night_mode: bool = False
    # Equal-loudness compensation: a low/high shelf that follows the listening
    # volume (see curve_fit.loudness_curve). It is a stream-splitting dimension
    # (its level is runtime, not persisted — only the on/off flag is).
    loudness_comp_enabled: bool = False
    # Active scene ("" = custom/manual). Saved per-speaker curves live in
    # eq_profiles; switching a scene copies it into eq_points.
    content_profile: str = ""
    eq_profiles: dict[str, list[EqPoint]] = Field(default_factory=dict)
    # Monotonic persisted revision used for cross-client optimistic locking.
    eq_revision: int = Field(default=0, ge=0)
    # A single durable checkpoint (one slot, overwritten by every new
    # checkpoint): undo still works after leaving the page or opening it on
    # another device, but only one tuning step can be undone.
    eq_undo: SpeakerEqUndo | None = None

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_eq_bands(cls, data):
        """Old configs persist 10-band (or legacy 5-band) slider gains; fold
        them into control points on first load."""
        if not isinstance(data, dict) or "eq_bands" not in data:
            return data
        bands = data.pop("eq_bands")
        if data.get("eq_points"):
            return data
        if isinstance(bands, list):
            # 5-band configs (60/250/1k/4k/12k) land on their nearest ISO band
            # of the 10-band layout, not on the first five positions.
            if len(bands) == 5:
                bands = [0.0, bands[0], 0.0, bands[1], 0.0, bands[2], 0.0, bands[3], 0.0, bands[4]]
            lo, hi = EQ_GAIN_RANGE
            gains = [max(lo, min(hi, float(b))) for b in bands[:EQ_BAND_COUNT]]
            gains += [0.0] * (EQ_BAND_COUNT - len(gains))
            data["eq_points"] = [
                {"freq": float(hz), "gain_db": g} for hz, g in zip(EQ_BANDS_HZ, gains, strict=True)
            ]
        return data


class ReceiverConfig(BaseModel):
    """An AirPlay name and the playback destination bound to it."""

    id: str = Field(min_length=1)
    name: str = Field(min_length=1, max_length=64)
    target_type: str = Field(default="selected", pattern=r"^(selected|speaker|group)$")
    target_id: str | None = None
    enabled: bool = True


class SpeakerGroupConfig(BaseModel):
    """A playback destination containing multiple physical speakers."""

    id: str = Field(min_length=1)
    name: str = Field(min_length=1, max_length=64)
    speaker_ids: list[str] = Field(default_factory=list)
    # Signed offset (ms) per member, relative to `anchor_did`. Positive = later
    # (delayed), negative = earlier (ahead). The anchor itself is always 0.
    delays_ms: dict[str, int] = Field(default_factory=dict)
    # Reference member every other member's offset is measured against.
    anchor_did: str | None = None
    # "mirror": every speaker plays the same stream. "stereo": exactly two
    # speakers, each plays one channel of the source through its own stream.
    mode: str = Field(default="mirror", pattern=r"^(mirror|stereo)$")
    channels: dict[str, str] = Field(default_factory=dict)  # did -> "left" | "right"
    gains_db: dict[str, float] = Field(default_factory=dict)  # loudness trim per speaker
    # External AirPlay devices (discovered via mDNS) that play alongside the
    # Xiaomi speakers; ids are MAC hex from the _raop service name.
    airplay_targets: list[str] = Field(default_factory=list)
    # External DLNA renderers (discovered via SSDP); ids are device UDNs.
    dlna_targets: list[str] = Field(default_factory=list)
    # Channel assignment per network device id (AirPlay id or DLNA UDN) in a
    # stereo group; no entry means the full stereo mix.
    network_channels: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _migrate_group_delays(cls, data: Any) -> Any:
        """One-step migration: fold the old absolute-lag delay fields
        (`delays_ms`, `audio_delays_ms`, `airplay_delays_ms`) into signed,
        anchor-relative `delays_ms`. Runs on the raw dict before Pydantic drops
        the removed fields, so a legacy JSON and a fresh group both normalize.

        Discriminator: a legacy group has no `anchor_did`; a migrated one does.
        """
        if not isinstance(data, dict):
            return data
        members = (
            list(data.get("speaker_ids") or [])
            + list(data.get("airplay_targets") or [])
            + list(data.get("dlna_targets") or [])
        )
        out = dict(data)
        out.pop("audio_delays_ms", None)
        out.pop("airplay_delays_ms", None)

        anchor = data.get("anchor_did")
        if anchor is None:
            # Legacy (or fresh): fold the three absolute-lag dicts, whose key
            # spaces are disjoint, then anchor on the least-delayed member.
            abs_lag: dict[str, int] = dict.fromkeys(members, 0)
            for src in (
                data.get("delays_ms") or {},
                data.get("audio_delays_ms") or {},
                data.get("airplay_delays_ms") or {},
            ):
                for key, value in src.items():
                    try:
                        abs_lag[str(key)] = int(value)
                    except (TypeError, ValueError):
                        continue
            anchor = min(members, key=lambda m: abs_lag.get(m, 0)) if members else None
            base = abs_lag.get(anchor, 0) if anchor else 0
            out["delays_ms"] = {m: abs_lag[m] - base for m in members if abs_lag[m] != base}
            out["anchor_did"] = anchor
            return out

        # Already migrated: keep signed offsets, force the anchor offset to 0,
        # and re-anchor (to the earliest member) if the anchor left the group.
        delays = {k: int(v) for k, v in (data.get("delays_ms") or {}).items()}
        if anchor not in members:
            anchor = members[0] if members else None
        delays.pop(anchor, None)
        out["delays_ms"] = delays
        out["anchor_did"] = anchor
        return out

    @property
    def member_count(self) -> int:
        """Total members: Xiaomi speakers + attached network devices."""
        return len(self.speaker_ids) + len(self.airplay_targets) + len(self.dlna_targets)

    def delay_holds(self) -> dict[str, int]:
        """Non-negative hold (ms) per delay-capable member — Xiaomi speakers and
        external AirPlay targets — normalized so the most-ahead member holds 0
        and the rest pad after it. This is the ONE normalization shared by the
        pull path (stream server per-client buffer) and the push path (AirPlay
        pre-buffer), so a mixed group aligns to a single live edge. DLNA
        renderers have no delay path and are excluded (they play live)."""
        members = [*self.speaker_ids, *self.airplay_targets]
        offsets = {m: int(self.delays_ms.get(m, 0)) for m in members}
        if not offsets:
            return {}
        min_off = min(offsets.values())
        return {m: max(0, offsets[m] - min_off) for m in members}


def _sanitize_airplay_targets(items) -> list[str]:
    return list(
        dict.fromkeys(
            str(item).lower() for item in items if re.fullmatch(r"[0-9a-f]{12}", str(item).lower())
        )
    )


def _sanitize_dlna_targets(items) -> list[str]:
    return list(dict.fromkeys(str(item).strip() for item in items if str(item).strip()))


class AirPlay2InstanceConfig(BaseModel):
    """An AirPlay 2 receiver identity mapped to one MiCast playback target."""

    id: str = Field(min_length=1)
    name: str = Field(min_length=1, max_length=50)
    target_type: str = Field(pattern=r"^(speaker|group)$")
    target_id: str = Field(min_length=1)
    enabled: bool = True


class Settings(BaseSettings):
    """Global application settings.

    Priority (highest to lowest):
    1. Environment variables
    2. Runtime memory (set via web UI)
    3. config/micast.json
    4. Code defaults
    """

    model_config = SettingsConfigDict(
        env_prefix="MICAST_",
        env_nested_delimiter="__",
        extra="ignore",
        env_parse_none_str="",
        env_file=".env",
        env_file_encoding="utf-8",
    )

    host: str = "0.0.0.0"
    port: int = 3000
    stream_host: str = ""
    stream_port: int = 8080
    # Preferred ports for the AirPlay services; None = use the built-in
    # default. A busy preferred port slides upward (see resolve_port and
    # raop.server), so these are starting points, not strict pins.
    airplay_rtsp_port: int | None = None
    airplay_udp_base: int | None = None
    airplay2_port: int | None = None
    pcm_source: str = "mock"
    airplay2_pcm_source: str = "mock"
    pcm_sample_rate: int = Field(default=48000, ge=8000, le=384000)
    stream_buffer_seconds: float = Field(default=0.25, ge=0.05, le=3.0)
    shairport_path: str = "shairport-sync"
    orchestrator_url: str = ""
    orchestrator_token: str = ""
    encryption_key: str | None = None

    audio: AudioConfig = Field(default_factory=AudioConfig)
    app: AppConfig = Field(default_factory=AppConfig)
    receiver_mode: str = Field(default="single", pattern=r"^(single|multi)$")
    airplay_protocol: str = Field(default="auto", pattern=r"^(auto|classic|airplay2)$")
    airplay_engine: str = Field(default="local", pattern=r"^(local|airplay2)$")
    dlna_enabled: bool = True
    sync_groups_enabled: bool = True
    large_delay_enabled: bool = False
    airplay2_enabled: bool = False
    # Experimental LAN discovery of external playback targets: AirPlay mDNS
    # browse + DLNA SSDP M-SEARCH. Off by default so an idle MiCast never
    # scans the network.
    network_discovery_enabled: bool = False
    # Touch-screen speakers show real cover art + scrolling lyrics: DAAP track
    # metadata from the phone is matched against Xiaomi's music library and
    # the play command is re-issued with the song's audioID.
    touchscreen_lyrics: bool = True
    # Opt-in session-start device volume. Zero is mute, not an off sentinel.
    default_volume: int = Field(default=0, ge=0, le=100)
    default_volume_enabled: bool = False
    # How long a paused AirPlay session may hold stream clients without any
    # data before the sweeper expires it and stops the Xiaomi playback.
    # Seconds; 0 disables the expiry entirely (pause indefinitely).
    stale_session_timeout: int = Field(default=60, ge=0)
    # How far a speaker's pulled stream may lead real time before the delay
    # line trims it back to live. Doubles as the tolerance for speaker clock
    # drift: larger absorbs more drift, smaller keeps latency tight. Trims are
    # applied in small slices (see LAG_SKIP_SLICE_SECONDS).
    client_max_lag_seconds: float = Field(default=4.0, ge=0.5, le=30.0)
    sender_volume_mode: str = Field(default="independent", pattern=r"^(independent|linked)$")
    # Webhook (飞书自定义机器人 / WxPusher) notified when the Xiaomi login
    # expires; empty = disabled.
    notify_webhook_url: str = ""
    provider_account_id: str | None = None
    selected_device_id: str | None = None
    speakers: list[SpeakerConfig] = Field(default_factory=list)
    receivers: list[ReceiverConfig] = Field(default_factory=list)
    groups: list[SpeakerGroupConfig] = Field(default_factory=list)
    airplay2_instances: list[AirPlay2InstanceConfig] = Field(default_factory=list)
    # Global curve library: user-named EQ curves, appliable to any speaker.
    saved_curves: dict[str, list[EqPoint]] = Field(default_factory=dict)

    # Preferred values captured before resolve_port slides a busy port to a
    # free one — self.port/self.stream_port then hold the *actual* bound port
    # (many call sites build URLs from it), while persistence keeps the
    # preferred one so a one-off conflict doesn't permanently move the port.
    _preferred_port: int | None = PrivateAttr(default=None)
    _preferred_stream_port: int | None = PrivateAttr(default=None)
    _config_revision: int = PrivateAttr(default=0)

    @property
    def config_revision(self) -> int:
        """Process-local monotonic revision used to notify connected UIs."""
        return self._config_revision

    def apply_resolved_port(self, field: str, resolved: int) -> None:
        """Adopt the resolved port while remembering the preferred value."""
        if getattr(self, f"_preferred_{field}") is None:
            setattr(self, f"_preferred_{field}", getattr(self, field))
        setattr(self, field, resolved)

    def preferred_port(self, field: str) -> int:
        """The port we'd like to bind (UI-editable), not the one we got."""
        preferred = getattr(self, f"_preferred_{field}")
        return preferred if preferred is not None else getattr(self, field)

    @property
    def effective_stream_host(self) -> str:
        """Return a reachable host for the audio stream URL."""
        if self.stream_host:
            return self.stream_host
        if self.host and self.host != "0.0.0.0":
            return self.host
        return _default_route_ip()

    @property
    def config_path(self) -> Path:
        return default_data_dir() / "micast.json"

    def load_from_file(self) -> None:
        """Load non-env overrides from config file."""
        path = self.config_path
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return

        # Only load fields not explicitly set by environment
        for key, value in data.items():
            # Internal orchestration settings come only from the integrated
            # Docker deployment and are never loaded from the data file.
            if key in {"orchestrator_url", "orchestrator_token"}:
                continue
            if key == "audio" and isinstance(value, dict):
                for audio_key, audio_value in value.items():
                    env_var = f"MI_AUDIO_{audio_key.upper()}"
                    if env_var not in os.environ:
                        setattr(self.audio, audio_key, audio_value)
            elif key == "app" and isinstance(value, dict):
                if "MICAST_APP_NAME" not in os.environ:
                    self.app = AppConfig.model_validate(value)
            elif key == "speakers" and isinstance(value, list):
                if "MICAST_SPEAKERS" not in os.environ:
                    self.speakers = [SpeakerConfig.model_validate(item) for item in value]
            elif key == "receivers" and isinstance(value, list):
                self.receivers = [ReceiverConfig.model_validate(item) for item in value]
            elif key == "groups" and isinstance(value, list):
                self.groups = [SpeakerGroupConfig.model_validate(item) for item in value]
            elif key == "airplay2_instances" and isinstance(value, list):
                self.airplay2_instances = [
                    AirPlay2InstanceConfig.model_validate(item) for item in value
                ]
            elif key == "saved_curves" and isinstance(value, dict):
                self.saved_curves = {
                    str(name): [EqPoint.model_validate(p) for p in points]
                    for name, points in value.items()
                    if isinstance(points, list)
                }
            else:
                env_var = f"MICAST_{key.upper()}"
                if env_var not in os.environ and hasattr(self, key):
                    setattr(self, key, value)
        delay_limit_ms = 15000 if self.large_delay_enabled else 5000
        for group in self.groups:
            group.delays_ms = {
                key: max(-delay_limit_ms, min(delay_limit_ms, int(value)))
                for key, value in group.delays_ms.items()
            }
        self._migrate_receivers()

    def reset_runtime(self) -> None:
        """Restore defaults in-memory after the data files were wiped (清空数据).

        Re-reads environment-backed fields through a fresh instance so env
        overrides (Docker / fnOS) survive the reset, then adopts its state.
        """
        fresh = Settings()
        self.__dict__.update(fresh.__dict__)

    def save_to_file(self) -> None:
        """Persist current runtime settings to config file."""
        data = {
            "audio": self.audio.model_dump(),
            "app": self.app.model_dump(),
            "port": self.preferred_port("port"),
            "stream_port": self.preferred_port("stream_port"),
            "airplay_rtsp_port": self.airplay_rtsp_port,
            "airplay_udp_base": self.airplay_udp_base,
            "airplay2_port": self.airplay2_port,
            "receiver_mode": self.receiver_mode,
            "airplay_protocol": self.airplay_protocol,
            "airplay_engine": self.airplay_engine,
            "dlna_enabled": self.dlna_enabled,
            "sync_groups_enabled": self.sync_groups_enabled,
            "large_delay_enabled": self.large_delay_enabled,
            "airplay2_enabled": self.airplay2_enabled,
            "network_discovery_enabled": self.network_discovery_enabled,
            "touchscreen_lyrics": self.touchscreen_lyrics,
            "default_volume": self.default_volume,
            "default_volume_enabled": self.default_volume_enabled,
            "stale_session_timeout": self.stale_session_timeout,
            "client_max_lag_seconds": self.client_max_lag_seconds,
            "sender_volume_mode": self.sender_volume_mode,
            "notify_webhook_url": self.notify_webhook_url,
            "provider_account_id": self.provider_account_id,
            "selected_device_id": self.selected_device_id,
            "speakers": [speaker.model_dump() for speaker in self.speakers],
            "receivers": [receiver.model_dump() for receiver in self.receivers],
            "groups": [group.model_dump() for group in self.groups],
            "airplay2_instances": [item.model_dump() for item in self.airplay2_instances],
            "saved_curves": {
                name: [p.model_dump() for p in points] for name, points in self.saved_curves.items()
            },
        }
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(data, indent=2, ensure_ascii=False)
        # Never expose a partially-written JSON file. This matters on NAS
        # storage where a process/container can disappear halfway through a
        # write and make the application unbootable on the next start.
        fd, temporary = tempfile.mkstemp(
            prefix=f".{self.config_path.name}.",
            suffix=".tmp",
            dir=self.config_path.parent,
            text=True,
        )
        temporary_path = Path(temporary)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self.config_path)
        finally:
            temporary_path.unlink(missing_ok=True)
        self._config_revision += 1

    def snapshot(self) -> dict[str, Any]:
        """Return a deep runtime snapshot suitable for config rollback."""
        return {
            "fields": deepcopy(self.__dict__),
            "private": deepcopy(self.__pydantic_private__),
        }

    def restore(self, snapshot: dict[str, Any], *, persist: bool = True) -> None:
        """Restore an earlier snapshot after a runtime apply failure."""
        self.__dict__.clear()
        self.__dict__.update(deepcopy(snapshot["fields"]))
        self.__pydantic_private__.clear()
        self.__pydantic_private__.update(deepcopy(snapshot["private"]))
        if persist:
            self.save_to_file()

    def update_audio(self, **kwargs) -> None:
        """Validate and replace audio config atomically, then persist."""
        current = self.audio.model_dump()
        current.update({key: value for key, value in kwargs.items() if key in current})
        self.audio = AudioConfig.model_validate(current)
        self.save_to_file()

    def update_app_name(self, name: str) -> None:
        """Update application/AirPlay name and persist."""
        self.app = AppConfig(name=name)
        self.save_to_file()

    def set_receiver_mode(self, mode: str) -> None:
        """Set receiver mode and persist."""
        if mode not in ("single", "multi"):
            raise ValueError("receiver_mode must be 'single' or 'multi'")
        self.receiver_mode = mode
        self.save_to_file()

    def set_airplay_protocol(self, protocol: str) -> None:
        """Select the advertised AirPlay service type and persist it."""
        if protocol not in ("auto", "classic", "airplay2"):
            raise ValueError("airplay_protocol must be auto, classic, or airplay2")
        self.airplay_protocol = protocol
        self.save_to_file()

    def set_airplay_engine(self, engine: str) -> None:
        if engine not in ("local", "airplay2"):
            raise ValueError("airplay_engine must be local or airplay2")
        self.airplay_engine = engine
        self.save_to_file()

    def set_dlna_enabled(self, enabled: bool) -> None:
        self.dlna_enabled = enabled
        self.save_to_file()

    def set_ports(self, changes: dict[str, int | None]) -> None:
        """Update preferred ports and persist.

        None restores the default. env-pinned fields are rejected by the
        caller; here we simply refuse unknown keys and out-of-range values.
        Only the preferred value changes — already-bound listeners keep their
        actual port until their service restarts.
        """
        for key, value in changes.items():
            if key not in EDITABLE_PORTS:
                raise ValueError(f"未知端口项: {key}")
            if value is not None and not 1024 <= int(value) <= 65535:
                raise ValueError(f"端口需在 1024-65535 之间: {key}={value}")
        for key, value in changes.items():
            default = EDITABLE_PORTS[key][1]
            resolved_value = value if value is not None else default
            if key in ("port", "stream_port"):
                setattr(self, f"_preferred_{key}", resolved_value)
            else:
                setattr(self, key, resolved_value)
        self.save_to_file()

    def set_sync_groups_enabled(self, enabled: bool) -> None:
        self.sync_groups_enabled = enabled
        self.save_to_file()

    def set_large_delay_enabled(self, enabled: bool) -> None:
        self.large_delay_enabled = enabled
        if not enabled:
            for group in self.groups:
                group.delays_ms = {
                    key: max(-5000, min(5000, int(value))) for key, value in group.delays_ms.items()
                }
        self.save_to_file()

    def set_touchscreen_lyrics(self, enabled: bool) -> None:
        self.touchscreen_lyrics = enabled
        self.save_to_file()

    def set_default_volume(self, volume: int) -> None:
        self.default_volume = max(0, min(100, int(volume)))
        self.save_to_file()

    def set_stale_session_timeout(self, seconds: int) -> None:
        seconds = int(seconds)
        if seconds < 0:
            raise ValueError("stale_session_timeout must be a non-negative integer")
        self.stale_session_timeout = seconds
        self.save_to_file()

    def set_client_max_lag_seconds(self, seconds: float) -> None:
        """Tolerance for speaker clock drift before the delay line trims to
        live; the stream server reads it per chunk, so it hot-applies."""
        value = float(seconds)
        if not 0.5 <= value <= 30.0:
            raise ValueError("client_max_lag_seconds must be between 0.5 and 30")
        self.client_max_lag_seconds = value
        self.save_to_file()

    def set_notify_webhook(self, url: str) -> None:
        self.notify_webhook_url = url.strip()
        self.save_to_file()

    def set_airplay2_enabled(self, enabled: bool) -> None:
        self.airplay2_enabled = enabled
        self.save_to_file()

    def set_network_discovery_enabled(self, enabled: bool) -> None:
        self.network_discovery_enabled = enabled
        self.save_to_file()

    def configure_airplay2_deployment(self, mode: str) -> None:
        """Apply deployment capabilities without conflating single and multi."""
        if mode == "disabled":
            self.airplay2_enabled = False
            self.airplay2_instances = []
            self.save_to_file()
            return
        if mode == "single":
            current = self.airplay2_instances[0] if self.airplay2_instances else None
            target_type = current.target_type if current else "speaker"
            target_id = current.target_id if current else (self.selected_device_id or "unmapped")
            self.airplay2_instances = [
                AirPlay2InstanceConfig(
                    id="airplay2",
                    name="MiCast",
                    target_type=target_type,
                    target_id=target_id,
                    enabled=True,
                )
            ]
        self.save_to_file()

    def bind_provider_account(self, account_id: str | None) -> bool:
        """Bind device relationships to a Xiaomi account.

        Returns True when an account switch cleared provider-owned topology.
        Application/audio settings are account-independent and survive the switch.
        """
        normalized = str(account_id).strip() if account_id else None
        if not normalized:
            return False
        if self.provider_account_id in (None, normalized):
            self.provider_account_id = normalized
            self.save_to_file()
            return False

        self.provider_account_id = normalized
        self.selected_device_id = None
        self.speakers = []
        self.groups = []
        self.receivers = [item for item in self.receivers if item.target_type == "selected"]
        self.airplay2_instances = []
        self.save_to_file()
        return True

    def upsert_airplay2_instance(
        self,
        *,
        instance_id: str | None,
        name: str,
        target_type: str,
        target_id: str,
        enabled: bool = True,
    ) -> AirPlay2InstanceConfig:
        current = next((item for item in self.airplay2_instances if item.id == instance_id), None)
        updated = AirPlay2InstanceConfig(
            id=instance_id or uuid.uuid4().hex[:12],
            name=name.strip(),
            target_type=target_type,
            target_id=target_id,
            enabled=enabled,
        )
        if current:
            self.airplay2_instances[self.airplay2_instances.index(current)] = updated
        else:
            self.airplay2_instances.append(updated)
        self.save_to_file()
        return updated

    def remove_airplay2_instance(self, instance_id: str) -> bool:
        before = len(self.airplay2_instances)
        self.airplay2_instances = [
            item for item in self.airplay2_instances if item.id != instance_id
        ]
        if len(self.airplay2_instances) == before:
            return False
        self.save_to_file()
        return True

    def active_receivers(self) -> list[ReceiverConfig]:
        """Receivers currently published by local discovery protocols."""
        return [
            receiver
            for receiver in self.receivers
            if receiver.enabled and (self.sync_groups_enabled or receiver.target_type != "group")
        ]

    def set_orchestrator(self, url: str, token: str | None = None) -> None:
        self.orchestrator_url = url.strip().rstrip("/")
        if token is not None and token.strip():
            self.orchestrator_token = token.strip()
        self.save_to_file()

    def _migrate_receivers(self) -> None:
        if not self.receivers and self.receiver_mode == "multi":
            self.receivers = [
                ReceiverConfig(
                    id=f"speaker-{speaker.did}",
                    name=speaker.alias or speaker.did,
                    target_type="speaker",
                    target_id=speaker.did,
                )
                for speaker in self.speakers
                if speaker.enabled
            ]
        speaker_names = {speaker.did: speaker.alias for speaker in self.speakers if speaker.alias}
        group_names = {group.id: group.name for group in self.groups}
        for receiver in self.receivers:
            if receiver.target_type == "speaker" and receiver.target_id in speaker_names:
                receiver.name = speaker_names[receiver.target_id]
            elif receiver.target_type == "group" and receiver.target_id in group_names:
                receiver.name = group_names[receiver.target_id]

    def receiver_targets(self, receiver_id: str) -> list[str]:
        receiver = next((item for item in self.receivers if item.id == receiver_id), None)
        if receiver is None:
            instance = next(
                (item for item in self.airplay2_instances if item.id == receiver_id), None
            )
            if instance:
                if instance.target_type == "speaker":
                    return [instance.target_id]
                group = next((item for item in self.groups if item.id == instance.target_id), None)
                return list(group.speaker_ids) if group else []
        if receiver is None:
            return []
        if receiver.target_type == "selected":
            return [self.selected_device_id] if self.selected_device_id else []
        if receiver.target_type == "speaker":
            return [receiver.target_id] if receiver.target_id else []
        group = next((item for item in self.groups if item.id == receiver.target_id), None)
        return list(group.speaker_ids) if group else []

    def receiver_target_delays(self, receiver_id: str) -> dict[str, int]:
        receiver = next((item for item in self.receivers if item.id == receiver_id), None)
        if receiver is None or receiver.target_type != "group":
            return {}
        group = next((item for item in self.groups if item.id == receiver.target_id), None)
        return dict(group.delays_ms) if group else {}

    def group_for_receiver(self, receiver_id: str) -> SpeakerGroupConfig | None:
        receiver = next((item for item in self.receivers if item.id == receiver_id), None)
        if receiver is not None and receiver.target_type == "group":
            return next((item for item in self.groups if item.id == receiver.target_id), None)
        # An AirPlay 2 instance can also target a group; its receiver identity
        # lives in airplay2_instances (not receivers), so resolve it here too —
        # otherwise channel/EQ/stream-variant lookups miss the group for it.
        instance = next((item for item in self.airplay2_instances if item.id == receiver_id), None)
        if instance is not None and instance.target_type == "group":
            return next((item for item in self.groups if item.id == instance.target_id), None)
        return None

    def sink_hold_ms(self, receiver_id: str, did: str) -> int:
        """Return the live hold for ``did`` in one specific playback entry.

        A speaker may belong to several groups. Looking it up by device id
        alone silently applied the first matching group's delay, which made
        the delay control of every other group appear to do nothing.
        """
        group = self.group_for_receiver(receiver_id)
        if group is None or did not in group.speaker_ids:
            return 0
        return group.delay_holds().get(did, 0)

    def receiver_channel(self, receiver_id: str, did: str) -> str | None:
        """Channel ('left'/'right') a speaker plays in a stereo group, else None."""
        group = self.group_for_receiver(receiver_id)
        if group is None or group.mode != "stereo":
            return None
        channel = group.channels.get(did)
        return channel if channel in ("left", "right") else None

    def receiver_airplay_targets(self, receiver_id: str) -> list[str]:
        """External AirPlay device ids attached to a receiver's group."""
        group = self.group_for_receiver(receiver_id)
        return list(group.airplay_targets) if group else []

    def receiver_dlna_targets(self, receiver_id: str) -> list[str]:
        """External DLNA renderer UDNs attached to a receiver's group."""
        group = self.group_for_receiver(receiver_id)
        return list(group.dlna_targets) if group else []

    def receiver_airplay_delays(self, receiver_id: str) -> dict[str, int]:
        """Normalized hold (ms) for a receiver's AirPlay targets, sharing the
        group-wide normalization with the Xiaomi pull path (most-ahead member
        across the whole group sits at 0)."""
        group = self.group_for_receiver(receiver_id)
        if group is None:
            return {}
        holds = group.delay_holds()
        return {k: holds.get(k, 0) for k in group.airplay_targets}

    def receiver_network_channels(self, receiver_id: str) -> dict[str, str]:
        """Channel assignment (left/right) per attached network device id."""
        group = self.group_for_receiver(receiver_id)
        return dict(group.network_channels) if group else {}

    def receiver_channel_variant_suffix(self, receiver_id: str, side: str) -> str:
        """Stream URL suffix serving one channel of a stereo group ("left" →
        "-L"). Prefers the EQ-less variant; "" when no such variant exists
        (mirror group), which conveniently means the plain base stream."""
        wanted_base = {"left": "-L", "right": "-R"}.get(side)
        if wanted_base is None:
            return ""
        variants = self.receiver_stream_variants(receiver_id)
        matching = [v for v in variants if v["base"] == wanted_base]
        if not matching:
            return ""
        plain = next((v for v in matching if not v["eq"]), None)
        return (plain or matching[0])["suffix"]

    def channel_suffix(self, receiver_id: str, did: str) -> str:
        """Stream URL suffix so each speaker of a stereo pair fetches its channel."""
        channel = self.receiver_channel(receiver_id, did)
        return {"left": "-L", "right": "-R"}.get(channel, "")

    def speaker_eq_curve(self, did: str) -> tuple[tuple[float, float], ...] | None:
        """Canonical EQ curve signature for a speaker; None when flat and no
        night mode. Night mode layers its bass shelf onto the active curve, so
        it also works with EQ disabled (a flat base curve)."""
        speaker = self.get_speaker(did)
        if not speaker:
            return None
        points = [(p.freq, p.gain_db) for p in speaker.eq_points] if speaker.eq_enabled else []
        if speaker.night_mode:
            points = add_curve(points, NIGHT_ATTENUATION)
        return curve_signature(points)

    def set_speaker_eq_curve(
        self,
        did: str,
        *,
        enabled: bool,
        points: list[tuple[float, float]],
        preset: str = "",
        target: str | None = None,
    ) -> SpeakerConfig:
        speaker = self._get_or_create_speaker(did)
        self._checkpoint_speaker_tuning(speaker)
        speaker.eq_enabled = enabled
        speaker.eq_points = [EqPoint(freq=f, gain_db=g) for f, g in normalize_points(points)]
        speaker.eq_preset = preset if preset in EQ_PRESET_POINTS else ""
        # A manual curve edit (or calibration/import) is a custom curve, not a
        # scene — clear the active-scene label so it stops claiming a saved one.
        speaker.content_profile = ""
        if target is not None:
            # The reference overlay accepts a built-in target name or a curve
            # from the library ("saved:<name>" / "preset:<key>"); anything else
            # (e.g. a since-deleted saved curve) collapses to no overlay.
            speaker.eq_target = (
                target
                if target in TARGET_CURVES
                or (target.startswith("saved:") and target[6:] in self.saved_curves)
                or (target.startswith("preset:") and target[7:] in EQ_PRESET_POINTS)
                else ""
            )
        self.save_to_file()
        return speaker

    def set_speaker_eq_target(self, did: str, target: str) -> SpeakerConfig:
        """Persist the editor's visual reference without changing audio state."""

        speaker = self._get_or_create_speaker(did)
        speaker.eq_target = (
            target
            if target in TARGET_CURVES
            or (target.startswith("saved:") and target[6:] in self.saved_curves)
            or (target.startswith("preset:") and target[7:] in EQ_PRESET_POINTS)
            else ""
        )
        # Reference selection is display state, not an audio adjustment: keep
        # the existing undo checkpoint but publish a revision for other tabs.
        speaker.eq_revision += 1
        self.save_to_file()
        return speaker

    @staticmethod
    def _speaker_tuning_snapshot(speaker: SpeakerConfig) -> SpeakerEqUndo:
        return SpeakerEqUndo(
            enabled=speaker.eq_enabled,
            points=[EqPoint(freq=p.freq, gain_db=p.gain_db) for p in speaker.eq_points],
            preset=speaker.eq_preset,
            target=speaker.eq_target,
            night_mode=speaker.night_mode,
            loudness_comp_enabled=speaker.loudness_comp_enabled,
            content_profile=speaker.content_profile,
        )

    def _checkpoint_speaker_tuning(self, speaker: SpeakerConfig) -> None:
        """Capture the single undo slot before an audible tuning change.

        The checkpoint covers exactly the audible tuning surface — EQ curve
        (enabled/points/preset), the reference target overlay, night mode,
        equal-loudness compensation and the active scene — so undo restores
        what the listener hears. ``set_speaker_eq_target`` deliberately does
        NOT checkpoint: it only moves the editor's visual reference, so it
        bumps ``eq_revision`` for cross-client sync but keeps the existing
        checkpoint intact.
        """
        speaker.eq_undo = self._speaker_tuning_snapshot(speaker)
        speaker.eq_revision += 1

    def undo_speaker_tuning(self, did: str) -> SpeakerConfig:
        speaker = self._get_or_create_speaker(did)
        snapshot = speaker.eq_undo
        if snapshot is None:
            raise ValueError("没有可撤销的调音调整")
        speaker.eq_enabled = snapshot.enabled
        speaker.eq_points = [EqPoint(freq=p.freq, gain_db=p.gain_db) for p in snapshot.points]
        speaker.eq_preset = snapshot.preset
        speaker.eq_target = snapshot.target
        speaker.night_mode = snapshot.night_mode
        speaker.loudness_comp_enabled = snapshot.loudness_comp_enabled
        speaker.content_profile = snapshot.content_profile
        speaker.eq_undo = None
        speaker.eq_revision += 1
        self.save_to_file()
        return speaker

    # ---- Global curve library ----

    def list_saved_curves(self) -> dict[str, list[tuple[float, float]]]:
        return {
            name: [(p.freq, p.gain_db) for p in points]
            for name, points in sorted(self.saved_curves.items())
        }

    @staticmethod
    def _validate_curve_name(name: str) -> str:
        name = name.strip()
        if not name or len(name) > 32:
            raise ValueError("曲线名称需为 1-32 个字符")
        return name

    def save_curve(self, name: str, points: list[tuple[float, float]]) -> None:
        """Store the current curve under a user-chosen name."""
        name = self._validate_curve_name(name)
        if name in self.saved_curves:
            raise ValueError("已存在同名曲线")
        self.saved_curves[name] = [EqPoint(freq=f, gain_db=g) for f, g in normalize_points(points)]
        self.save_to_file()

    def delete_saved_curve(self, name: str) -> None:
        if self.saved_curves.pop(name, None) is not None:
            self.save_to_file()

    def rename_saved_curve(self, old: str, new: str) -> None:
        if old not in self.saved_curves:
            raise ValueError("曲线不存在")
        new = self._validate_curve_name(new)
        if new != old and new in self.saved_curves:
            raise ValueError("已存在同名曲线")
        self.saved_curves[new] = self.saved_curves.pop(old)
        self.save_to_file()

    def set_speaker_night_mode(self, did: str, enabled: bool) -> SpeakerConfig:
        speaker = self._get_or_create_speaker(did)
        if speaker.night_mode == bool(enabled):
            return speaker
        self._checkpoint_speaker_tuning(speaker)
        speaker.night_mode = bool(enabled)
        self.save_to_file()
        return speaker

    def set_speaker_loudness(self, did: str, enabled: bool) -> SpeakerConfig:
        speaker = self._get_or_create_speaker(did)
        if speaker.loudness_comp_enabled == bool(enabled):
            return speaker
        self._checkpoint_speaker_tuning(speaker)
        speaker.loudness_comp_enabled = bool(enabled)
        self.save_to_file()
        return speaker

    def speaker_loudness(self, did: str) -> bool:
        """True when a speaker has equal-loudness compensation enabled."""
        speaker = self.get_speaker(did)
        return bool(speaker and speaker.loudness_comp_enabled)

    def save_speaker_profile(self, did: str, profile: str) -> SpeakerConfig:
        """Save the speaker's current curve into a named scene slot."""
        speaker = self._get_or_create_speaker(did)
        if profile not in CONTENT_PROFILES:
            raise ValueError(f"unknown content profile: {profile}")
        speaker.eq_profiles[profile] = [
            EqPoint(freq=p.freq, gain_db=p.gain_db) for p in speaker.eq_points
        ]
        self.save_to_file()
        return speaker

    def set_speaker_content_profile(self, did: str, profile: str) -> SpeakerConfig:
        """Activate a saved scene, copying its curve into the active EQ."""
        speaker = self._get_or_create_speaker(did)
        if profile not in CONTENT_PROFILES:
            raise ValueError(f"unknown content profile: {profile}")
        points = speaker.eq_profiles.get(profile)
        if not points:
            raise ValueError(f"content profile not saved: {profile}")
        self._checkpoint_speaker_tuning(speaker)
        speaker.eq_points = [EqPoint(freq=p.freq, gain_db=p.gain_db) for p in points]
        speaker.eq_enabled = True
        speaker.eq_preset = ""
        speaker.content_profile = profile
        self.save_to_file()
        return speaker

    def delete_speaker_profile(self, did: str, profile: str) -> SpeakerConfig:
        speaker = self._get_or_create_speaker(did)
        speaker.eq_profiles.pop(profile, None)
        if speaker.content_profile == profile:
            speaker.content_profile = ""
        self.save_to_file()
        return speaker

    def set_speaker_eq(
        self, did: str, *, enabled: bool, bands: list[float], preset: str = ""
    ) -> SpeakerConfig:
        """Deprecated 10-band slider API: converts band gains to control points."""
        bands = list(bands)
        # Same 5→10 migration as the config validator: a stale client posting
        # the old 60/250/1k/4k/12k layout lands on the nearest ISO bands.
        if len(bands) == 5 and EQ_BAND_COUNT == 10:
            bands = [0.0, bands[0], 0.0, bands[1], 0.0, bands[2], 0.0, bands[3], 0.0, bands[4]]
        points = legacy_bands_to_points(EQ_BANDS_HZ, bands)
        return self.set_speaker_eq_curve(did, enabled=enabled, points=points, preset=preset)

    def needs_plain_base(self, receiver_id: str) -> bool:
        """Whether anything can actually consume the un-split, un-EQ'd mix.

        The plain base stream is a whole extra encoder plus tee branch, so it
        is published only when a sink that resolves to it exists: an external
        DLNA renderer or AirPlay target attached to the group (they pull the
        bare entry / the base mix — a network member without a channel
        assignment is one of those), or a target whose own channel + EQ +
        loudness all land on the plain suffix.

        Anything else means nobody can ask for it — field data (0.3.3): a
        single EQ'd speaker kept publishing a base stream that broadcast 4 MB
        over 100 s with zero clients (the speaker pulls its ``-q1``), while
        that box was already CPU-starved and its speakers were stuttering.
        """
        # External consumers: DLNA renderers pull /stream/{entry} directly and
        # the AirPlay-target tap carries the base mix.
        if self.receiver_dlna_targets(receiver_id) or self.receiver_airplay_targets(receiver_id):
            return True
        # A target whose channel, EQ and loudness together resolve to the
        # plain suffix. Deliberately not stream_suffix()/receiver_stream_variants:
        # the variants ask this question, so calling them here recurses.
        return any(
            self.channel_suffix(receiver_id, did) == ""
            and self.speaker_eq_curve(did) is None
            and not self.speaker_loudness(did)
            for did in self.receiver_targets(receiver_id)
        )

    def receiver_stream_variants(self, receiver_id: str) -> list[dict]:
        """Streams a receiver must publish: one per (channel, EQ, loudness).

        Returns entries of {suffix, base, channel, eq, loudness}; the plain
        entry of each channel keeps the base suffix (-L/-R/"") so speakers
        without EQ or loudness share one stream exactly as before. Distinct
        non-flat signatures (or loudness on/off) get ``-q{n}`` (or ``-Lq{n}``)
        in order of first appearance.
        """
        variants: list[dict] = []
        seen: set[tuple[str, tuple[tuple[float, float], ...] | None, bool]] = set()
        eq_counts: dict[str, int] = {}
        for did in self.receiver_targets(receiver_id):
            channel = self.receiver_channel(receiver_id, did)
            base = {"left": "-L", "right": "-R"}.get(channel, "")
            curve = self.speaker_eq_curve(did)
            loudness = self.speaker_loudness(did)
            key = (base, curve, loudness)
            if key in seen:
                continue
            seen.add(key)
            if curve is None and not loudness:
                suffix = base
            else:
                eq_counts[base] = eq_counts.get(base, 0) + 1
                suffix = f"{base}-q{eq_counts[base]}"
            variants.append(
                {
                    "suffix": suffix,
                    "base": base,
                    "channel": channel,
                    "eq": curve,
                    "loudness": loudness,
                }
            )
        # Network devices with a channel assignment need that channel's
        # stream too (DLNA renderers pull it directly) — even when the group
        # has no Xiaomi speaker holding it.
        group = self.group_for_receiver(receiver_id)
        if group and group.mode == "stereo":
            assigned_sides = set(group.network_channels.values())
            for side, base in (("left", "-L"), ("right", "-R")):
                if side in assigned_sides and (base, None, False) not in seen:
                    seen.add((base, None, False))
                    variants.append(
                        {
                            "suffix": base,
                            "base": base,
                            "channel": side,
                            "eq": None,
                            "loudness": False,
                        }
                    )
        # The plain base stream exists only when something consumes it: the
        # cheap raw-PCM bypass for mirror speakers, DLNA renderers without a
        # channel assignment, external targets, and anything else that just
        # wants the mix. A channel-split group needs none of them.
        if self.needs_plain_base(receiver_id) and ("", None, False) not in seen:
            variants.append(
                {"suffix": "", "base": "", "channel": None, "eq": None, "loudness": False}
            )
        return variants

    def stream_suffix(self, receiver_id: str, did: str) -> str:
        """Full stream URL suffix (channel + EQ + loudness split) for one speaker.

        The wanted split is the published variant matching this sink's channel
        + EQ + loudness. When no variant matches — a sink whose tuning is not
        part of this receiver's plan — the suffix falls back to a variant that
        IS published for the same channel (the plain one, else the first), so
        the URL never names a stream nobody registered: the speaker's player
        reads the resulting 404 as "this format is unsupported". The plain
        channel suffix is the last resort only.
        """
        base = self.channel_suffix(receiver_id, did)
        curve = self.speaker_eq_curve(did)
        loudness = self.speaker_loudness(did)
        if curve is None and not loudness:
            return base
        variants = self.receiver_stream_variants(receiver_id)
        for variant in variants:
            if (variant["base"], variant["eq"], variant["loudness"]) == (base, curve, loudness):
                return variant["suffix"]
        matching = [variant for variant in variants if variant["base"] == base]
        if not matching:
            return base
        plain = next((v for v in matching if v["eq"] is None and not v["loudness"]), None)
        return (plain or matching[0])["suffix"]

    def stream_id_for(self, receiver_id: str, did: str | None = None) -> str:
        """Stream endpoint that serves one sink of a receiver.

        The suffix already carries channel + EQ split, so this is the ONLY
        correct way to build a play URL: appending a suffix to an already
        suffixed stream id asks for a stream that does not exist (a 404 the
        speaker reads as "nothing to play").
        """
        if did is None:
            return receiver_id
        return f"{receiver_id}{self.stream_suffix(receiver_id, did)}"

    def stream_url_for(self, receiver_id: str, did: str | None = None) -> str:
        """Full play URL (up to the stream name) of one receiver for one sink."""
        return (
            f"http://{self.effective_stream_host}:{self.stream_port}"
            f"/stream/{self.stream_id_for(receiver_id, did)}"
        )

    def audio_entry_ids(self) -> list[str]:
        """Every id an audio entry can have: receivers + AirPlay 2 instances."""
        return [item.id for item in self.receivers] + [item.id for item in self.airplay2_instances]

    def entry_id_of_stream(self, stream_id: str) -> str | None:
        """Base entry id a stream id belongs to.

        Stream ids are ``<entry_id>`` or ``<entry_id>-L/-R/-qN`` variants, so
        the longest entry id the stream starts with is its owner. Recovery has
        to act on the ENTRY (rebuild its pipelines), never on the variant.
        """
        matches = [
            entry_id
            for entry_id in self.audio_entry_ids()
            if stream_id == entry_id or stream_id.startswith(f"{entry_id}-")
        ]
        return max(matches, key=len) if matches else None

    def add_receiver(
        self, name: str, target_type: str, target_id: str | None = None
    ) -> ReceiverConfig:
        receiver = ReceiverConfig(
            id=uuid.uuid4().hex[:12], name=name, target_type=target_type, target_id=target_id
        )
        self.receivers.append(receiver)
        self.save_to_file()
        return receiver

    def remove_receiver(self, receiver_id: str) -> bool:
        before = len(self.receivers)
        self.receivers = [item for item in self.receivers if item.id != receiver_id]
        if len(self.receivers) == before:
            return False
        self.save_to_file()
        return True

    def update_receiver(
        self,
        receiver_id: str,
        *,
        name: str | None = None,
        target_type: str | None = None,
        target_id: str | None = None,
        enabled: bool | None = None,
    ) -> ReceiverConfig | None:
        receiver = next((item for item in self.receivers if item.id == receiver_id), None)
        if receiver is None:
            return None
        data = receiver.model_dump()
        if name is not None:
            data["name"] = name
        if target_type is not None:
            data["target_type"] = target_type
            data["target_id"] = target_id
        elif target_id is not None:
            data["target_id"] = target_id
        if enabled is not None:
            data["enabled"] = enabled
        updated = ReceiverConfig.model_validate(data)
        self.receivers[self.receivers.index(receiver)] = updated
        self.save_to_file()
        return updated

    def add_group(
        self,
        name: str,
        speaker_ids: list[str],
        airplay_targets: list[str] | None = None,
        dlna_targets: list[str] | None = None,
    ) -> SpeakerGroupConfig:
        group = SpeakerGroupConfig(
            id=uuid.uuid4().hex[:12],
            name=name,
            speaker_ids=list(dict.fromkeys(speaker_ids)),
            airplay_targets=_sanitize_airplay_targets(airplay_targets or []),
            dlna_targets=_sanitize_dlna_targets(dlna_targets or []),
        )
        if group.member_count < 2:
            raise ValueError("组合至少需要两个成员（音箱或网络设备）")
        self.groups.append(group)
        self.save_to_file()
        return group

    def update_group(
        self,
        group_id: str,
        *,
        name: str | None = None,
        speaker_ids: list[str] | None = None,
        delays_ms: dict[str, int] | None = None,
        mode: str | None = None,
        channels: dict[str, str] | None = None,
        gains_db: dict[str, float] | None = None,
        airplay_targets: list[str] | None = None,
        dlna_targets: list[str] | None = None,
        network_channels: dict[str, str] | None = None,
        anchor_did: str | None = None,
    ) -> SpeakerGroupConfig | None:
        group = next((item for item in self.groups if item.id == group_id), None)
        if group is None:
            return None
        data = group.model_dump()
        if name is not None:
            data["name"] = name
        if speaker_ids is not None:
            data["speaker_ids"] = list(dict.fromkeys(speaker_ids))
        delay_limit_ms = 15000 if self.large_delay_enabled else 5000
        if delays_ms is not None:
            data["delays_ms"] = {
                str(key): max(-delay_limit_ms, min(delay_limit_ms, int(value)))
                for key, value in delays_ms.items()
            }
        if mode is not None:
            if mode not in ("mirror", "stereo"):
                raise ValueError("mode must be 'mirror' or 'stereo'")
            data["mode"] = mode
        if channels is not None:
            data["channels"] = {
                str(key): value
                for key, value in channels.items()
                if value in ("left", "right", "both")
            }
        if gains_db is not None:
            data["gains_db"] = {
                str(key): max(-12.0, min(12.0, float(value))) for key, value in gains_db.items()
            }
        if airplay_targets is not None:
            data["airplay_targets"] = _sanitize_airplay_targets(airplay_targets)
        if dlna_targets is not None:
            data["dlna_targets"] = _sanitize_dlna_targets(dlna_targets)
        if network_channels is not None:
            data["network_channels"] = {
                str(key): value
                for key, value in network_channels.items()
                if value in ("left", "right")
            }
        if anchor_did is not None:
            if anchor_did not in data["speaker_ids"]:
                raise ValueError("anchor_did 必须是组合中的音箱")
            old_anchor = data.get("anchor_did")
            if delays_ms is None and old_anchor and old_anchor != anchor_did:
                # Re-anchor without new offsets: shift the reference frame so
                # the physical (min-normalized) timing is unchanged.
                members = [
                    *data["speaker_ids"],
                    *data.get("airplay_targets", []),
                    *data.get("dlna_targets", []),
                ]
                offsets = {member: data["delays_ms"].get(member, 0) for member in members}
                shift = offsets.get(anchor_did, 0)
                data["delays_ms"] = {
                    member: value - shift
                    for member, value in offsets.items()
                    if member != anchor_did and value != shift
                }
            data["anchor_did"] = anchor_did
            # The anchor is the reference: its offset is always 0.
            data["delays_ms"].pop(anchor_did, None)
        # A stereo group needs at least two members (any mix of Xiaomi
        # speakers and network devices); every speaker holds a channel
        # (any mix allowed — all-right is valid). Unassigned speakers
        # default to first=left, the rest=right.
        if data["mode"] == "stereo":
            member_count = (
                len(data["speaker_ids"])
                + len(data.get("airplay_targets") or [])
                + len(data.get("dlna_targets") or [])
            )
            if member_count < 2:
                raise ValueError("立体声组至少需要两个成员（音箱或网络设备）")
            assigned = {
                did: channel
                for did, channel in data["channels"].items()
                if did in data["speaker_ids"] and channel in ("left", "right", "both")
            }
            for index, did in enumerate(data["speaker_ids"]):
                if did not in assigned:
                    assigned[did] = "left" if index == 0 else "right"
            data["channels"] = assigned
        updated = SpeakerGroupConfig.model_validate(data)
        self.groups[self.groups.index(group)] = updated
        if name is not None:
            for receiver in self.receivers:
                if receiver.target_type == "group" and receiver.target_id == group_id:
                    receiver.name = updated.name
        self.save_to_file()
        return updated

    def target_references(self, target_type: str, target_id: str) -> list[dict]:
        """Every entry pointing at a speaker/group, for pre-delete checks.

        Returns ``[{"kind": "receiver"|"airplay2"|"group", "id", "name"}]`` —
        classic receivers, AirPlay 2 instances, and (for speakers) the groups
        the speaker belongs to.
        """
        refs: list[dict] = []
        for receiver in self.receivers:
            if receiver.target_type == target_type and receiver.target_id == target_id:
                refs.append({"kind": "receiver", "id": receiver.id, "name": receiver.name})
        for instance in self.airplay2_instances:
            if instance.target_type == target_type and instance.target_id == target_id:
                refs.append({"kind": "airplay2", "id": instance.id, "name": instance.name})
        if target_type == "speaker":
            for group in self.groups:
                if target_id in group.speaker_ids:
                    refs.append({"kind": "group", "id": group.id, "name": group.name})
        return refs

    def remove_group(self, group_id: str) -> bool:
        if any(
            item.target_type == "group" and item.target_id == group_id for item in self.receivers
        ):
            return False
        # AirPlay 2 instances reference groups too — deleting underneath them
        # leaves a dangling mapping whose runtime silently collapses.
        if any(
            item.target_type == "group" and item.target_id == group_id
            for item in self.airplay2_instances
        ):
            return False
        before = len(self.groups)
        self.groups = [item for item in self.groups if item.id != group_id]
        if len(self.groups) == before:
            return False
        self.save_to_file()
        return True

    def select_device(self, did: str | None) -> None:
        """Persist selected device for single-receiver mode."""
        self.selected_device_id = did
        self.save_to_file()

    def _get_or_create_speaker(self, did: str) -> SpeakerConfig:
        """Return existing speaker config or create a new one."""
        for speaker in self.speakers:
            if speaker.did == did:
                return speaker
        speaker = SpeakerConfig(did=did)
        self.speakers.append(speaker)
        return speaker

    def get_speaker(self, did: str) -> SpeakerConfig | None:
        """Return persisted speaker config if it exists."""
        for speaker in self.speakers:
            if speaker.did == did:
                return speaker
        return None

    def set_alias(self, did: str, alias: str) -> None:
        """Set the speaker alias and keep its AirPlay receiver name in sync."""
        speaker = self._get_or_create_speaker(did)
        speaker.alias = alias
        for receiver in self.receivers:
            if receiver.target_type == "speaker" and receiver.target_id == did:
                receiver.name = alias
        self.save_to_file()

    def set_enabled(self, did: str, enabled: bool) -> None:
        """Set whether a speaker is enabled as an independent AirPlay target."""
        speaker = self._get_or_create_speaker(did)
        speaker.enabled = enabled
        receiver_id = f"speaker-{did}"
        existing = next((item for item in self.receivers if item.id == receiver_id), None)
        if enabled and existing is None:
            self.receivers.append(
                ReceiverConfig(
                    id=receiver_id,
                    name=speaker.alias or did,
                    target_type="speaker",
                    target_id=did,
                    enabled=True,
                )
            )
        elif existing is not None:
            existing.enabled = enabled
            existing.name = speaker.alias or existing.name
        self.save_to_file()

    _merge_rewrote: bool = PrivateAttr(default=False)

    def consume_merge_rewrite(self) -> bool:
        """True (once) when the last merge_speakers rewrote a device id.

        The bridge uses this to rebuild the pipelines whose stream ids and
        targets still reference the obsolete id.
        """
        rewrote = self._merge_rewrote
        self._merge_rewrote = False
        return rewrote

    def merge_speakers(self, discovered: list[dict]) -> list[SpeakerConfig]:
        """Merge discovered Xiaomi devices with persisted config.

        Newly discovered devices are added with alias = native name and enabled = false.
        Returns the merged list in the same order as discovered devices.
        """
        persisted = {s.did: s for s in self.speakers}
        by_miot = {s.miot_did: s for s in self.speakers if s.miot_did}
        discovered_name_counts: dict[str, int] = {}
        for device in discovered:
            name = str(device.get("name") or "").strip()
            if name:
                discovered_name_counts[name] = discovered_name_counts.get(name, 0) + 1

        merged: list[SpeakerConfig] = []
        consumed: set[str] = set()
        for device in discovered:
            did = str(device.get("deviceID") or "").strip()
            if not did:
                continue
            miot_did = str(device.get("miotDID") or "").strip()
            hardware = str(device.get("hardware") or "").strip()
            native_name = str(device.get("name") or "").strip()

            existing = persisted.get(did) or (by_miot.get(miot_did) if miot_did else None)
            if existing is None:
                existing = SpeakerConfig(did=did, alias=native_name)
            elif existing.did != did:
                old_did = existing.did
                existing.did = did
                self._replace_speaker_reference(old_did, did)

            # Legacy files did not persist miotDID.  If one currently-discovered
            # speaker has this name, consolidate its obsolete same-name records.
            legacy_duplicates = [
                item
                for item in self.speakers
                if item.did != did
                and item.did not in consumed
                and native_name
                and discovered_name_counts.get(native_name) == 1
                and item.alias == native_name
                and not item.miot_did
            ]
            for duplicate in legacy_duplicates:
                existing.enabled = existing.enabled or duplicate.enabled
                self._replace_speaker_reference(duplicate.did, did)
                consumed.add(duplicate.did)

            if not existing.alias:
                existing.alias = native_name
            existing.miot_did = miot_did or existing.miot_did
            existing.hardware = hardware or existing.hardware
            consumed.add(did)
            merged.append(existing)

        # Preserve order: persisted speakers not currently discovered go at the end.
        for speaker in self.speakers:
            if speaker.did not in consumed:
                merged.append(speaker)
        self.speakers = merged
        self.save_to_file()
        return merged

    def _replace_speaker_reference(self, old_did: str, new_did: str) -> None:
        """Move every persisted relationship from an obsolete ID to a current one."""
        if not old_did or old_did == new_did:
            return
        self._merge_rewrote = True
        if self.selected_device_id == old_did:
            self.selected_device_id = new_did
        for receiver in self.receivers:
            if receiver.target_type == "speaker" and receiver.target_id == old_did:
                receiver.target_id = new_did
        for instance in self.airplay2_instances:
            if instance.target_type == "speaker" and instance.target_id == old_did:
                instance.target_id = new_did
        for group in self.groups:
            group.speaker_ids = list(
                dict.fromkeys(new_did if item == old_did else item for item in group.speaker_ids)
            )
            for mapping in (group.delays_ms, group.channels, group.gains_db):
                if old_did in mapping:
                    value = mapping.pop(old_did)
                    mapping.setdefault(new_did, value)
            if group.anchor_did == old_did:
                group.anchor_did = new_did


settings = Settings()
settings.load_from_file()
settings._migrate_receivers()
