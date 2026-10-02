"""Single source of truth for terminating playback output."""

import asyncio

from micast.audio_bridge import AudioBridge
from micast.config import settings
from micast.playback_sessions import PlaybackSessions
from micast.xiaomi.device_manager import DeviceManager


def _base_receiver_id(receiver_id: str) -> str:
    return receiver_id.removesuffix("-L").removesuffix("-R")


async def stop_output(
    bridge: AudioBridge,
    device_manager: DeviceManager,
    receiver_id: str | None = None,
) -> dict[str, object]:
    """Stop speakers and sender/stream connections consistently."""
    base_id = _base_receiver_id(receiver_id) if receiver_id else None
    sessions = getattr(bridge, "sessions", None)
    if isinstance(sessions, PlaybackSessions):
        service = getattr(bridge, "dlna_service", None)
        if service is not None:
            ids = [base_id.removeprefix("dlna:")] if base_id else list(service.states)
            await asyncio.gather(*(service.cancel_next(rid) for rid in ids))
        targets = (
            settings.receiver_targets(base_id) if base_id
            else device_manager.playing_ids()
        )
        stream_ids = (
            bridge.entry_stream_ids(base_id) if base_id
            else bridge.stream_server.stream_ids()
        )
        kicked = sum(bridge.stream_server.client_count(sid) for sid in stream_ids)
        disconnected = sum(
            "sender" in lease["resources"] for lease in sessions.snapshot()
            if base_id is None or lease["owner"] == base_id
        )
        await sessions.close_all(base_id, reason="user_stop")
        # Manual diagnostic playback can predate a sender lease.
        await asyncio.gather(
            *(device_manager.stop_playback(did) for did in targets
              if device_manager.owner_of(did) is None),
            return_exceptions=True,
        )
        return {"disconnected": disconnected, "kicked": kicked, "stopped": targets}
    disconnected = await bridge.disconnect_sessions(base_id)
    if base_id:
        stream_ids = [base_id, f"{base_id}-L", f"{base_id}-R"]
        kicked = sum(bridge.stream_server.kick_clients(sid) for sid in stream_ids)
        targets = settings.receiver_targets(base_id)
    else:
        kicked = sum(
            bridge.stream_server.kick_clients(sid)
            for sid in bridge.stream_server.stream_ids()
        )
        targets = [
            str(device.get("deviceID"))
            for device in device_manager.get_control_targets()
            if device.get("deviceID")
            and (
                device_manager.is_playing(str(device["deviceID"]))
                or device_manager.is_paused(str(device["deviceID"]))
            )
        ]
    await asyncio.gather(
        *(device_manager.stop_playback(did) for did in dict.fromkeys(targets)),
        return_exceptions=True,
    )
    return {"disconnected": disconnected, "kicked": kicked, "stopped": targets}
