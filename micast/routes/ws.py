"""Realtime state push over WebSocket — replaces UI polling when connected.

Pushes status and topology every 2s, with metadata changes delivered within
0.5s. Cloud playback remains at 6s. The client falls back to plain polling
if the socket drops.
"""

import asyncio
import contextlib
import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from micast.access import COOKIE_NAME, AccessManager
from micast.config import settings
from micast.routes.playback import build_playback_state
from micast.topology import build_topology

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ws"])


def install(bridge, device_manager, access_manager: AccessManager | None = None) -> APIRouter:
    @router.websocket("/api/ws")
    async def state_ws(websocket: WebSocket):
        if (
            access_manager
            and access_manager.auth_enabled
            and not access_manager.valid_session(websocket.cookies.get(COOKIE_NAME))
        ):
            await websocket.close(code=4401)
            return
        await websocket.accept()
        try:
            status_json = ""
            metadata_json = ""
            playback_json = ""
            tuning_json = ""
            config_revision = -1
            ticks = 0
            while True:
                # Fast metadata checks are local only. Ordinary telemetry and
                # expensive topology/cloud projections retain their cadence.
                metadata = json.dumps(bridge._now_playing, ensure_ascii=False)
                if ticks % 4 == 0 or metadata != metadata_json:
                    metadata_json = metadata
                    snapshot = bridge.status
                    status = json.dumps(snapshot, ensure_ascii=False)
                    if status != status_json:
                        status_json = status
                        await websocket.send_text(
                            json.dumps({"type": "status", "data": snapshot}, ensure_ascii=False)
                        )
                if ticks % 4 == 0:
                    await websocket.send_json(
                        {"type": "topology", "data": build_topology(bridge, device_manager)}
                    )
                    await websocket.send_json({"type": "heartbeat"})
                if ticks % 12 == 0:  # cloud playback stays at 6s
                    playback = await build_playback_state(device_manager)
                    payload = json.dumps(playback, ensure_ascii=False)
                    if payload != playback_json:
                        playback_json = payload
                        await websocket.send_text(
                            json.dumps({"type": "playback", "data": playback}, ensure_ascii=False)
                        )
                tuning = {speaker.did: speaker.eq_revision for speaker in settings.speakers}
                payload = json.dumps(tuning, ensure_ascii=False, sort_keys=True)
                if payload != tuning_json:
                    tuning_json = payload
                    await websocket.send_text(
                        json.dumps({"type": "tuning", "data": tuning}, ensure_ascii=False)
                    )
                if settings.config_revision != config_revision:
                    config_revision = settings.config_revision
                    await websocket.send_text(
                        json.dumps({"type": "config", "revision": config_revision})
                    )
                ticks += 1
                await asyncio.sleep(0.5)
        except WebSocketDisconnect:
            pass
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.debug("WS client dropped", exc_info=True)
        finally:
            with contextlib.suppress(Exception):
                await websocket.close()

    return router
