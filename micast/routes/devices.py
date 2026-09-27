"""Device discovery routes."""

import asyncio

from fastapi import APIRouter, HTTPException

from micast.audio_bridge import AudioBridge
from micast.config import EQ_PRESET_POINTS, settings
from micast.config_apply import apply_config_transaction
from micast.curve_fit import CURVE_FREQ_RANGE, CURVE_GAIN_RANGE, TARGET_CURVES
from micast.xiaomi.auth import XiaomiAuthError
from micast.xiaomi.device_manager import CODEC_FORMATS, CODEC_LABELS, DeviceManager

router = APIRouter(prefix="/api/devices", tags=["devices"])

# Fallback names for common Xiaomi speaker hardware codes
_HARDWARE_NAMES: dict[str, str] = {
    "LX06": "小爱音箱 Pro",
    "LX01": "小爱音箱 mini",
    "L06A": "小爱音箱",
    "L06B": "小爱音箱 mini",
    "L06C": "小爱音箱 Play",
    "L09G": "小爱音箱 HD",
    "S12": "小爱音箱 Art",
    "S12A": "小爱音箱 Art 电池版",
    "L16A": "Xiaomi Sound",
    "OH2P": "Xiaomi 智能音箱 Pro",
    "OH2": "Xiaomi 智能音箱",
    "L05B": "小爱音箱 Play",
    "L05C": "小爱音箱 Play",
    "L15A": "Xiaomi Sound Pro",
    "L17A": "Xiaomi Sound Move",
}


def _clean_name(raw: str | None, hardware: str | None) -> str:
    """Prefer a verified hardware mapping when Xiaomi returns a poor device name."""
    if hardware and hardware in _HARDWARE_NAMES:
        return _HARDWARE_NAMES[hardware]
    if raw and raw.strip():
        return raw
    return hardware or "未知设备"


