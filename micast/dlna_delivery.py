"""One downloaded source, with raw or DSP output selected by audio requirements.

A network failure never switches to handing an unverified remote URL to a speaker.
Legacy receiver delivery preferences remain readable; 'direct' means cached raw
output when no processing is required, not external-URL delivery.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from urllib.parse import urlsplit

from micast.config import settings

logger = logging.getLogger(__name__)


class DeliverySuperseded(ValueError):
    """The controller cancelled or replaced this playback request."""


class DlnaDelivery:
    START_TIMEOUT_SECONDS = 150.0

    def __init__(self, service):
        self.service = service
        self.modes: dict[str, str] = {}
        self._versions: dict[str, int] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._started: dict[str, float] = {}

    def cancel(self, receiver_id):
        self._versions[receiver_id] = self._versions.get(receiver_id, 0) + 1
        task = self._tasks.get(receiver_id)
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    def reset(self, receiver_id):
        self.modes.pop(receiver_id, None)
        self._started.pop(receiver_id, None)

    def needs_processing(self, receiver_id, state):
        receiver = self.service.receiver(receiver_id)
        return (
            getattr(receiver, "dlna_delivery", "auto") == "local"
            or state.volume_mode == "independent"
            or bool(settings.group_for_receiver(receiver_id))
            or bool(settings.receiver_airplay_targets(receiver_id))
            or bool(settings.receiver_dlna_targets(receiver_id))
            or any(
                v["eq"] or v["channel"] or v["loudness"]
                for v in settings.receiver_stream_variants(receiver_id)
            )
            # MP3 is the common denominator across Xiaomi models. Other
            # formats use the configured shared encoder rather than guessing.
            or not urlsplit(state.uri).path.lower().endswith(".mp3")
        )

    async def play(self, receiver_id, state, resume=False):
        self.cancel(receiver_id)
        task = asyncio.current_task()
        self._tasks[receiver_id] = task
        version = self._versions[receiver_id]
        identity = state.session_id

        def current():
            if self._versions[receiver_id] != version or state.session_id != identity:
                raise DeliverySuperseded("播放请求已被替换或取消")

        try:
            old = self.service.cache.owners.get(receiver_id)
            if old and (old.uri != state.uri or old.identity != identity):
                lease = self.service.sessions.current(self.service._owner(receiver_id))
                if lease:
                    await self.service.sessions.close(lease.token, "media_replaced")
            entry = await self.service.cache.prepare(receiver_id, state.uri, identity)
            current()
            matcher = getattr(self.service, "match_track_display", None)
            if matcher is not None:
                await matcher(receiver_id, state)
                current()
            position = self.service._positions.get(receiver_id, 0)
            processed = self.needs_processing(receiver_id, state) or position > 0
            self.modes[receiver_id] = "local" if processed else "proxy"
            logger.info(
                "DLNA %s output=%s (shared cache, no preflight)",
                receiver_id,
                self.modes[receiver_id],
            )
            async with asyncio.timeout(self.START_TIMEOUT_SECONDS):
                if processed:
                    if self.service.media is None:
                        raise ValueError("当前安装缺少音频处理服务")
                    path = await entry.wait()
                    current()
                    state.source_path = str(path)
                    await self.service.media.play(receiver_id, state, position, resume=resume)
                    logger.info("DLNA %s cached decode output ready", receiver_id)
                else:
                    await self._start_proxy(receiver_id, state, entry, current)
                current()
        except asyncio.CancelledError:
            current()
            raise
        except Exception:
            current()
            if self.modes.get(receiver_id) == "proxy":
                lease = self.service.sessions.current(self.service._owner(receiver_id))
                if lease is not None and lease.identity == identity:
                    await self.service.sessions.close(lease.token, "start_failed")
            raise
        finally:
            if self._tasks.get(receiver_id) is task:
                self._tasks.pop(receiver_id, None)

    async def _start_proxy(self, receiver_id, state, entry, current):
        service = self.service
        targets = settings.receiver_targets(receiver_id)
        if not targets:
            raise ValueError("Playback target has no speakers")
        owner = service._owner(receiver_id)
        lease = service.sessions.begin(owner, "dlna", state.session_id)
        # begin() announces an active lease before the output actually starts.
        state.state = "TRANSITIONING"
        url = f"http://{settings.effective_stream_host}:{settings.stream_port}/dlna-file/{entry.token}"

        async def send(did):
            result = await service.device_manager.play_stream(did, url, owner=owner, force=True)
            service._register_speakers(receiver_id, lease, [did], [result])
            return result

        results = await asyncio.gather(*(send(did) for did in targets), return_exceptions=True)
        current()
        if not any(result is True for result in results):
            await service.sessions.close(lease.token, "start_failed")
            raise ValueError("No speaker accepted the playback command")
        service._register_speakers(receiver_id, lease, targets, results)
        logger.info(
            "DLNA %s local proxy sent to %s/%s speakers",
            receiver_id,
            sum(r is True for r in results),
            len(targets),
        )
        try:
            await entry.wait()
            current()
            self._started[receiver_id] = asyncio.get_running_loop().time()
        except BaseException:
            await service.sessions.close(lease.token, "start_failed")
            raise

    async def seek(self, receiver_id, state, seconds):
        version, identity = self._versions.get(receiver_id, 0), state.session_id

        def current():
            if self._versions.get(receiver_id, 0) != version or state.session_id != identity:
                raise DeliverySuperseded("定位请求已被替换")

        if state.state != "PLAYING":
            if self.service.media is not None:
                entry = await self.service.cache.prepare(receiver_id, state.uri, state.session_id)
                path = await entry.wait()
                current()
                state.source_path = str(path)
                await self.service.media.seek_position(receiver_id, state, seconds)
                current()
            self.service._positions[receiver_id] = seconds
            return
        if self.service.media is None:
            raise ValueError("当前安装不支持定位播放")
        old_identity = state.session_id
        old_mode = self.modes.get(receiver_id)
        old_position = self.service._positions.get(receiver_id, 0)
        # Reuse the downloaded file; seeking never reopens the external URL.
        entry = self.service.cache.owners.get(receiver_id)
        if entry is None:
            raise ValueError("媒体缓存已失效")
        path = await entry.wait()
        current()
        state.source_path = str(path)
        old_token = entry.token
        state.session_id = requested_identity = uuid.uuid4().hex
        self.service.cache.rebind(entry, state.session_id)
        self.service._positions[receiver_id] = seconds
        self.modes[receiver_id] = "local"
        try:
            await self.service.media.play(receiver_id, state, seconds, resume=True)
            if (
                state.session_id != requested_identity
                or self._versions.get(receiver_id, 0) != version
            ):
                raise DeliverySuperseded("定位请求已被替换")
        except BaseException:
            if state.session_id == requested_identity and not entry.closed:
                state.session_id = old_identity
                # The previous speaker may still be using this URL when
                # decoding fails before replacing its lease. Restore that
                # exact URL, rather than minting an unusable third token.
                self.service.cache.rebind(entry, old_identity, token=old_token)
                self.service._positions[receiver_id] = old_position
                if old_mode is not None:
                    self.modes[receiver_id] = old_mode
            raise

    def position(self, receiver_id):
        if self.modes.get(receiver_id) == "local" and self.service.media is not None:
            return self.service.media.position(receiver_id) or 0
        position = self.service._positions.get(receiver_id, 0)
        state = self.service.state_for(receiver_id)
        started = self._started.get(receiver_id)
        if (
            self.modes.get(receiver_id) == "proxy"
            and started is not None
            and state.state == "PLAYING"
        ):
            position += asyncio.get_running_loop().time() - started
        return position
