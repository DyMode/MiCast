"""Persisted domain models and model-local legacy migrations. No environment or IO."""

import re
from typing import Any

from pydantic import BaseModel, Field, model_validator

from micast.curve_fit import CURVE_FREQ_RANGE, CURVE_GAIN_RANGE, TARGET_CURVES

EQ_GAIN_RANGE = CURVE_GAIN_RANGE

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
        members = [*self.speaker_ids, *self.airplay_targets, *self.dlna_targets]
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