def install(device_manager: DeviceManager, bridge: AudioBridge | None = None) -> APIRouter:
    @router.get("")
    async def get_devices(refresh: bool = False):
        try:
            devices = await device_manager.list_devices(force=refresh)
            if bridge and device_manager.consume_merge_rewrite():
                # A device id migrated under us: stream ids and pipeline
                # targets still reference the old did — rebuild the affected
                # entries now instead of after the next unrelated restart.
                await bridge.apply_config_change()
            device_ids = [d.get("deviceID") for d in devices]
            volumes = await asyncio.gather(
                *(
                    device_manager.get_volume(did, refresh=True) if did else _no_volume()
                    for did in device_ids
                )
            )
            result = []
            for d, volume in zip(devices, volumes, strict=True):
                did = d.get("deviceID")
                speaker = settings.get_speaker(did)
                native_name = _clean_name(d.get("name"), d.get("hardware"))
                alias = device_manager.get_alias(did) if did else native_name
                result.append(
                    {
                        "did": did,
                        "name": native_name,
                        "alias": alias,
                        "model": d.get("hardware"),
                        "presence": d.get("presence", "unknown"),
                        "play_error": device_manager.play_errors().get(did),
                        "codec_capabilities": device_manager.codec_capabilities(did) if did else {},
                        "codec_capability_details": (
                            device_manager.codec_capability_details(did) if did else {}
                        ),
                        # The formats the app can be asked to serve, in its own
                        # words: the UI lists "未测" for the ones with no record
                        # instead of inventing names of its own.
                        "codec_formats": list(CODEC_FORMATS),
                        "codec_labels": dict(CODEC_LABELS),
                        "playing": device_manager.is_playing(did),
                        "muted": device_manager.is_muted(did),
                        "enabled": speaker.enabled if speaker else False,
                        "selected": did == settings.selected_device_id,
                        "volume": volume,
                        "eq": {
                            "enabled": speaker.eq_enabled if speaker else False,
                            "points": (
                                [[p.freq, p.gain_db] for p in speaker.eq_points] if speaker else []
                            ),
                            "preset": speaker.eq_preset if speaker else "",
                            "target": speaker.eq_target if speaker else "",
                            "night_mode": speaker.night_mode if speaker else False,
                            "loudness_comp_enabled": speaker.loudness_comp_enabled
                            if speaker
                            else False,
                            "content_profile": speaker.content_profile if speaker else "",
                            "revision": speaker.eq_revision if speaker else 0,
                            "undo_available": bool(speaker and speaker.eq_undo is not None),
                            "profiles": (
                                {
                                    k: [[p.freq, p.gain_db] for p in v]
                                    for k, v in speaker.eq_profiles.items()
                                }
                                if speaker
                                else {}
                            ),
                        },
                    }
                )
            return result
        except XiaomiAuthError as e:
            raise HTTPException(status_code=401, detail=str(e)) from e
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.get("/eq/presets")
    async def get_eq_presets():
        return {
            "presets": {k: [[f, g] for f, g in v] for k, v in EQ_PRESET_POINTS.items()},
            "targets": {k: [[f, g] for f, g in v] for k, v in TARGET_CURVES.items()},
            "saved": settings.list_saved_curves(),
            "freq_range": list(CURVE_FREQ_RANGE),
            "gain_range": list(CURVE_GAIN_RANGE),
        }

    @router.post("/eq")
    async def set_eq(payload: dict):
        """Deprecated 10-band API kept for old clients; bands become points."""
        did = payload.get("did")
        enabled = payload.get("enabled")
        bands = payload.get("bands")
        if not did or not isinstance(enabled, bool) or not isinstance(bands, list):
            raise HTTPException(status_code=400, detail="did, enabled and bands required")
        try:
            async def apply_audio() -> None:
                if bridge:
                    await bridge.apply_config_change()

            speaker = await apply_config_transaction(
                lambda: settings.set_speaker_eq(
                    did, enabled=enabled, bands=bands, preset=str(payload.get("preset", ""))
                ),
                apply_audio,
            )
        except (TypeError, ValueError) as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        # EQ changes which stream a speaker pulls (split by EQ signature), so
        # pipelines are rebuilt and playing speakers re-pointed, not just the
        # encoder restarted. Rapid slider drags are debounced by the UI.
        return {
            "did": did,
            "eq": {
                "enabled": speaker.eq_enabled,
                "points": [[p.freq, p.gain_db] for p in speaker.eq_points],
                "preset": speaker.eq_preset,
                "target": speaker.eq_target,
                "night_mode": speaker.night_mode,
                "loudness_comp_enabled": speaker.loudness_comp_enabled,
                "content_profile": speaker.content_profile,
                "profiles": {
                    k: [[p.freq, p.gain_db] for p in v] for k, v in speaker.eq_profiles.items()
                },
            },
        }

    @router.post("/select")
    async def select_device(payload: dict):
        did = payload.get("did")
        if did is None:
            raise HTTPException(status_code=400, detail="did required")

        def mutate_select():
            if not device_manager.select_device(did):
                raise LookupError(did)

        async def apply_single_mode() -> None:
            if bridge and settings.receiver_mode == "single":
                await bridge.restart()

        try:
            await apply_config_transaction(mutate_select, apply_single_mode)
        except LookupError:
            raise HTTPException(status_code=404, detail="device not found") from None
        return {"selected": did}

    @router.post("/alias")
    async def set_alias(payload: dict):
        did = payload.get("did")
        alias = str(payload.get("alias", "")).strip()
        if not did or not alias:
            raise HTTPException(status_code=400, detail="did and alias required")

        async def apply_alias() -> None:
            if bridge:
                await bridge.apply_config_change()

        await apply_config_transaction(
            lambda: device_manager.set_alias(did, alias), apply_alias
        )
        return {"did": did, "alias": alias}

    @router.post("/enabled")
    async def set_enabled(payload: dict):
        did = payload.get("did")
        enabled = payload.get("enabled")
        if not did or not isinstance(enabled, bool):
            raise HTTPException(status_code=400, detail="did and enabled boolean required")

        async def apply_enabled() -> None:
            if bridge:
                await bridge.apply_config_change()

        await apply_config_transaction(
            lambda: device_manager.set_enabled(did, enabled), apply_enabled
        )
        return {"did": did, "enabled": enabled}

    return router


async def _no_volume() -> None:
    return None
