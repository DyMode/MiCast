"""DLNA URI adaptation into shared sessions, DSP, output leases and streams."""

import asyncio

from micast.config import settings
from micast.media_source import MediaPCMSource
from micast.pcm_source import ReaderPCMSource
from micast.pipeline_factory import build_branches
from micast.speaker_pipeline import SpeakerPipeline

OUTPUT_CONNECT_TIMEOUT = 8.0


class MediaPlayback:
    def __init__(self, bridge, manager):
        self.bridge, self.manager = bridge, manager
        self.sources = {}
        self._locks = {}
        self._requests = {}
        self._positions = {}
        self._versions = {}
        self._preparing = {}

    async def cancel_pending(self, receiver_id):
        owner = f"dlna:{receiver_id}"
        self._versions[owner] = self._versions.get(owner, 0) + 1
        source = self._preparing.get(owner)
        if source is not None:
            await source.stop()

    async def play(self, receiver_id, state, seek_seconds=0, resume=False):
        owner = f"dlna:{receiver_id}"
        version = self._versions.get(owner, 0) + 1
        self._versions[owner] = version
        async with self._locks.setdefault(owner, asyncio.Lock()):
            if version != self._versions[owner]:
                raise ValueError("播放请求已被替换")
            previous = self.bridge.sessions.current(owner)
            previous_state = previous.state if previous else None
            source = MediaPCMSource(state.uri, seek_seconds, deferred=True)
            # Open and prime before retiring audible playback. A bad URI/seek
            # must not destroy the current source during a configuration rebuild.
            self._preparing[owner] = source
            try:
                reader = await source.start()
            finally:
                if self._preparing.get(owner) is source:
                    self._preparing.pop(owner, None)
            if version != self._versions[owner] or (previous is not None and (
                self.bridge.sessions.current(owner) is not previous
                or (
                    previous.state != previous_state
                    and previous.state.value in ("paused", "closing", "closed")
                )
            )):
                await source.stop()
                raise ValueError("播放会话已结束或已暂停")
            try:
                return await self._play(receiver_id, state, source, reader, resume)
            except BaseException:
                current = self.bridge.sessions.current(owner)
                if current is not None and current is not previous:
                    await self.bridge.sessions.close(current.token, reason="start_failed")
                await source.stop()
                if self.sources.get(owner) is source:
                    self.sources.pop(owner, None)
                    self._requests.pop(owner, None)
                raise

    async def _play(self, receiver_id, state, source, reader, resume=False):
        owner = f"dlna:{receiver_id}"
        current = self.bridge.sessions.current(owner)
        if current:
            await self.bridge.sessions.close(current.token, "media_replaced")
        lease = self.bridge.sessions.begin(owner, "dlna", state.session_id)
        self.sources[owner] = source
        self._requests[owner] = (receiver_id, state)
        variants = settings.receiver_stream_variants(receiver_id)
        wants_tap = bool(settings.receiver_airplay_targets(receiver_id))
        branches = build_branches(reader, variants, owner, 44100, wants_tap, start=False)
        tee = branches.tee
        if tee:
            self.bridge._tees[owner] = tee
        if branches.tap:
            self.bridge._target_taps[owner] = branches.tap
        group = settings.group_for_receiver(receiver_id)
        pipelines = []

        async def release():
            # This generation owns its exact objects, never a replacement's.
            for pipeline in pipelines:
                await pipeline.stop()
                if self.bridge._pipelines.get(pipeline._stream_id) is pipeline:
                    self.bridge._pipelines.pop(pipeline._stream_id, None)
            if tee:
                await tee.stop()
            await source.stop()
            self._positions[owner] = source.position
            if self.sources.get(owner) is source:
                self.sources.pop(owner, None)
                self._requests.pop(owner, None)
                self.bridge._target_taps.pop(owner, None)
                self.bridge._tees.pop(owner, None)

        self.bridge.sessions.register(lease.token, "media", release, kind="media")
        hold = max(group.delay_holds().values(), default=0) / 1000 if group else 0
        source.on_end = lambda error: self.bridge.sessions.quiet(
            lease.token,
            "media_error" if error else "media_finished",
            grace=3 + hold + settings.stream_buffer_seconds,
        )
        for index, variant in enumerate(variants):
            sid = owner + variant["suffix"]

            pipeline = SpeakerPipeline(
                receiver_id,
                receiver_id,
                ReaderPCMSource(branches.readers[index]),
                self.bridge.stream_server,
                input_sample_rate=44100,
                stream_id=sid,
                group_id=group.id if group else None,
                channel=variant["channel"],
                eq_curve=variant["eq"],
                loudness=variant.get("loudness", False),
                pace_source=False,
                session_active=lambda: self.bridge.sessions.valid(lease.token),
            )
            pipeline.set_input_volume(
                state.volume
                if state.volume_mode == "independent" and not state.muted
                else 0
                if state.muted
                else 100
            )
            pipelines.append(pipeline)
            self.bridge._pipelines[sid] = pipeline
            await pipeline.start()
        if tee:
            tee.start()
        targets = settings.receiver_targets(receiver_id)
        base = f"http://{settings.effective_stream_host}:{settings.stream_port}/stream/"
        results = await asyncio.gather(
            *(
                self.manager.play_stream(
                    did,
                    f"{base}{owner}{settings.stream_suffix(receiver_id, did)}/for/{owner}/{did}",
                    owner=owner,
                    force=True,
                    steal=not resume,
                )
                for did in targets
            ),
            return_exceptions=True,
        )
        modes = getattr(self.bridge, "_volume_modes", None)
        if modes is not None:
            modes[owner] = state.volume_mode
        if state.volume_mode == "linked" and state.volume_received and self.bridge._airplay_targets:
            await self.bridge._airplay_targets.set_volume(owner, state.volume)
        await self.bridge._start_entry_targets(owner, resume=resume)
        external = bool(
            settings.receiver_airplay_targets(receiver_id)
            or settings.receiver_dlna_targets(receiver_id)
        )
        if not external and not any(result is True for result in results):
            await self.bridge.sessions.close(lease.token, "start_failed")
            raise ValueError("No speaker accepted the playback command")
        await self._wait_output_ready(owner, lease.token)
        source.activate()
        self.set_volume(receiver_id, state)
        return results

    async def _wait_output_ready(self, owner, token):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + OUTPUT_CONNECT_TIMEOUT
        while self.bridge.sessions.valid(token):
            http_ready = any(
                self.bridge.stream_server.client_count(sid) > 0
                for sid in self.bridge.entry_stream_ids(owner)
            )
            adapter = self.bridge._airplay_targets
            targets = adapter.statuses().get(owner, {}) if adapter else {}
            rtp_ready = any(item.get("status") == "streaming" for item in targets.values())
            pending = any(item.get("status") == "connecting" for item in targets.values())
            if (http_ready or rtp_ready) and (not pending or loop.time() >= deadline):
                return
            if loop.time() >= deadline:
                raise ValueError("投送指令已发送，但音箱未建立输出连接")
            await asyncio.sleep(0.05)
        raise ValueError("播放会话已结束")

    def position(self, receiver_id):
        source = self.sources.get(f"dlna:{receiver_id}")
        return source.position if source else self._positions.get(f"dlna:{receiver_id}", 0)

    def set_position(self, receiver_id, seconds):
        self._positions[f"dlna:{receiver_id}"] = seconds

    async def seek_position(self, receiver_id, state, seconds):
        """Validate a non-playing seek without acquiring targets or starting output."""
        await self.cancel_pending(receiver_id)
        owner = f"dlna:{receiver_id}"
        version = self._versions[owner]
        identity, transport = state.session_id, state.state
        source = MediaPCMSource(state.uri, seconds, deferred=True)
        self._preparing[owner] = source
        try:
            await source.start()
            if (version != self._versions[owner] or state.session_id != identity
                    or state.state != transport):
                raise ValueError("定位请求已被替换")
            self._positions[owner] = seconds
        finally:
            if self._preparing.get(owner) is source:
                self._preparing.pop(owner, None)
            await source.stop()

    def set_volume(self, receiver_id, state):
        owner = f"dlna:{receiver_id}"
        percent = 0 if state.muted else state.volume if state.volume_mode == "independent" else 100
        for sid in self.bridge.entry_stream_ids(owner):
            pipeline = self.bridge.pipeline_for_stream(sid)
            if pipeline:
                pipeline.set_input_volume(percent)
                pipeline.set_loudness_level(state.volume)
        if self.bridge._airplay_targets:
            self.bridge._airplay_targets.set_input_volume(owner, percent)
            self.bridge._airplay_targets.set_loudness_level(owner, state.volume)

    async def pause(self, receiver_id):
        await self.cancel_pending(receiver_id)
        owner = f"dlna:{receiver_id}"
        lease = self.bridge.sessions.current(owner)
        if lease:
            self.bridge.sessions.pause(lease.token)
            await self.bridge.sessions.tick()

    async def refresh(self, owners=None):
        for owner, (receiver_id, state) in list(self._requests.items()):
            if owners is not None and owner not in owners:
                continue
            lease = self.bridge.sessions.current(owner)
            if lease and self.bridge.sessions.valid(lease.token):
                await self.play(receiver_id, state, self.position(receiver_id) or 0, resume=True)
