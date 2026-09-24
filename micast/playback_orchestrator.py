"""Playback orchestration between the AirPlay bridge and the Xiaomi cloud.

Owns the session-lifecycle glue that used to live as closures inside
``micast.main.lifespan``: starting/stopping speakers on session events,
group membership reconciliation, anchor pause/resume, group stream
recovery after unexpected HTTP disconnects, linked-volume forwarding,
default-volume application, lyrics sessions and audio-restart re-points.

The orchestrator never creates background tasks itself; the caller injects
a ``start_background`` callback that retains task ownership (lifetime and
exception reporting stay in main's lifespan).
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from micast.audio_bridge import AudioBridge
from micast.config import settings
from micast.lyrics import LyricsSession
from micast.xiaomi.device_manager import DeviceManager

logger = logging.getLogger(__name__)

# Grace period before a torn-down AirPlay session pauses its speakers;
# reconnects within this window are transparent to the speakers.
SESSION_STOP_GRACE_SECONDS = 3.0

# Upper bound for hook awaits that issue cloud commands while the bridge
# holds its restart lock — a hanging cloud call must not wedge every later
# config apply behind the lock.
HOOK_TIMEOUT_SECONDS = 5.0


def stream_url_for_receiver(receiver_id: str, device_id: str) -> str:
    """Stream URL of one receiver for one speaker, channel/EQ suffix included."""
    return (
        f"http://{settings.effective_stream_host}:{settings.stream_port}"
        f"/stream/{receiver_id}{settings.stream_suffix(receiver_id, device_id)}"
    )


class PlaybackOrchestrator:
    """Wires bridge session hooks to DeviceManager cloud commands."""

    def __init__(
        self,
        bridge: AudioBridge,
        device_manager: DeviceManager,
        start_background: Callable[[Awaitable, str], asyncio.Task],
    ):
        self.bridge = bridge
        self.device_manager = device_manager
        self._start_background = start_background
        self._pending_stops: dict[str, asyncio.Task] = {}
        self._pending_group_recoveries: dict[str, asyncio.Task] = {}
        self._lyrics_sessions: dict[str, LyricsSession] = {}
        # Receivers whose session start this orchestrator already served. The
        # AirPlay 2 receiver (shairport-sync) fires play-begins again on track
        # gaps and underruns while the session never ended — replaying the
        # cloud play in that case only makes the Xiaomi player reload the URL.
        self._started_sessions: set[str] = set()
        self._session_locks: dict[str, asyncio.Lock] = {}

    def attach(self) -> None:
        """Install every bridge/device-manager hook this orchestrator serves."""
        self.bridge.on_group_membership_changed = self.reconcile_group
        self.device_manager.on_anchor_offline = self.pause_group_for_anchor
        self.device_manager.on_anchor_recovered = self.resume_group_for_anchor
        self.bridge.stream_server.on_client_disconnected = self.recover_group_streams
        self.bridge.on_session_start = self.on_session_start
        self.bridge.on_session_stop = self.on_session_stop
        self.bridge.on_local_stream = self.play_receiver
        self.bridge.on_receiver_volume = self.on_receiver_volume
        self.bridge.on_volume_session_start = self.apply_default_volume
        self.bridge.on_audio_restarted = self.on_audio_restarted
        self.bridge.on_airplay2_retarget = self.release_retargeted_speaker

    async def stop_all(self) -> None:
        """Tear down lyrics sessions (lifespan shutdown)."""
        await asyncio.gather(
            *(session.stop() for session in list(self._lyrics_sessions.values())),
            return_exceptions=True,
        )
        self._lyrics_sessions.clear()

    async def resend_with_audio_id(self, receiver_id: str, audio_id: str) -> None:
        """Re-issue play with a library audioID so touch-screen speakers swap
        the default cover for real cover art + scrolling lyrics."""
        self.bridge.lyrics_matched[receiver_id] = audio_id
        for did in self.device_manager.playing_ids():
            if self.device_manager.owner_of(did) != receiver_id:
                continue
            url = self.device_manager.stream_url_of(did)
            if url:
                await self.device_manager.play_stream(
                    did, url, owner=receiver_id, force=True, audio_id=audio_id
                )

    async def start_lyrics_session(self, receiver_id: str) -> None:
        if not settings.touchscreen_lyrics:
            return
        server = self.bridge.local_server(receiver_id)
        if server is None:
            return
        old = self._lyrics_sessions.pop(receiver_id, None)
        if old:
            await old.stop()
        session = LyricsSession(
            receiver_id,
            server,
            self.device_manager,
            lambda audio_id: self.resend_with_audio_id(receiver_id, audio_id),
        )
        self._lyrics_sessions[receiver_id] = session
        session.start()

    async def apply_default_volume(self, receiver_id: str) -> None:
        if not settings.default_volume_enabled:
            return
        if self.bridge._volume_modes.get(receiver_id) == "linked":
            return  # The sender owns volume in linked mode; do not race it.
        await asyncio.sleep(0.5)  # let the play command land first
        for did in self.device_manager.owned_targets(receiver_id, receiver_id):
            try:
                await self.device_manager.set_volume(did, settings.default_volume)
            except Exception:
                logger.debug("默认音量设置失败 %s", did, exc_info=True)

    async def play_receiver(self, receiver_id: str, url: str, steal: bool = True):
        # Any playback (re)start cancels a pending stop from a recent teardown:
        # the stream outlives individual sessions, so quick reconnects are free.
        pending = self._pending_stops.pop(receiver_id, None)
        if pending:
            pending.cancel()
        targets = settings.receiver_targets(receiver_id)
        if not targets:
            logger.warning("Receiver %s has no playback target", receiver_id)
            return
        if not steal:
            # Resume blips only re-assert targets this receiver still owns;
            # never rip a speaker away from another active receiver.
            owned = [
                did for did in targets if self.device_manager.owner_of(did) in (None, receiver_id)
            ]
            skipped = [did for did in targets if did not in owned]
            if skipped:
                logger.info(
                    "Receiver %s resumed; skipping %s (owned by another receiver)",
                    receiver_id,
                    skipped,
                )
            targets = owned
            if not targets:
                return
        # Delay now lives in the stream server's per-client buffer (keyed by
        # ?sink=): session start fires every target at once and the sink buffer
        # holds each speaker back by its own normalized offset. No start-time
        # stagger, and a member can also be pulled earlier in place.

        # One offline/unreachable speaker must not break the session-start
        # chain for the others — gather everything, then record the failures
        # so the watchdog can retry them and the UI can show them.
        attempted: dict[str, str] = {}

        async def play_target(did: str):
            # Stereo/EQ splits give each speaker its own stream suffix.
            # The suffix belongs to the path; the cache-buster query goes last.
            base = url + settings.stream_suffix(receiver_id, did)
            play_url = f"{base}/for/{receiver_id}/{did}?s={time.time_ns()}"
            attempted[did] = play_url
            await self.device_manager.play_stream(did, play_url, owner=receiver_id)

        results = await asyncio.gather(
            *(play_target(did) for did in targets), return_exceptions=True
        )
        for did, result in zip(targets, results, strict=True):
            if isinstance(result, Exception):
                logger.warning(
                    "Speaker %s failed to start for %s: %s", did, receiver_id, result
                )
                self.device_manager.note_play_error(
                    did, receiver_id, str(result), attempted.get(did)
                )
            else:
                self.device_manager.clear_play_error(did)

        # The cloud often acknowledges a URL even when the speaker's decoder
        # rejects its media type. Verify the real HTTP pull after startup and
        # learn this device's support for the format actually in use.
        self._start_background(
            self.verify_receiver_streams(receiver_id, targets, attempted),
            f"verify-codec:{receiver_id}",
        )

    async def verify_receiver_streams(
        self,
        receiver_id: str,
        targets: list[str],
        attempted: dict[str, str] | None = None,
    ) -> None:
        fmt = "PCM/WAV" if not settings.audio.auto_transcode else settings.audio.format.upper()
        pending = {
            did for did in targets if self.device_manager.owner_of(did) == receiver_id
        }
        # Slow speakers and a cold stream server can need a few seconds before
        # the first bytes arrive. Do not turn startup latency into a false
        # codec incompatibility.
        for _attempt in range(3):
            await asyncio.sleep(2.0)
            if not self.bridge.is_session_active(receiver_id):
                # The session ended before any pull could be observed. With no
                # live session a missing pull proves nothing about the
                # speaker's codec support — abort without recording it.
                return
            healthy_now = {did for did in pending if self._stream_active_for(did)}
            for did in healthy_now:
                self.device_manager.note_codec_capability(did, fmt, True, "stream_pull_confirmed")
            pending -= healthy_now
            if not pending:
                return
        if not self.bridge.is_session_active(receiver_id):
            return
        for did in pending:
            if self.device_manager.owner_of(did) != receiver_id:
                # Ownership moved (the phone switched AirPlay 1/2 or another
                # receiver took the speaker): whatever this probe concludes, it
                # must not touch the new owner's playback.
                continue
            current_url = self.device_manager.stream_url_of(did)
            if attempted is not None and current_url != attempted.get(did):
                # A newer play superseded the URL we probed (re-point, lyrics
                # re-send, recovery replay): the verdict below would stop a
                # playback that is no longer the one we started from.
                continue
            message = f"音箱未实际拉取 {fmt} 音频，可能不支持当前格式"
            self.device_manager.note_codec_capability(did, fmt, False, "no_stream_pull")
            url = current_url
            # A decoder that accepted the cloud command but never pulls the
            # stream leaves a live server-side queue. Unload it immediately so
            # an unsupported format cannot turn into an endless dropped-chunk
            # counter and CPU/network churn. keep_error=True: this stop is the
            # ERROR's remediation, not a cleanup that clears it — capture the
            # URL first because the stop forgets it.
            await self.device_manager.stop_playback(did, owner=receiver_id, keep_error=True)
            self.device_manager.note_play_error(did, receiver_id, message, url, retry=False)
            logger.warning("Speaker %s did not pull %s for receiver %s", did, fmt, receiver_id)

    def _stream_active_for(self, device_id: str) -> bool:
        """Watchdog ground truth: is the speaker really pulling its stream right
        now? The cloud reports "playing" even when the speaker fetches nothing."""
        # Imported here to avoid a hard cycle: main imports this module.
        from micast.main import _stream_active_for

        return _stream_active_for(device_id)

    def _session_lock(self, receiver_id: str) -> asyncio.Lock:
        lock = self._session_locks.get(receiver_id)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[receiver_id] = lock
        return lock

    async def on_session_start(self, receiver_id: str):
        # Serialize start/stop pairs per receiver: a RECORD and a shairport
        # play-begins callback can interleave and each would otherwise issue
        # its own full round of cloud plays.
        async with self._session_lock(receiver_id):
            # Skip only when the session never ended (duplicate play-begins).
            # After an engine restart the bridge's active set is cleared, so a
            # genuinely fresh start always replays even if our latch survived.
            if (
                receiver_id in self._started_sessions
                and self.bridge.is_session_active(receiver_id)
            ):
                logger.debug(
                    "Receiver %s session start already served; skipping replay",
                    receiver_id,
                )
                return
            self._started_sessions.add(receiver_id)
        url = f"http://{settings.effective_stream_host}:{settings.stream_port}/stream/{receiver_id}"
        await self.play_receiver(receiver_id, url)
        await self.start_lyrics_session(receiver_id)
        self._start_background(
            self.apply_default_volume(receiver_id), f"default-volume:{receiver_id}"
        )

    async def on_session_stop(self, receiver_id: str):
        async with self._session_lock(receiver_id):
            self._started_sessions.discard(receiver_id)
            self.bridge._sender_volumes.pop(receiver_id, None)
            self.bridge._volume_modes.pop(receiver_id, None)
            lyrics = self._lyrics_sessions.pop(receiver_id, None)
            if lyrics:
                await lyrics.stop()
            self.bridge.lyrics_matched.pop(receiver_id, None)

        async def delayed_stop():
            try:
                await asyncio.sleep(SESSION_STOP_GRACE_SECONDS)
                results = await asyncio.gather(
                    *(
                        self.device_manager.stop_playback(did, owner=receiver_id)
                        for did in settings.receiver_targets(receiver_id)
                    ),
                    return_exceptions=True,
                )
                for did, result in zip(
                    settings.receiver_targets(receiver_id), results, strict=True
                ):
                    if isinstance(result, Exception):
                        logger.warning("Speaker %s cleanup failed: %s", did, result)
                # Give paused speakers a clean EOF: otherwise they hold the
                # HTTP connection open forever, silently waiting for data.
                self.bridge.drop_stream_clients(receiver_id)
                # External AirPlay targets get the same grace as the speakers:
                # a reconnect within the window never tore them down.
                await self.bridge.stop_airplay_targets(receiver_id)
                await self.bridge.stop_dlna_targets(receiver_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Receiver cleanup failed for %s", receiver_id)
            finally:
                if self._pending_stops.get(receiver_id) is asyncio.current_task():
                    self._pending_stops.pop(receiver_id, None)

        pending = self._pending_stops.pop(receiver_id, None)
        if pending:
            pending.cancel()
        self._pending_stops[receiver_id] = self._start_background(
            delayed_stop(), f"delayed-stop:{receiver_id}"
        )

    async def reconcile_group(self, group_id: str, removed_dids: list[str]):
        """Live membership edit on a group: stop the removed speakers and
        incrementally play the added ones. Pipelines are untouched — every
        needed stream variant already exists (the route rebuilds otherwise).
        steal=False means only unowned or already-owned speakers are touched,
        and same-URL replays are skipped, so playing speakers are undisturbed.
        """
        for receiver in settings.active_receivers():
            if receiver.target_type != "group" or receiver.target_id != group_id:
                continue
            owns_session = any(
                self.device_manager.owner_of(did) == receiver.id
                for did in (*self.device_manager.playing_ids(), *removed_dids)
            )
            if not owns_session:
                continue  # no live session on this receiver
            url = (
                f"http://{settings.effective_stream_host}:{settings.stream_port}"
                f"/stream/{receiver.id}"
            )
            for did in removed_dids:
                await self.device_manager.stop(did, owner=receiver.id)
            await self.play_receiver(receiver.id, url, steal=False)

    async def pause_group_for_anchor(self, group_id: str) -> None:
        """Anchor went offline: pause every other member (keep their stream
        URLs so resume is cheap) and tear down network targets. The anchor
        itself is left alone so its watchdog keeps detecting recovery."""
        for receiver in settings.active_receivers():
            if receiver.target_type != "group" or receiver.target_id != group_id:
                continue
            group = settings.group_for_receiver(receiver.id)
            anchor = group.anchor_did if group else None
            for did in settings.receiver_targets(receiver.id):
                if did == anchor:
                    continue
                if self.device_manager.owner_of(did) == receiver.id:
                    await self.device_manager.stop(did, owner=receiver.id)
            await self.bridge.stop_airplay_targets(receiver.id)
            await self.bridge.stop_dlna_targets(receiver.id)

    async def resume_group_for_anchor(self, group_id: str) -> None:
        """Anchor recovered: re-assert the paused members (steal=False only
        touches speakers this receiver still owns)."""
        for receiver in settings.active_receivers():
            if receiver.target_type != "group" or receiver.target_id != group_id:
                continue
            url = (
                f"http://{settings.effective_stream_host}:{settings.stream_port}"
                f"/stream/{receiver.id}"
            )
            await self.play_receiver(receiver.id, url, steal=False)

    async def recover_group_streams(self, receiver_id: str, disconnected_sink: str) -> None:
        """Rejoin every Xiaomi member after one unexpected HTTP disconnect."""
        current = self._pending_group_recoveries.get(receiver_id)
        if current is not None and not current.done():
            return

        async def recover() -> None:
            try:
                # Ignore transient socket replacement and normal session end.
                await asyncio.sleep(0.75)
                server = self.bridge.local_server(receiver_id)
                group = settings.group_for_receiver(receiver_id)
                if server is None or server.sessions <= 0 or group is None:
                    return
                # Xiaomi players commonly open a short probe connection and
                # immediately replace it with the real pull. If the same sink
                # has reconnected during the debounce window, playback is
                # healthy and restarting the whole group creates a stutter loop.
                if self.bridge.stream_server.sink_connected(receiver_id, disconnected_sink):
                    logger.info(
                        "Grouped sink %s replaced its HTTP connection; recovery skipped",
                        disconnected_sink,
                    )
                    return
                targets = [
                    did
                    for did in group.speaker_ids
                    if self.device_manager.owner_of(did) == receiver_id
                ]
                if len(targets) < 2:
                    return
                logger.warning(
                    "Grouped sink %s disconnected from %s; resynchronizing %s",
                    disconnected_sink,
                    receiver_id,
                    targets,
                )
                self.bridge.stream_server.begin_group_recovery(receiver_id, targets)
                self.bridge.drop_stream_clients(receiver_id)
                base = (
                    f"http://{settings.effective_stream_host}:{settings.stream_port}"
                    f"/stream/{receiver_id}"
                )

                async def replay(did: str) -> None:
                    stream = base + settings.stream_suffix(receiver_id, did)
                    url = f"{stream}/for/{receiver_id}/{did}?s={time.time_ns()}"
                    await self.device_manager.play_stream(did, url, owner=receiver_id, force=True)

                results = await asyncio.gather(
                    *(replay(did) for did in targets), return_exceptions=True
                )
                failures = [result for result in results if isinstance(result, Exception)]
                if failures:
                    self.bridge.stream_server.abort_group_recovery(receiver_id)
                    logger.warning(
                        "Group stream recovery command failed for %s: %s",
                        receiver_id,
                        failures[0],
                    )
            finally:
                self._pending_group_recoveries.pop(receiver_id, None)

        self._pending_group_recoveries[receiver_id] = self._start_background(
            recover(), f"group-recovery:{receiver_id}"
        )

    async def on_receiver_volume(self, receiver_id: str, percent: int):
        if self.bridge._volume_modes.get(receiver_id) != "linked":
            return
        # Only affect speakers owned by this session, never another source.
        await asyncio.gather(
            *(
                self.device_manager.set_volume(did, percent)
                for did in self.device_manager.owned_targets(receiver_id, receiver_id)
            )
        )

    async def release_retargeted_speaker(self, device_id: str, owner: str) -> None:
        """Unload the speaker an AirPlay 2 instance was retargeted away from.

        Without this the old speaker keeps the stale URL and keeps pulling the
        instance's stream, so two speakers stream one instance side by side.
        Ownership moves only for the NEW target, so nothing else stops it.
        """
        if self.device_manager.owner_of(device_id) not in (None, owner):
            # A different receiver owns it now (e.g. classic AirPlay took the
            # speaker over) — that receiver's playback is not ours to end.
            return
        await self.device_manager.stop_playback(device_id, owner=owner)

    async def on_audio_restarted(self):
        # Encoding or topology changed under live connections; playing speakers
        # must reconnect to pick up the new codec or channel URL. Recompute the
        # URL from the speaker's current OWNER — when several receivers target
        # the same speaker (single + group), a settings-order lookup would pick
        # the wrong stream and hand the speaker silence.
        for did in self.device_manager.playing_ids():
            owner = self.device_manager.owner_of(did)
            url = stream_url_for_receiver(owner, did) if owner else None
            if not url:
                url = self.device_manager.stream_url_of(did)
            if url:
                play_url = f"{url}/for/{owner}/{did}?s={time.time_ns()}"
                await self.device_manager.play_stream(did, play_url, force=True)
