"""Audio bridge orchestrating PCM source → encoder → HTTP stream for one or more receivers."""

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable

from micast.audio_metrics import metrics
from micast.config import resolve_port, settings
from micast.deployment import airplay2_mode
from micast.local_airplay import LocalAirPlayProvider
from micast.orchestration import DesiredReceiver, OrchestratorClient
from micast.pcm_source import PCMSource, ReaderPCMSource, create_pcm_source
from micast.pcm_tee import PCMTee
from micast.receiver_manager import ReceiverManager
from micast.speaker_pipeline import SpeakerPipeline
from micast.stream_plan import PlanDiff, PlanSnapshot, compute_plan, diff_plans
from micast.stream_server import StreamServer

logger = logging.getLogger(__name__)

# Stale-client sweeper: Xiaomi speakers keep retrying a stream URL they were
# once told to play, so any teardown path we miss (canceled pending stop, a
# speaker re-pulling an old URL on its own) leaves a client attached to a
# silent stream forever — the topology then shows a permanent 滞留 edge.
STREAM_SWEEP_INTERVAL_SECONDS = 15.0
STREAM_IDLE_KICK_SECONDS = 10.0
# EQ drag edits commit one plan change per point; the settle window lets a
# burst land as ONE encoder restart. Used by wait_config_settled() (before the
# config transaction) and by the in-apply fallback debounce.
EQ_SETTLE_SECONDS = 0.6
# Hooks run while _restart_lock is held and issue cloud commands; a hanging
# call must not freeze every later config apply, so bound each hook await.
_HOOK_TIMEOUT_SECONDS = 5.0


class AudioBridge:
    """Owns the stream server, receivers, and per-receiver pipelines."""

    def __init__(self):
        self._stream_server = StreamServer()
        self._receiver_manager = ReceiverManager()
        self._local_provider = LocalAirPlayProvider()
        self._pipelines: dict[str, SpeakerPipeline] = {}
        self._airplay2_pipelines: dict[str, SpeakerPipeline] = {}
        self._airplay2_runtime: dict[str, dict] = {}
        self._airplay2_sources: dict[str, PCMSource] = {}
        self._airplay2_tees: dict[str, PCMTee] = {}
        # Last known playback target per AirPlay 2 instance: a retarget must
        # release the speaker the instance just left (it would otherwise keep
        # pulling the old stream next to the new one).
        self._airplay2_targets: dict[str, str] = {}
        # Set by attach_device_manager(): the supervisor re-issues playback
        # through it (the bridge itself never talks to speakers directly).
        self._device_manager = None
        self._supervisor = None
        self._tees: dict[str, PCMTee] = {}
        self._running = False
        self._status = "idle"
        self._error_count = 0
        self._restart_lock = asyncio.Lock()
        self._restart_requested = False
        self._audio_restart_requested = False
        self._stall_recovery_requested = False
        # Session-stop callbacks emitted while MiCast replaces a receiver are
        # maintenance events, not sender intent. Suppress their delayed speaker
        # stop so recovery never pauses the user's device.
        self._maintenance_sessions: set[str] = set()
        self._sweeper_task: asyncio.Task | None = None
        self._aux_tasks: set[asyncio.Task] = set()
        self._stale_active_since: dict[str, float] = {}
        # Receivers with a live sender session right now. Gates the pipelines'
        # PCM-stall watchdog (no session → no bytes is normal, not a stall).
        self._active_sessions: set[str] = set()
        # External AirPlay targets (created lazily once the provider's shared
        # Zeroconf exists); both are None in tests and on the airplay2 engine.
        self._airplay_discovery = None
        self._airplay_targets = None
        self._target_taps: dict[str, asyncio.StreamReader] = {}
        self._dlna_discovery = None
        self._dlna_targets = None
        self.on_session_start: Callable[[str], Awaitable[None]] | None = None
        self.on_session_stop: Callable[[str], Awaitable[None]] | None = None
        self.on_local_stream: Callable[..., Awaitable[None]] | None = None
        self.on_audio_restarted: Callable[[], Awaitable[None]] | None = None
        self.on_receiver_volume: Callable[[str, int], Awaitable[None]] | None = None
        self._volume_modes: dict[str, str] = {}
        self._sender_volumes: dict[str, int] = {}
        self.on_volume_session_start: Callable[[str], Awaitable[None]] | None = None
        # Stream-plan snapshot: the last config state the running pipelines were
        # built from. Every config mutation funnels through apply_config_change,
        # which diffs the fresh plan against this and rebuilds only what moved.
        # This baseline is IN-MEMORY ONLY: it is recomputed from settings at
        # engine start (and after any full restart), never persisted. A partial
        # apply failure keeps the failed entries' OLD fingerprints here (see
        # apply_config_change), so the next apply re-diffs exactly those
        # entries — a retry neither forgets the failure nor redoes work that
        # already landed.
        self._plan: PlanSnapshot | None = None
        self._plan_update_requested = False
        # EQ debounce state shared by wait_config_settled(): rapid commits
        # extend one deadline and only the first caller actually sleeps.
        self._settle_deadline = 0.0
        self._settle_waiting = False
        # Fired when a group's Xiaomi membership changed (group_id, removed dids);
        # main.py wires its reconcile_group closure here.
        self.on_group_membership_changed: Callable[[str, list[str]], Awaitable[None]] | None = None
        # Fired when an AirPlay 2 instance is retargeted to a different speaker
        # (old_did, instance_id): the previous speaker is still playing the old
        # URL and must be unloaded, or it keeps pulling alongside the new one.
        self.on_airplay2_retarget: Callable[[str, str], Awaitable[None]] | None = None

    @property
    def status(self) -> dict:
        return {
            "status": self._status,
            "pcm_source": (
                "AirPlay 音频" if settings.airplay_engine == "local" else settings.pcm_source
            ),
            "airplay_engine": settings.airplay_engine,
            "airplay_protocol": "classic" if settings.airplay_engine == "local" else "airplay2",
            "audio": settings.audio.model_dump(),
            "stream_url": self._default_stream_url(),
            "error_count": self._error_count,
            "receivers": self._receiver_statuses(),
            "orchestration": self._orchestration_status(),
            "airplay2_instances": list(self._airplay2_runtime.values()),
            "diagnostics": self.diagnostics,
            "now_playing": self._now_playing(),
        }

    def _now_playing(self) -> dict:
        """Per-receiver DAAP track metadata + matched library audioID.

        Lets the UI (and tests without a touch-screen speaker) see what the
        sender reported and whether the lyrics/cover chain found a match.
        """
        matched = getattr(self, "lyrics_matched", {}) or {}
        now = {}
        for receiver_id, item in self._local_provider.receivers.items():
            server = item.server
            if not server:
                continue
            meta = getattr(server, "daap_meta", None) or {}
            audio_id = matched.get(receiver_id)
            if not meta and not audio_id:
                continue
            now[receiver_id] = {
                "title": meta.get("title"),
                "artist": meta.get("artist"),
                "album": meta.get("album"),
                "audio_id": audio_id,
            }
        return now

    @property
    def diagnostics(self) -> dict:
        """Small, stable counters used to distinguish network, decode and consumer stalls."""
        raop = {}
        for receiver_id, item in self._local_provider.receivers.items():
            server = item.server
            if server:
                active_errors = server.active_transport_errors
                active_timing = getattr(server, "active_timing", {})
                raop[receiver_id] = {
                    "active_sessions": getattr(server, "recording_sessions", server.sessions),
                    "connected_sessions": server.sessions,
                    "total_sessions": server.total_sessions,
                    "decode_errors": active_errors["decode_errors"],
                    "dropped_packets": active_errors["dropped_packets"],
                    "resend_requests": active_errors["resend_requests"],
                    "historical_decode_errors": server.decode_errors,
                    "historical_dropped_packets": server.dropped_packets,
                    "input_buffer_ms": server.active_input_buffer_ms,
                    "timing_requests": active_timing.get("timing_requests", server.timing_requests),
                    "timing_responses": active_timing.get(
                        "timing_responses", server.timing_responses
                    ),
                    "clients": server.active_clients,
                }
        streams = {
            receiver_id: {
                "clients": self._stream_server.client_count(receiver_id),
                "bytes_sent": self._stream_server.total_bytes_sent.get(receiver_id, 0),
                "dropped_chunks": self._stream_server.dropped_chunks.get(receiver_id, 0),
                "dropped_bytes": self._stream_server.dropped_bytes.get(receiver_id, 0),
                "dropped_ms": self._stream_server.drop_metrics(receiver_id)["estimated_ms"],
                "pipeline_drops": (
                    pipeline.drop_stats()
                    if (pipeline := self.pipeline_for_stream(receiver_id)) is not None
                    else {}
                ),
                "input": (
                    pipeline.input_stats()
                    if (pipeline := self.pipeline_for_stream(receiver_id)) is not None
                    else {}
                ),
                "pace": (
                    pipeline.pace_stats()
                    if (pipeline := self.pipeline_for_stream(receiver_id)) is not None
                    else {}
                ),
                "flowing": self._stream_server.is_flowing(receiver_id),
                "latency": self._stream_server.latency_metrics(receiver_id),
            }
            for receiver_id in self._stream_server.stream_ids()
        }
        airplay2_sessions = self._active_sessions & self._airplay2_entry_ids()
        for stream in streams.values():
            input_stats = stream.get("input") or {}
            # AirPlay 2 has no RAOP session, so it has no input_buffer_ms of its
            # own; the pipeline's own pacing is the equivalent measurement
            # (buffered_ms = fed audio leading the wall clock).
            stream["input_buffer_ms"] = int(input_stats.get("buffered_ms") or 0)
        return {
            "raop": raop,
            "streams": streams,
            "sinks": self._stream_server.sink_latency_metrics(),
            "airplay_targets": (self._airplay_targets.statuses() if self._airplay_targets else {}),
            "dlna_targets": self._dlna_targets.statuses() if self._dlna_targets else {},
            # Per-entry health: which state the arbiter put each entry in,
            # its reason, and the last recovery action it took.
            "entries": self._supervisor.snapshot() if self._supervisor else {},
            # Cumulative, cross-reconnect counters plus a rolling event log:
            # connection-scoped counters reset whenever a speaker reconnects,
            # which is exactly when a periodic stutter becomes invisible.
            "audio": metrics.snapshot(),
            # Live sender sessions, split by ingress. AirPlay 2 never opens a RAOP
            # session (shairport + PCM sources feed its pipelines), so the RAOP
            # counters alone report an idle input while an AirPlay 2 sender plays.
            "sessions": {
                "active": sorted(self._active_sessions),
                "classic": sorted(self._active_sessions - airplay2_sessions),
                "airplay2": sorted(airplay2_sessions),
            },
        }

    def _airplay2_entry_ids(self) -> set[str]:
        """Ids whose audio arrives through the AirPlay 2 ingress.

        Configured instances cover every live session; the runtime maps keep an
        entry recognised for as long as its session lives, even when the config
        entry was removed or disabled mid-play.
        """
        ids = {item.id for item in settings.airplay2_instances}
        ids.update(self._airplay2_runtime)
        ids.update(self._airplay2_sources)
        ids.update(self._airplay2_tees)
        return ids

    def _receiver_statuses(self) -> list[dict]:
        if settings.airplay_engine == "local":
            return [
                {
                    "did": item.id,
                    "name": item.name,
                    "status": item.status,
                    "stream_url": item.stream_url,
                    "detail": item.detail,
                }
                for item in self._local_provider.receivers.values()
            ]
        return [
            {
                "did": p.device_id,
                "name": p.alias,
                "status": p.status,
                "stream_url": p.stream_url,
                "detail": next(
                    (
                        receiver.detail
                        for receiver in self._receiver_manager.receivers
                        if receiver.device_id == p.device_id
                    ),
                    "",
                ),
            }
            for p in self._pipelines.values()
        ]

    def pipeline_for_stream(self, stream_id: str) -> "SpeakerPipeline | None":
        """The pipeline feeding a stream id (classic or AirPlay 2), if any."""
        return self._pipelines.get(stream_id) or self._airplay2_pipelines.get(stream_id)

    def _orchestration_status(self) -> dict:
        if settings.airplay_engine == "local":
            return {
                "configured": True,
                "status": self._status,
                "detail": "经典 AirPlay 已就绪",
            }
        return self._receiver_manager.orchestration_status

    def _default_stream_url(self) -> str:
        if settings.airplay_engine == "local":
            first = next(iter(self._local_provider.receivers.values()), None)
            return first.stream_url if first else ""
        if settings.receiver_mode == "single":
            device_id = settings.selected_device_id or "default"
            return (
                f"http://{settings.effective_stream_host}:{settings.stream_port}/stream/{device_id}"
            )
        return f"http://{settings.effective_stream_host}:{settings.stream_port}/stream"

    async def start(self) -> None:
        """Start the bridge: stream server, receivers, and pipelines."""
        if self._running:
            return
        self._running = True
        self._status = "starting"
        logger.info("Starting audio bridge")

        try:
            await self._start_engine()
            self._status = self._derive_status()
            self._plan = compute_plan(settings)
        except Exception as e:
            logger.exception("Failed to start audio bridge: %s", e)
            self._status = "error"
            self._error_count += 1
            with contextlib.suppress(Exception):
                await self._stop_engine()
            self._running = False
            raise RuntimeError("音频核心启动失败") from e

        self._sweeper_task = asyncio.create_task(self._sweep_stale_stream_clients())

    async def stop(self) -> None:
        """Stop everything."""
        if not self._running:
            return
        self._running = False
        self._status = "stopping"
        logger.info("Stopping audio bridge")

        if self._sweeper_task:
            self._sweeper_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._sweeper_task
            self._sweeper_task = None
        for task in list(self._aux_tasks):
            task.cancel()
        if self._aux_tasks:
            await asyncio.gather(*list(self._aux_tasks), return_exceptions=True)
        self._aux_tasks.clear()

        await self._stop_engine()

        self._plan = None
        self._status = "idle"

    def _spawn_aux(self, coroutine, label: str) -> asyncio.Task:
        """Track bridge-owned helper tasks until normal completion or stop."""
        task = asyncio.create_task(coroutine, name=f"bridge:{label}")
        self._aux_tasks.add(task)

        def done(finished: asyncio.Task) -> None:
            self._aux_tasks.discard(finished)
            if finished.cancelled():
                return
            error = finished.exception()
            if error:
                logger.error(
                    "Bridge helper %s failed",
                    finished.get_name(),
                    exc_info=(type(error), error, error.__traceback__),
                )

        task.add_done_callback(done)
        return task

    async def restart(self) -> None:
        """Restart the bridge after config changes; rapid calls coalesce to one."""
        self._restart_requested = True
        async with self._restart_lock:
            while self._restart_requested:
                self._restart_requested = False
                await self._restart_engine_locked()

    async def restart_stream_server(self) -> None:
        """Rebind the stream server after the preferred stream port changed.

        Re-resolves from the preferred port (sliding upward when busy) so a
        hot change never binds a stale address.
        """
        settings.stream_port = resolve_port(
            settings.preferred_port("stream_port"), "MICAST_STREAM_PORT"
        )
        await self._stream_server.stop()
        await self._stream_server.start()

    async def _restart_engine_locked(self) -> None:
        """Full engine teardown+start. Caller must hold ``_restart_lock``."""
        logger.info("Restarting audio bridge due to config change")
        await self._stop_engine()
        self._status = "restarting"
        try:
            await self._start_engine()
            self._status = self._derive_status()
            self._plan = compute_plan(settings)
        except Exception as e:
            logger.exception("Failed to restart audio bridge: %s", e)
            self._status = "error"
            self._error_count += 1
            raise RuntimeError("音频服务重新启动失败，请稍后重试") from e

    async def wait_config_settled(self) -> None:
        """EQ-settle debounce window, shared by rapid successive callers.

        Runs BEFORE the config transaction (apply_config_transaction) so the
        wait never holds the process-wide config lock: while a drag settles,
        every other config/tuning route stays responsive. Callers funnel into
        apply_config_change(debounce=False) afterwards; the in-lock recompute
        there applies the FINAL settings, so nothing this window coalesced is
        lost.
        """
        self._settle_deadline = time.monotonic() + EQ_SETTLE_SECONDS
        if self._settle_waiting:
            return  # one waiter already sleeping on behalf of everyone
        self._settle_waiting = True
        try:
            while True:
                remaining = self._settle_deadline - time.monotonic()
                if remaining <= 0:
                    return
                await asyncio.sleep(remaining)
        finally:
            self._settle_waiting = False

    async def apply_config_change(self, debounce: bool = True) -> None:
        """Single entry point for every config mutation.

        Recomputes the stream plan, diffs it against what the running pipelines
        were built from, and rebuilds only what moved (per the dispatch table in
        ``_apply_plan_diff``). Rapid successive mutations (slider drags) coalesce
        via the dirty flag: the plan is recomputed at apply time.

        The optional EQ debounce wait (debounce=True) happens OUTSIDE
        ``_restart_lock`` but INSIDE whatever lock the caller holds: tuning
        routes hold the process-wide config transaction lock across
        apply_runtime, so they must run ``wait_config_settled()`` first and
        pass debounce=False — sleeping here would freeze every config route
        for the whole drag. Rollback paths likewise pass debounce=False: the
        settle already happened (or failed) before the transaction opened.
        """
        if not self._running:
            return
        self._plan_update_requested = True
        settled = not debounce
        while True:
            new_plan = compute_plan(settings)
            diff = diff_plans(self._plan, new_plan)
            if _is_debounceable_eq_change(diff) and not settled:
                # Tuning EQ mid-playback commits one plan change per point
                # edit; each restarts the encoder and audibly gaps every
                # playing speaker. Wait a beat — lock-free — and recompute so
                # a burst of edits (or a dragging sender) lands as ONE restart.
                self._plan_update_requested = False
                await asyncio.sleep(EQ_SETTLE_SECONDS)
                settled = True
                continue
            if diff.noop:
                break
            async with self._restart_lock:
                self._plan_update_requested = False
                new_plan = compute_plan(settings)
                diff = diff_plans(self._plan, new_plan)
                if not diff.noop:
                    logger.info("Applying stream plan change: %s", _diff_summary(diff))
                failed = await self._apply_plan_diff(diff)
                if failed:
                    # Advance the plan per entry: entries that applied keep the
                    # new fingerprint, failed ones keep the old one so the next
                    # apply still sees their diff (and only theirs) to retry.
                    merged_entries = dict(new_plan["entries"])
                    old_entries = self._plan["entries"] if self._plan else {}
                    for entry_id in failed:
                        if entry_id in old_entries:
                            merged_entries[entry_id] = old_entries[entry_id]
                        else:
                            merged_entries.pop(entry_id, None)
                    new_plan = {"engine": new_plan["engine"], "entries": merged_entries}
                self._plan = new_plan
                if failed:
                    raise RuntimeError("声音设置暂未完全生效，请重试")
            settled = not debounce
            if not self._plan_update_requested:
                break

    async def _apply_plan_diff(self, diff: PlanDiff) -> set[str]:
        """Dispatch a plan diff to the narrowest rebuild path. Lock held.

        Returns the entry ids whose runtime apply failed. The caller merges
        those entries' old fingerprints into ``self._plan`` and raises, so a
        retry re-diffs only what actually failed instead of either forgetting
        the failure (stale plan) or redoing work that already landed.
        """
        if diff.full_restart_required:
            await self._restart_engine_locked()
            return set()
        if diff.classic_added_removed and settings.airplay_engine != "local":
            # The orchestrator engine has no per-entry lifecycle — restart it.
            await self._restart_engine_locked()
            return set()

        audio_hook = False
        failed_entries: set[str] = set()
        if settings.airplay_engine == "local":
            if diff.classic_added_removed:
                await self._reconcile_classic_entries_locked(diff)
                audio_hook = True
            if diff.classic_rebuild:
                await self._rebuild_classic_entries_locked(diff.classic_rebuild)
                audio_hook = True

        encoder_restart = set(diff.encoder_restart)
        # Entries already rebuilt or re-created above got fresh encoders.
        encoder_restart -= diff.classic_rebuild | diff.classic_added | diff.classic_removed
        if encoder_restart:
            logger.info("Restarting encoders for EQ/audio change: %s", sorted(encoder_restart))
            pipelines = {**self._pipelines, **self._airplay2_pipelines}
            for key, pipeline in pipelines.items():
                owner = _stream_owner(key, sorted(encoder_restart))
                if owner is None:
                    continue
                try:
                    suffix = key[len(owner) :]
                    variant = next(
                        (
                            item
                            for item in settings.receiver_stream_variants(owner)
                            if item["suffix"] == suffix
                        ),
                        None,
                    )
                    if variant is None:
                        raise RuntimeError(f"stream variant disappeared: {key}")
                    pipeline.set_audio_character(
                        eq_curve=variant["eq"], loudness=variant.get("loudness", False)
                    )
                    await pipeline.restart_encoder()
                except Exception:
                    logger.exception("Failed to restart encoder %s", key)
                    self._error_count += 1
                    failed_entries.add(owner)
            if diff.audio_only:
                # Codec/format changed under live connections; speakers must
                # reconnect to pick up the new stream. EQ-only edits keep the
                # endpoint (and any live AirPlay 2 session) untouched.
                audio_hook = True

        if diff.airplay2_added or diff.airplay2_removed:
            if settings.airplay2_enabled:
                await self._start_airplay2_pipelines()
            else:
                await self._stop_airplay2_pipelines()
        if diff.airplay2_rebuild:
            await self._rebuild_airplay2_instances_locked(diff.airplay2_rebuild)
            # A retarget can retire the exact stream endpoints playing speakers
            # are pulling (e.g. -Lq1 of the old group). The orphan GC above
            # kicked them; now re-point every playing speaker at its current
            # URL so the group comes back instead of going silent everywhere.
            audio_hook = True

        for entry_id in sorted(diff.external_airplay_changed):
            await self._reconcile_entry_airplay_targets(entry_id)
        for entry_id in sorted(diff.external_dlna_changed):
            await self._reconcile_entry_dlna_targets(entry_id)

        if diff.membership_changed and self.on_group_membership_changed:
            for group_id, removed in sorted(diff.membership_changed.items()):
                try:
                    # The hook issues cloud commands while _restart_lock is
                    # held; bound it so one hanging API call cannot wedge
                    # every later config apply behind the lock.
                    await asyncio.wait_for(
                        self.on_group_membership_changed(group_id, removed),
                        timeout=_HOOK_TIMEOUT_SECONDS,
                    )
                except Exception:
                    logger.exception("group-membership hook failed for %s", group_id)

        if audio_hook and self.on_audio_restarted:
            try:
                await asyncio.wait_for(
                    self.on_audio_restarted(), timeout=_HOOK_TIMEOUT_SECONDS
                )
            except Exception:
                logger.exception("audio-restarted hook failed")
                # The hook re-points every playing speaker, so a failure may
                # affect any entry this diff touched — keep them all un-advanced.
                failed_entries |= (
                    set(diff.encoder_restart)
                    | set(diff.classic_rebuild)
                    | set(diff.classic_added)
                    | set(diff.airplay2_rebuild)
                )

        return failed_entries

    async def _reconcile_entry_airplay_targets(self, entry_id: str) -> None:
        """Re-assert one entry's external AirPlay targets (classic or AirPlay 2).

        Reconnects targets whose delay/channel moved (delay is a connect-time
        pre-buffer) and starts/stops membership — without touching the Xiaomi
        pull path, which re-reads holds live.
        """
        if not self._airplay_targets:
            return
        target_ids = settings.receiver_airplay_targets(entry_id)
        tap = self._target_taps.get(entry_id)
        if tap is None and target_ids:
            # No PCM tap exists yet (the group had no targets when its
            # pipelines were built) — rebuild just this entry to create it.
            if entry_id in self._airplay2_runtime or entry_id in {
                item.id for item in settings.airplay2_instances
            }:
                await self._rebuild_airplay2_instances_locked({entry_id})
            elif settings.airplay_engine == "local":
                await self._rebuild_classic_entries_locked({entry_id})
            tap = self._target_taps.get(entry_id)
        if tap is None:
            return
        await self._airplay_targets.start_targets(
            entry_id,
            target_ids,
            tap,
            settings.receiver_airplay_delays(entry_id),
            settings.receiver_network_channels(entry_id),
        )

    async def _reconcile_entry_dlna_targets(self, entry_id: str) -> None:
        """Re-assert one entry's DLNA renderers (classic or AirPlay 2)."""
        if not self._dlna_targets:
            return
        await self._dlna_targets.reconcile(
            entry_id,
            settings.receiver_dlna_targets(entry_id),
            settings.receiver_network_channels(entry_id),
        )

    async def restart_audio(self) -> None:
        """Apply audio encoding changes without dropping sessions or streams.

        Only the per-receiver encoder is rebuilt; phones stay connected and
        speakers keep their HTTP connections. Playing speakers are then re-told
        to play (a new connection is required for the new codec to be picked up).

        Rapid calls (e.g. dragging a delay slider) coalesce to one restart —
        each encoder restart briefly mutes the stream, so serializing every
        tick would keep the audio torn for the whole drag.
        """
        if settings.airplay_engine != "local":
            await self.restart()
            return
        self._audio_restart_requested = True
        async with self._restart_lock:
            while self._audio_restart_requested:
                self._audio_restart_requested = False
                logger.info("Restarting audio encoders for config change")
                for pipeline in self._pipelines.values():
                    try:
                        await pipeline.restart_encoder()
                    except Exception:
                        logger.exception("Failed to restart pipeline %s", pipeline.device_id)
                        self._error_count += 1
                if self.on_audio_restarted:
                    try:
                        await asyncio.wait_for(
                            self.on_audio_restarted(), timeout=_HOOK_TIMEOUT_SECONDS
                        )
                    except Exception:
                        logger.exception("audio-restarted hook failed")

    async def reconcile_receivers(self) -> None:
        """Apply receiver definition changes without restarting unchanged local receivers."""
        if settings.airplay_engine != "local":
            await self.restart()
            return
        await self.restart()

    async def reconcile_airplay2(self) -> None:
        """Incrementally publish AirPlay 2 instances without disturbing healthy inputs."""
        async with self._restart_lock:
            if settings.airplay2_enabled:
                await self._start_airplay2_pipelines()
            else:
                await self._stop_airplay2_pipelines()

    async def rebuild_airplay2_group(self, group_id: str) -> None:
        """Rebuild AirPlay 2 pipelines mapped to a changed speaker group."""
        await self.rebuild_airplay2_groups({group_id})

    async def rebuild_airplay2_groups(self, group_ids: set[str]) -> None:
        """Rebuild mapped AirPlay 2 instances once for one or more groups."""
        affected = {
            item.id
            for item in settings.airplay2_instances
            if item.enabled and item.target_type == "group" and item.target_id in group_ids
        }
        await self._rebuild_airplay2_instances(affected)

    async def rebuild_airplay2_for_speaker(self, did: str) -> None:
        """Apply an EQ change to direct and group AirPlay 2 mappings."""
        group_ids = {group.id for group in settings.groups if did in group.speaker_ids}
        affected = {
            item.id
            for item in settings.airplay2_instances
            if item.enabled
            and (
                (item.target_type == "speaker" and item.target_id == did)
                or (item.target_type == "group" and item.target_id in group_ids)
            )
        }
        await self._rebuild_airplay2_instances(affected)

    async def _rebuild_airplay2_instances(self, affected: set[str]) -> None:
        if not settings.airplay2_enabled:
            return
        if not affected:
            return
        async with self._restart_lock:
            await self._rebuild_airplay2_instances_locked(affected)

    async def _rebuild_airplay2_instances_locked(self, affected: set[str]) -> None:
        """Stop then restart the given AirPlay 2 instances. Lock held."""
        # Capture the OLD stream ids first: a retarget can change the variant
        # plan (e.g. stereo -Lq1 → single base), and speakers still attached to
        # an endpoint whose plan no longer exists would pull silence forever.
        stale = {
            key
            for key in getattr(self, "_airplay2_pipelines", {})
            for instance_id in affected
            if key == instance_id or key.startswith(f"{instance_id}-")
        }
        # A retarget leaves the previous speaker playing the old URL: nothing
        # stops it (ownership only moves for the NEW did), so it keeps pulling
        # the old stream alongside the new one. Remember what each instance was
        # aimed at, then release the speakers that are no longer targeted.
        previous_targets = {
            instance_id: getattr(self, "_airplay2_targets", {}).get(instance_id)
            for instance_id in affected
        }
        for instance_id in sorted(affected):
            await self._stop_airplay2_pipeline(instance_id)
        await self._start_airplay2_pipelines()
        stream_server = getattr(self, "_stream_server", None)
        if stream_server is None:
            return
        active = set(getattr(self, "_pipelines", {})) | set(
            getattr(self, "_airplay2_pipelines", {})
        )
        for stream_id in stale - active:
            self._stream_server.kick_clients(stream_id)
            self._stream_server.unregister_stream(stream_id)
        await self._release_retargeted_speakers(affected, previous_targets)

    async def _release_retargeted_speakers(
        self, affected: set[str], previous_targets: dict[str, str | None]
    ) -> None:
        """Stop speakers that an AirPlay 2 instance left behind on retarget."""
        stream_server = getattr(self, "_stream_server", None)
        if stream_server is None:
            return
        for instance_id in sorted(affected):
            old_target = previous_targets.get(instance_id)
            new_target = getattr(self, "_airplay2_targets", {}).get(instance_id)
            if not old_target or old_target == new_target:
                continue
            logger.info(
                "AirPlay 2 instance %s retargeted %s -> %s; releasing the old speaker",
                instance_id,
                old_target,
                new_target or "(none)",
            )
            stream_ids = [
                key
                for key in stream_server.stream_ids()
                if key == instance_id or key.startswith(f"{instance_id}-")
            ]
            for stream_id in stream_ids:
                stream_server.kick_clients(stream_id, sink=old_target)
            hook = self.on_airplay2_retarget
            if hook is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(hook(old_target, instance_id), _HOOK_TIMEOUT_SECONDS)

    def _resolve_airplay2_targets(self) -> dict[str, str]:
        """Current playback target per AirPlay 2 instance, for retarget detection."""
        return {
            item.id: item.target_id
            for item in settings.airplay2_instances
            if item.target_id
        }

    async def stop_airplay2(self) -> None:
        """Withdraw every entry from the compose-owned orchestrator."""
        if settings.orchestrator_url and settings.orchestrator_token:
            await OrchestratorClient(
                settings.orchestrator_url, settings.orchestrator_token
            ).reconcile([])

    async def shutdown_airplay2(self) -> None:
        """Explicitly withdraw every managed AirPlay 2 instance."""
        try:
            await self.stop_airplay2()
        except Exception as exc:
            logger.exception("Failed to clear the internal AirPlay 2 orchestrator")
            raise RuntimeError("内部编排服务未能停止") from exc
        await self._stop_airplay2_pipelines()

    async def _start_pipelines(self) -> None:
        for receiver in self._receiver_manager.receivers:
            pipeline = SpeakerPipeline(
                device_id=receiver.device_id,
                alias=receiver.name,
                pcm_source=receiver.pcm_source,
                stream_server=self._stream_server,
                on_session_start=self.on_session_start,
                input_sample_rate=48000,
                pace_source=False,
                session_active=self._session_active_for(receiver.device_id),
                on_source_stall=self._recover_stalled_source,
            )
            self._pipelines[receiver.device_id] = pipeline
            if receiver.status == "error":
                pipeline._status = "error"
                self._error_count += 1
                continue
            try:
                await pipeline.start()
            except Exception:
                logger.exception("Failed to start pipeline for %s", receiver.device_id)
                self._error_count += 1

    async def _start_engine(self) -> None:
        if settings.airplay_engine == "local":
            await self._stream_server.start()
            desired = [(item.id, item.name) for item in settings.active_receivers()]
            await self._local_provider.start(
                desired,
                settings.effective_stream_host,
                self._local_session_start,
                self._local_session_stop,
                self._local_volume,
            )
            await self._ensure_airplay_discovery()
            for item in self._local_provider.receivers.values():
                if item.status != "running" or not item.server:
                    continue
                await self._create_local_pipelines(item)
            if settings.airplay2_enabled:
                await self._start_airplay2_pipelines()
            return
        await self._stream_server.start()
        await self._receiver_manager.start()
        await self._start_pipelines()

    async def _ensure_airplay_discovery(self) -> None:
        """(Re)bind LAN AirPlay discovery to the provider's shared Zeroconf."""
        if not settings.network_discovery_enabled:
            return
        from micast.airplay_discovery import AirPlayDiscovery
        from micast.airplay_targets import AirPlayTargetManager
        from micast.dlna_client import DlnaDiscovery, DlnaTargetManager

        if self._dlna_targets is None:
            self._dlna_discovery = DlnaDiscovery()
            self._dlna_targets = DlnaTargetManager(self._dlna_discovery)
            await self._dlna_discovery.start()

        zeroconf = self._local_provider.zeroconf
        if zeroconf is None:
            return
        if self._airplay_targets is None:
            self._airplay_discovery = AirPlayDiscovery(
                zeroconf, own_ids=lambda: self._local_provider.own_macs
            )
            self._airplay_targets = AirPlayTargetManager(self._airplay_discovery)
            await self._airplay_discovery.start()
        else:
            await self._airplay_discovery.rebind(zeroconf)

    async def set_network_discovery(self, enabled: bool) -> None:
        """Hot on/off for experimental LAN discovery (AirPlay mDNS + DLNA SSDP).

        Disabling stops active casts to external targets too — without
        discovery running, those sessions can't survive a network blip anyway.
        """
        if enabled:
            if self._running and settings.airplay_engine == "local":
                await self._ensure_airplay_discovery()
            return
        if self._airplay_targets:
            await self._airplay_targets.stop_all()
        if self._dlna_targets:
            await self._dlna_targets.stop_all()
        if self._airplay_discovery:
            await self._airplay_discovery.stop()
        if self._dlna_discovery:
            await self._dlna_discovery.stop()
        # Drop the registries so routes report an empty list instead of stale
        # devices discovered before the toggle flipped.
        self._airplay_targets = None
        self._airplay_discovery = None
        self._dlna_targets = None
        self._dlna_discovery = None

    @property
    def airplay_discovery(self):
        return self._airplay_discovery

    def local_server(self, receiver_id: str):
        """The RaopServer behind a local receiver (DAAP metadata source)."""
        item = self._local_provider.receivers.get(receiver_id)
        return item.server if item else None

    @property
    def airplay_target_manager(self):
        return self._airplay_targets

    @property
    def dlna_discovery(self):
        return self._dlna_discovery

    @property
    def dlna_target_manager(self):
        return self._dlna_targets

    async def reconcile_dlna_targets(self, group_id: str) -> None:
        """Apply a dlna_targets membership change to a live session."""
        if not self._dlna_targets:
            return
        for receiver in settings.active_receivers():
            if receiver.target_type != "group" or receiver.target_id != group_id:
                continue
            await self._dlna_targets.reconcile(
                receiver.id,
                settings.receiver_dlna_targets(receiver.id),
                settings.receiver_network_channels(receiver.id),
            )

    async def stop_dlna_targets(self, receiver_id: str) -> None:
        if self._dlna_targets:
            await self._dlna_targets.stop_targets(receiver_id)

    async def reconcile_airplay_targets(self, group_id: str) -> None:
        """Apply an airplay_targets membership change to live sessions without
        touching pipelines or the Xiaomi path."""
        if not self._airplay_targets:
            return
        for receiver in settings.active_receivers():
            if receiver.target_type != "group" or receiver.target_id != group_id:
                continue
            target_ids = settings.receiver_airplay_targets(receiver.id)
            tap = self._target_taps.get(receiver.id)
            if tap is None and target_ids:
                # No PCM tap exists yet (the group had no targets when its
                # pipelines were built) — rebuild this entry to create it.
                await self._rebuild_entry_for_tap(receiver.id)
                tap = self._target_taps.get(receiver.id)
            if tap is None:
                continue
            await self._airplay_targets.start_targets(
                receiver.id,
                target_ids,
                tap,
                settings.receiver_airplay_delays(receiver.id),
                settings.receiver_network_channels(receiver.id),
            )

    async def stop_airplay_targets(self, receiver_id: str) -> None:
        if self._airplay_targets:
            await self._airplay_targets.stop_targets(receiver_id)

    async def _start_airplay2_pipelines(self) -> None:
        active_instances = {item.id: item for item in settings.airplay2_instances if item.enabled}
        tracked = set(self._airplay2_runtime) | set(self._airplay2_sources)
        for instance_id in list(tracked):
            if instance_id not in active_instances:
                await self._stop_airplay2_pipeline(instance_id)
                self._airplay2_runtime.pop(instance_id, None)
        # Refresh the retarget baseline only after the pipelines exist, so a
        # failure to start keeps the old target recorded for the next attempt.
        self._airplay2_targets = self._resolve_airplay2_targets()

        desired = [
            DesiredReceiver(key=item.id, device_id=item.id, name=item.name, protocol="airplay2")
            for item in active_instances.values()
        ]
        if not desired:
            return
        if airplay2_mode() == "single":
            instance = next(iter(active_instances.values()))
            group, stereo, variants = self._airplay2_variant_plan(instance)
            desired_ids = {f"{instance.id}{v['suffix']}" for v in variants}
            existing_ids = {
                key
                for key in self._airplay2_pipelines
                if key == instance.id or key.startswith(f"{instance.id}-")
            }
            if existing_ids == desired_ids and all(
                self._airplay2_pipelines[key].status == "running" for key in existing_ids
            ):
                return
            if existing_ids:
                await self._stop_airplay2_pipeline(instance.id)
            runtime = {"id": instance.id, "status": "starting", "detail": "正在启动"}
            self._airplay2_runtime[instance.id] = runtime
            # A local (shairport) source gets the configured preferred port so
            # run-shairport scans from it instead of always starting at 7000.
            source_env: dict[str, str] = {}
            if settings.airplay2_port:
                source_env["MICAST_AIRPLAY2_PORT"] = str(settings.airplay2_port)
            source = create_pcm_source(settings.airplay2_pcm_source, env=source_env)
            self._airplay2_sources[instance.id] = source
            try:
                reader = await source.start()
            except Exception as exc:
                runtime.update(status="error", detail=str(exc))
                logger.exception("Single AirPlay 2 PCM source failed")
                return
            await self._start_airplay2_variant_pipelines(
                instance,
                group,
                stereo,
                variants,
                reader,
                input_sample_rate=None,
                pace_source=True,
                input_volume=None,
                runtime=runtime,
            )
            if runtime["status"] == "starting":
                runtime.update(status="running", detail="运行正常")
            return
        try:
            actual = await OrchestratorClient(
                settings.orchestrator_url, settings.orchestrator_token
            ).reconcile(desired)
        except Exception as exc:
            logger.exception("Internal AirPlay 2 orchestrator reconcile failed")
            for item in active_instances.values():
                pipeline = self._airplay2_pipelines.get(item.id)
                self._airplay2_runtime[item.id] = (
                    {"id": item.id, "status": "running", "detail": ""}
                    if pipeline and pipeline.status == "running"
                    else {"id": item.id, "status": "error", "detail": str(exc)}
                )
            return
        for result in actual:
            runtime = {"id": result.device_id, "status": result.status, "detail": result.error}
            self._airplay2_runtime[result.device_id] = runtime
            if result.status != "running" or not result.pcm_host:
                continue
            instance = active_instances.get(result.device_id)
            if not instance:
                continue
            group, stereo, variants = self._airplay2_variant_plan(instance)
            desired_ids = {f"{instance.id}{v['suffix']}" for v in variants}
            existing_ids = {
                key
                for key in self._airplay2_pipelines
                if key == instance.id or key.startswith(f"{instance.id}-")
            }
            if existing_ids == desired_ids and all(
                self._airplay2_pipelines[key].status == "running" for key in existing_ids
            ):
                continue
            if existing_ids:
                await self._stop_airplay2_pipeline(instance.id)

            try:
                source = create_pcm_source(f"tcp:{result.pcm_host}:{result.pcm_port}")
                source_reader = await source.start()
            except Exception as exc:
                logger.exception("AirPlay 2 PCM source connect failed for %s", instance.id)
                runtime.update(status="error", detail=str(exc))
                continue
            self._airplay2_sources[instance.id] = source

            # Fail quiet until a validated receiver callback supplies volume.
            known_volume = self._sender_volumes.get(instance.id)
            input_volume = (
                100
                if known_volume is not None and self._volume_modes.get(instance.id) == "linked"
                else known_volume or 0
            )
            await self._start_airplay2_variant_pipelines(
                instance,
                group,
                stereo,
                variants,
                source_reader,
                input_sample_rate=48000,
                pace_source=False,
                input_volume=input_volume,
                runtime=runtime,
            )

    def _airplay2_variant_plan(self, instance) -> tuple:
        """Resolve an instance's (group, stereo, stream variants) plan.

        Stereo groups get one channel-split stream per side; anything else
        collapses to the base (and EQ) variants — same rule local classic
        receivers follow.
        """
        group = settings.group_for_receiver(instance.id)
        variants = settings.receiver_stream_variants(instance.id)
        # Stereo mode needs at least one channel-split stream.
        stereo = bool(group and group.mode == "stereo" and any(v["base"] for v in variants))
        if not stereo:
            variants = [v for v in variants if v["base"] == ""] or [
                {"suffix": "", "base": "", "channel": None, "eq": None}
            ]
        return group, stereo, variants

    async def _start_airplay2_variant_pipelines(
        self,
        instance,
        group,
        stereo: bool,
        variants: list[dict],
        reader,
        *,
        input_sample_rate: int | None,
        pace_source: bool,
        input_volume: int | None,
        runtime: dict,
    ) -> None:
        """Fan one AirPlay 2 instance's PCM reader out into its stream variants.

        Shared by single and orchestrated deployments so a stereo-group target
        behaves identically in both: the reader is teed and each channel/EQ
        pipeline pans or shapes its side.
        """
        readers = [reader]
        # External AirPlay targets get one extra tee output carrying the
        # un-EQ'd base mix, exactly like the classic path's tap.
        wants_tap = bool(settings.receiver_airplay_targets(instance.id))
        if len(variants) > 1 or wants_tap:
            tee = PCMTee(reader, outputs=len(variants) + (1 if wants_tap else 0))
            tee.start()
            self._airplay2_tees[instance.id] = tee
            readers = list(tee.outputs)
            for index, variant in enumerate(variants):
                readers[index].name = f"{instance.id}{variant['suffix']}"
        if wants_tap:
            self._target_taps[instance.id] = readers[-1]
            readers = readers[:-1]
        else:
            self._target_taps.pop(instance.id, None)
        for index, variant in enumerate(variants):
            stream_id = f"{instance.id}{variant['suffix']}"
            label = {"left": "左声道", "right": "右声道"}.get(variant["channel"])
            alias = f"{instance.name} ({label})" if label else instance.name
            if variant["eq"]:
                alias = f"{alias} · EQ"
            pipeline = SpeakerPipeline(
                device_id=instance.id,
                alias=alias,
                pcm_source=ReaderPCMSource(readers[index]),
                stream_server=self._stream_server,
                on_session_start=self.on_session_start,
                stream_id=stream_id,
                group_id=group.id if stereo else None,
                channel=variant["channel"] if stereo else None,
                eq_curve=variant["eq"],
                loudness=variant.get("loudness", False),
                input_sample_rate=input_sample_rate,
                pace_source=pace_source,
                session_active=self._session_active_for(instance.id),
                on_source_stall=self._recover_stalled_source,
            )
            self._airplay2_pipelines[stream_id] = pipeline
            if input_volume is not None:
                pipeline.set_input_volume(input_volume)
            pipeline.set_loudness_level(self._sender_volumes.get(instance.id, 100))
            try:
                await pipeline.start()
            except Exception as exc:
                logger.exception("AirPlay 2 pipeline %s failed", stream_id)
                runtime.update(status="error", detail=str(exc))

    async def _create_local_pipelines(self, item) -> None:
        """Create the pipeline(s) for one running local receiver.

        One variant per (stereo channel, EQ signature): speakers with the same
        EQ share a stream; a speaker with its own EQ gets a split stream
        (``{rid}-q1`` / ``{rid}-Lq1`` …) fed by a tee of the receiver's PCM.
        """
        group = settings.group_for_receiver(item.id)
        variants = settings.receiver_stream_variants(item.id)
        # Stereo mode needs at least one channel-split stream — from speakers
        # holding channels and/or network devices with a channel assignment
        # (a group of only network devices is stereo-capable too).
        stereo = bool(group and group.mode == "stereo" and any(v["base"] for v in variants))
        if not stereo:
            # Channel split is a stereo-group feature; ignore stray channel
            # assignments and collapse to the EQ variants of the base stream.
            variants = [v for v in variants if v["base"] == ""] or [
                {"suffix": "", "base": "", "channel": None, "eq": None}
            ]

        readers = [item.server.pcm_reader]
        # External AirPlay targets get one extra tee output carrying the
        # un-EQ'd base mix (EQ is per Xiaomi speaker and stays on their streams).
        wants_tap = bool(settings.receiver_airplay_targets(item.id))
        if len(variants) > 1 or wants_tap:
            tee = PCMTee(item.server.pcm_reader, outputs=len(variants) + (1 if wants_tap else 0))
            tee.start()
            self._tees[item.id] = tee
            readers = tee.outputs
            for index, variant in enumerate(variants):
                readers[index].name = f"{item.id}{variant['suffix']}"
        if wants_tap:
            self._target_taps[item.id] = readers[-1]
            readers = readers[:-1]
        else:
            self._target_taps.pop(item.id, None)

        base_url = (
            f"http://{settings.effective_stream_host}:{settings.stream_port}/stream/{item.id}"
        )
        for index, variant in enumerate(variants):
            stream_id = f"{item.id}{variant['suffix']}"
            label = {"left": "左声道", "right": "右声道"}.get(variant["channel"])
            alias = f"{item.name} ({label})" if label else item.name
            if variant["eq"]:
                alias = f"{alias} · EQ"
            pipeline = SpeakerPipeline(
                device_id=item.id,
                alias=alias,
                pcm_source=ReaderPCMSource(readers[index]),
                stream_server=self._stream_server,
                input_sample_rate=44100,
                stream_id=stream_id,
                group_id=group.id if stereo else None,
                channel=variant["channel"] if stereo else None,
                eq_curve=variant["eq"],
                loudness=variant.get("loudness", False),
                session_active=self._session_active_for(item.id),
                on_source_stall=self._recover_stalled_source,
            )
            self._pipelines[stream_id] = pipeline
            pipeline.set_input_volume(
                100
                if self._volume_modes.get(item.id) == "linked"
                else self._sender_volumes.get(item.id, 100)
            )
            pipeline.set_loudness_level(self._sender_volumes.get(item.id, 100))
            try:
                await pipeline.start()
            except Exception as exc:
                item.status = "error"
                item.detail = str(exc)
        item.stream_url = base_url
        if len(variants) > 1:
            logger.info(
                "Receiver %s: %d stream variants (%s)",
                item.id,
                len(variants),
                ", ".join(v["suffix"] or "(base)" for v in variants),
            )

    async def rebuild_pipelines(self) -> None:
        """Rebuild pipelines after a topology change (mirror ↔ stereo).

        Unlike a full restart, RAOP servers and phone sessions stay up, and
        stream endpoints are kept registered so connected speakers do not get
        disconnected mid-play. Playing speakers are re-told their (possibly
        new per-channel) stream URL via the audio-restarted hook.
        """
        if settings.airplay_engine != "local":
            await self.restart()
            return
        async with self._restart_lock:
            await self._rebuild_pipelines_locked()
        if self.on_audio_restarted:
            try:
                await asyncio.wait_for(self.on_audio_restarted(), timeout=_HOOK_TIMEOUT_SECONDS)
            except Exception:
                logger.exception("audio-restarted hook failed")

    async def _rebuild_pipelines_locked(self) -> None:
        """Rebuild all local pipelines, keeping streams registered. Lock held."""
        logger.info("Rebuilding pipelines for topology change")
        await self._stop_pipelines(keep_streams=True)
        for item in self._local_provider.receivers.values():
            if item.status != "running" or not item.server:
                continue
            try:
                await self._create_local_pipelines(item)
            except Exception:
                logger.exception("Failed to rebuild pipeline for %s", item.id)
                self._error_count += 1
        # Drop endpoints that no longer exist (e.g. -L/-R after stereo→mirror).
        self._gc_dead_streams()
        self._plan = compute_plan(settings)

    @staticmethod
    def _pipeline_needs_rebuild(pipeline) -> bool:
        """True when a pipeline cannot serve the next session as-is.

        A pipeline whose PCM reader finished reports either running == False or
        a non-running status. Test doubles expose Mock attributes instead of
        real ones, so both checks demand a real bool / str before acting.
        """
        if getattr(pipeline, "running", True) is False:
            return True
        status = getattr(pipeline, "status", "running")
        return isinstance(status, str) and status != "running"

    async def _local_session_start(self, receiver_id: str, resume: bool = False) -> None:
        self._active_sessions.add(receiver_id)
        # A pipeline whose encoder exited cleanly (its PCM reader hit EOF when
        # the previous session tore down) cannot be revived by start(): it would
        # reuse the finished reader and exit again — that is the 3s stop/start
        # churn seen in the field. Rebuild the entry instead, which gives it a
        # fresh reader.
        stale = [
            key
            for key, pipeline in self._pipelines.items()
            if (key == receiver_id or key.startswith(f"{receiver_id}-"))
            and self._pipeline_needs_rebuild(pipeline)
        ]
        if stale:
            async with self._restart_lock:
                await self._rebuild_classic_entries_locked({receiver_id})
        if not resume:
            self._volume_modes[receiver_id] = settings.sender_volume_mode
            if settings.sender_volume_mode == "independent" and self._airplay_targets:
                self._airplay_targets.independent_volume(receiver_id)
        has_pipeline = any(
            key == receiver_id or key.startswith(f"{receiver_id}-") for key in self._pipelines
        )
        if has_pipeline and self.on_local_stream:
            # No cache-buster here: the channel suffix (-L/-R) is appended by
            # the caller and must stay inside the path, before any query.
            url = (
                f"http://{settings.effective_stream_host}:{settings.stream_port}"
                f"/stream/{receiver_id}"
            )
            # A resume after a network blip re-asserts only targets this
            # receiver still owns; a fresh session may steal from others.
            await self.on_local_stream(receiver_id, url, steal=not resume)
            if receiver_id in self._sender_volumes:
                await self._local_volume(receiver_id, self._sender_volumes[receiver_id])
            if not resume and self.on_volume_session_start:
                self._spawn_aux(
                    self.on_volume_session_start(receiver_id),
                    f"volume-session:{receiver_id}",
                )
        tap = self._target_taps.get(receiver_id)
        if tap is None and self._airplay_targets and settings.receiver_airplay_targets(receiver_id):
            # A session on an entry whose pipelines predate the target list —
            # build the missing tap on the spot instead of dropping the target.
            await self._rebuild_entry_for_tap(receiver_id)
            tap = self._target_taps.get(receiver_id)
        await self._start_entry_targets(receiver_id, resume=resume)
        if receiver_id in self._sender_volumes:
            await self._local_volume(receiver_id, self._sender_volumes[receiver_id])

    async def _rebuild_entry_for_tap(self, entry_id: str) -> None:
        """Rebuild one entry's pipelines so its external-target PCM tap exists."""
        is_airplay2 = (
            any(
                key == entry_id or key.startswith(f"{entry_id}-")
                for key in self._airplay2_pipelines
            )
            or entry_id in self._airplay2_runtime
        )
        if is_airplay2:
            await self._rebuild_airplay2_instances({entry_id})
        elif settings.airplay_engine == "local":
            async with self._restart_lock:
                await self._rebuild_classic_entries_locked({entry_id})

    async def _start_entry_targets(self, entry_id: str, *, resume: bool) -> None:
        """Start the external AirPlay/DLNA members of an entry's group.

        Shared by the classic local session path and the AirPlay 2 session
        callback so a mapped group's network members play regardless of which
        ingress the phone used.
        """
        tap = self._target_taps.get(entry_id)
        if tap is not None and self._airplay_targets:
            await self._airplay_targets.start_targets(
                entry_id,
                settings.receiver_airplay_targets(entry_id),
                tap,
                settings.receiver_airplay_delays(entry_id),
                settings.receiver_network_channels(entry_id),
                initial_volume=(
                    settings.default_volume
                    if not resume
                    and settings.default_volume_enabled
                    and self._volume_modes.get(entry_id) == "independent"
                    else None
                ),
            )
        dlna_ids = settings.receiver_dlna_targets(entry_id)
        if dlna_ids and self._dlna_targets:
            # DLNA renderers pull the HTTP stream — no PCM tap needed.
            cast_url = (
                f"http://{settings.effective_stream_host}:{settings.stream_port}/stream/{entry_id}"
            )
            await self._dlna_targets.play_targets(
                entry_id, dlna_ids, cast_url, settings.receiver_network_channels(entry_id)
            )
            if (
                not resume
                and settings.default_volume_enabled
                and self._volume_modes.get(entry_id) == "independent"
            ):
                await self._dlna_targets.set_volume(entry_id, settings.default_volume)

    async def _local_session_stop(self, receiver_id: str) -> None:
        self._active_sessions.discard(receiver_id)
        if self.on_session_stop:
            try:
                await self.on_session_stop(receiver_id)
            except Exception:
                logger.exception("session_stop hook failed for %s", receiver_id)

    def _session_active_for(self, receiver_id: str) -> Callable[[], bool]:
        return lambda: receiver_id in self._active_sessions

    async def _recover_stalled_source(self, stream_id: str) -> None:
        """Replace the upstream receiver after PCM stalls in a live session.

        The rebuild tears the pipelines down before recreating them; if it
        throws, the entry is left half-stopped and the stall watchdog is
        already gone (it exits after spawning this recovery) — so a single
        failure would silence the entry until the next engine restart. Retry
        once after a short backoff; the flag in the finally still releases the
        re-entry latch, so a later stall triggers a fresh attempt.
        """
        if self._stall_recovery_requested:
            return
        self._stall_recovery_requested = True
        logger.warning("Rebuilding AirPlay receiver after PCM stall on %s", stream_id)
        try:
            airplay2_ids = [item.id for item in settings.airplay2_instances if item.enabled]
            instance_id = _stream_owner(stream_id, airplay2_ids)
            if instance_id is None:
                await self._stall_recovery_once(self.restart)
                return
            self._maintenance_sessions.add(instance_id)
            self._active_sessions.discard(instance_id)
            try:
                await self._stall_recovery_once(
                    lambda: self._rebuild_airplay2_instances({instance_id})
                )
            finally:
                self._maintenance_sessions.discard(instance_id)
        finally:
            self._stall_recovery_requested = False

    @staticmethod
    async def _stall_recovery_once(action) -> None:
        """Run one stall-recovery action; retry once after a backoff.

        An exhausted retry propagates to the caller's task logger — the entry
        stays silent but the next stall (watchdog re-arms on the new
        pipelines, or a full engine restart) gets another chance."""
        for attempt in (1, 2):
            try:
                await action()
                return
            except Exception:
                logger.exception(
                    "Stall recovery attempt %d failed%s",
                    attempt,
                    "; retrying once" if attempt == 1 else "",
                )
                if attempt == 1:
                    await asyncio.sleep(2.0)
                else:
                    raise

    def stream_starved(self, stream_id: str) -> bool:
        """The stream's pipeline stopped producing bytes during a live sender
        session — a connected speaker is then pulling a worthless stream. A
        paused sender looks identical at this layer, so callers must treat this
        as "worth a restore nudge", not as proof of failure."""
        pipeline = self._pipelines.get(stream_id) or self._airplay2_pipelines.get(stream_id)
        if pipeline is None or pipeline.device_id not in self._active_sessions:
            return False
        silence = pipeline.source_silence_seconds
        return silence is not None and silence > 10.0

    async def _local_volume(self, receiver_id: str, percent: int) -> None:
        """Apply a sender's volume through the loudness stack for `receiver_id`.

        Loudness is four deliberate layers — sender digital gain (this method,
        applied to raw PCM via ``apply_pcm_gain``), per-speaker EQ, per-speaker
        trim (``gains_db``), and the physical amplifier — and two modes:

        * ``independent`` — sender volume stays digital (stream gain only);
          the physical speaker volume is a separate, per-device concern.
        * ``linked`` — sender volume drives the physical speakers' own volume
          (``DeviceManager.owned_targets``, or the external target managers)
          and the stream gain is pinned to 100% to avoid double attenuation.

        DLNA keeps its own session latch of the same mode in
        ``DlnaService.state.volume_mode``; owner keys are namespaced per
        ingress (``dlna:{id}`` vs bare ``id``) so the two paths never steal
        each other's speakers.
        """
        self._sender_volumes[receiver_id] = percent
        mode = self._volume_modes.setdefault(receiver_id, settings.sender_volume_mode)
        pipelines = {**self._pipelines, **getattr(self, "_airplay2_pipelines", {})}
        for key, pipeline in pipelines.items():
            if key == receiver_id or key.startswith(f"{receiver_id}-"):
                pipeline.set_input_volume(100 if mode == "linked" else percent)
                pipeline.set_loudness_level(percent)
        if self._airplay_targets:
            self._airplay_targets.set_input_volume(
                receiver_id, 100 if mode == "linked" else percent
            )
            if mode == "linked":
                await self._airplay_targets.set_volume(receiver_id, percent)
            else:
                self._airplay_targets.independent_volume(receiver_id)
        if mode == "linked" and getattr(self, "_dlna_targets", None):
            await self._dlna_targets.set_volume(receiver_id, percent)
        logger.info("AirPlay %s stream volume -> %s%%", receiver_id, percent)
        if self.on_receiver_volume:
            await self.on_receiver_volume(receiver_id, percent)

    async def _stop_engine(self) -> None:
        # Receiver teardown invalidates every sender session. Leaving these
        # latches set makes freshly-created silent pipelines immediately look
        # stalled and creates a restart loop.
        self._active_sessions.clear()
        self._stale_active_since.clear()
        if self._airplay_targets:
            await self._airplay_targets.stop_all()
        if self._dlna_targets:
            await self._dlna_targets.stop_all()
        if self._airplay_discovery:
            await self._airplay_discovery.stop()
        if self._dlna_discovery:
            await self._dlna_discovery.stop()
            self._dlna_discovery = None
            self._dlna_targets = None
        self._target_taps.clear()
        await self._local_provider.stop()
        await self._stop_airplay2_pipelines()
        await self._stop_pipelines()
        await self._receiver_manager.stop()
        await self._stream_server.stop()

    def _derive_status(self) -> str:
        receivers = (
            list(self._local_provider.receivers.values())
            if settings.airplay_engine == "local"
            else self._receiver_manager.receivers
        )
        if not receivers:
            return "idle"
        failed = sum(receiver.status == "error" for receiver in receivers)
        if failed == len(receivers):
            return "error"
        if failed:
            return "degraded"
        return "running"

    async def _stop_pipelines(self, keep_streams: bool = False) -> None:
        for pipeline in list(self._pipelines.values()):
            try:
                await pipeline.stop(keep_stream=keep_streams)
            except Exception:
                logger.exception("Error stopping pipeline for %s", pipeline.device_id)
        self._pipelines.clear()
        for tee in self._tees.values():
            await tee.stop()
        self._tees.clear()

    async def _stop_classic_entry_pipelines(
        self, entry_id: str, keep_streams: bool = False
    ) -> None:
        """Stop one entry's pipelines and PCM tee, leaving every other entry
        (and its phone sessions) running."""
        stream_ids = [
            key for key in self._pipelines if key == entry_id or key.startswith(f"{entry_id}-")
        ]
        for stream_id in stream_ids:
            pipeline = self._pipelines.pop(stream_id, None)
            if not pipeline:
                continue
            try:
                await pipeline.stop(keep_stream=keep_streams)
            except Exception:
                logger.exception("Error stopping pipeline for %s", stream_id)
        tee = self._tees.pop(entry_id, None)
        if tee:
            try:
                await tee.stop()
            except Exception:
                logger.exception("Error stopping PCM tee for %s", entry_id)
        self._target_taps.pop(entry_id, None)

    def _gc_dead_streams(self) -> None:
        """Drop registered endpoints no pipeline serves any more."""
        active_stream_ids = set(self._pipelines) | set(self._airplay2_pipelines)
        for stream_id in self._stream_server.stream_ids():
            if stream_id not in active_stream_ids:
                self._stream_server.unregister_stream(stream_id)

    async def _reconcile_classic_entries_locked(self, diff: PlanDiff) -> None:
        """Apply classic receiver add/remove/rename without touching the rest.

        The RAOP layer diffs its own receiver set (stopping only removed or
        renamed servers), so unrelated receivers keep their sessions; only the
        affected entries' pipelines and stream endpoints are torn down and
        re-created. Lock held.
        """
        logger.info(
            "Reconciling classic receivers: +%s -%s",
            sorted(diff.classic_added),
            sorted(diff.classic_removed),
        )
        desired = [(item.id, item.name) for item in settings.active_receivers()]
        await self._local_provider.start(
            desired,
            settings.effective_stream_host,
            self._local_session_start,
            self._local_session_stop,
            self._local_volume,
        )
        for entry_id in sorted(diff.classic_removed):
            await self._stop_classic_entry_pipelines(entry_id)
        for entry_id in sorted(diff.classic_added):
            item = self._local_provider.receivers.get(entry_id)
            if not item or item.status != "running" or not item.server:
                continue
            try:
                await self._create_local_pipelines(item)
            except Exception:
                logger.exception("Failed to create pipelines for %s", entry_id)
                self._error_count += 1
        self._gc_dead_streams()

    async def _rebuild_classic_entries_locked(self, entry_ids: set[str]) -> None:
        """Rebuild only the given entries' pipelines, keeping stream endpoints
        registered so connected speakers do not get disconnected. Lock held."""
        logger.info("Rebuilding pipelines for entries: %s", sorted(entry_ids))
        for entry_id in sorted(entry_ids):
            await self._stop_classic_entry_pipelines(entry_id, keep_streams=True)
            item = self._local_provider.receivers.get(entry_id)
            if not item or item.status != "running" or not item.server:
                continue
            try:
                await self._create_local_pipelines(item)
            except Exception:
                logger.exception("Failed to rebuild pipeline for %s", entry_id)
                self._error_count += 1
        self._gc_dead_streams()

    async def _stop_airplay2_pipelines(self) -> None:
        instance_ids = (
            set(self._airplay2_runtime) | set(self._airplay2_sources) | set(self._airplay2_tees)
        )
        for instance_id in instance_ids:
            await self._stop_airplay2_pipeline(instance_id)
        self._airplay2_runtime.clear()

    async def _stop_airplay2_pipeline(self, instance_id: str) -> None:
        stream_ids = [
            key
            for key in self._airplay2_pipelines
            if key == instance_id or key.startswith(f"{instance_id}-")
        ]
        for stream_id in stream_ids:
            pipeline = self._airplay2_pipelines.pop(stream_id, None)
            if not pipeline:
                continue
            try:
                await pipeline.stop()
            except Exception:
                logger.exception("Error stopping AirPlay 2 pipeline %s", pipeline.device_id)
        # External members of the instance's group stop with it.
        if self._airplay_targets:
            await self._airplay_targets.stop_targets(instance_id)
        if self._dlna_targets:
            await self._dlna_targets.stop_targets(instance_id)
        self._target_taps.pop(instance_id, None)
        tee = self._airplay2_tees.pop(instance_id, None)
        if tee:
            try:
                await tee.stop()
            except Exception:
                logger.exception("Error stopping AirPlay 2 PCM tee for %s", instance_id)
        source = self._airplay2_sources.pop(instance_id, None)
        if source:
            try:
                await source.stop()
            except Exception:
                logger.exception("Error stopping AirPlay 2 PCM source for %s", instance_id)

    async def session_start(self, device_id: str | None = None) -> None:
        """Called when an AirPlay session begins on a receiver."""
        if device_id is None:
            # Single-receiver fallback: use the only pipeline.
            if len(self._pipelines) == 1:
                device_id = next(iter(self._pipelines.keys()))
            else:
                logger.warning("session_start called without device_id in multi-receiver mode")
                return
        pipeline = self._pipelines.get(device_id) or self._airplay2_pipelines.get(device_id)
        if pipeline and device_id in self._airplay2_pipelines and self._pipeline_needs_rebuild(
            pipeline
        ):
            # Same revival rule as classic receivers: a pipeline whose reader
            # finished needs a rebuild, not a start() that reuses it.
            async with self._restart_lock:
                await self._rebuild_airplay2_instances_locked({device_id})
            pipeline = self._airplay2_pipelines.get(device_id) or pipeline
        if pipeline:
            self._active_sessions.add(device_id)
            self._volume_modes[device_id] = settings.sender_volume_mode
            await pipeline.session_start()
            if device_id in self._airplay2_pipelines:
                # AirPlay 2 ingress: start the mapped group's external
                # AirPlay/DLNA members too (classic sessions do this in
                # _local_session_start; this callback is their only trigger).
                tap_missing = self._target_taps.get(device_id) is None
                if tap_missing and settings.receiver_airplay_targets(device_id):
                    await self._rebuild_entry_for_tap(device_id)
                await self._start_entry_targets(device_id, resume=False)
            if device_id in self._sender_volumes:
                await self._local_volume(device_id, self._sender_volumes[device_id])
        else:
            logger.warning("session_start for unknown receiver: %s", device_id)

    async def session_stop(self, device_id: str | None = None) -> None:
        """Called when an AirPlay session ends on a receiver."""
        if device_id is None:
            if len(self._pipelines) == 1:
                device_id = next(iter(self._pipelines.keys()))
            else:
                return
        self._active_sessions.discard(device_id)
        if device_id in self._maintenance_sessions:
            logger.info("Ignoring maintenance session stop for %s", device_id)
            return
        if self.on_session_stop:
            try:
                await self.on_session_stop(device_id)
            except Exception:
                logger.exception("session_stop hook failed for %s", device_id)

    async def disconnect_sessions(self, receiver_id: str | None = None) -> int:
        """Disconnect active AirPlay senders while leaving receivers advertised."""
        if settings.airplay_engine != "local":
            return 0
        return await self._local_provider.disconnect(receiver_id)

    @property
    def stream_server(self) -> StreamServer:
        """Read-only access to the stream server for playback lifecycle code."""
        return self._stream_server

    def is_session_active(self, receiver_id: str) -> bool:
        """Whether a sender session is currently live for this receiver."""
        return receiver_id in self._active_sessions

    def stream_client_count(self, stream_id: str) -> int:
        """How many speakers are currently pulling a stream (ground truth for
        "is audio really flowing out")."""
        return self._stream_server.client_count(stream_id)

    # -- supervisor primitives ---------------------------------------------
    # The audio supervisor owns the recovery policy; these are the idempotent
    # building blocks it drives (and the signals it judges from).
    def attach_device_manager(self, device_manager) -> None:
        """Give the supervisor primitives access to the speaker controller."""
        self._device_manager = device_manager

    def attach_supervisor(self, supervisor) -> None:
        """Expose the health arbiter's state in diagnostics."""
        self._supervisor = supervisor

    def entry_ids(self) -> list[str]:
        """Every audio entry: classic receivers plus AirPlay 2 instances."""
        return sorted(set(self._pipelines) | set(self._airplay2_pipelines))

    def entry_stream_ids(self, entry_id: str) -> list[str]:
        return [
            stream_id
            for stream_id in self._stream_server.stream_ids()
            if stream_id == entry_id or stream_id.startswith(f"{entry_id}-")
        ]

    def entry_targets(self, entry_id: str) -> list[str]:
        return settings.receiver_targets(entry_id)

    def entry_pipeline_usable(self, entry_id: str) -> bool:
        """False when no pipeline can serve the next session as-is.

        A pipeline whose PCM reader finished (clean encoder exit) looks idle
        forever; reviving it needs a rebuild, so it must not be mistaken for
        "healthy but quiet".
        """
        streams = self.entry_stream_ids(entry_id)
        pipelines = [
            pipeline
            for stream_id in streams
            if (pipeline := self.pipeline_for_stream(stream_id)) is not None
        ]
        if not pipelines:
            return False
        return all(
            pipeline.running and pipeline.status == "running" for pipeline in pipelines
        )

    def entry_source_idle_ms(self, entry_id: str) -> float | None:
        """Milliseconds since the entry's PCM source last delivered real bytes."""
        idles = [
            idle
            for stream_id in self.entry_stream_ids(entry_id)
            if (pipeline := self.pipeline_for_stream(stream_id)) is not None
            and (idle := pipeline.source_idle_ms()) is not None
        ]
        if not idles:
            return None
        return max(idles)

    def entry_source_bursty(self, entry_id: str) -> bool:
        """True when the source delivers in lumps rather than steadily."""
        return any(
            pipeline.source_bursty()
            for stream_id in self.entry_stream_ids(entry_id)
            if (pipeline := self.pipeline_for_stream(stream_id)) is not None
        )

    def entry_consumer_lagging(self, entry_id: str) -> bool:
        """True when a speaker of this entry cannot keep up with us.

        Only then does widening the delay-line reserve help. A starved source
        (lumps the reserve never fills) must not be "fixed" the same way — that
        would hold audio back even longer and make delivery less even.
        """
        for stream_id in self.entry_stream_ids(entry_id):
            if self._stream_server.dropped_chunks.get(stream_id, 0):
                return True
            for state in self._stream_server.client_delay_states(stream_id):
                if int(state.get("queue_drops") or 0):
                    return True
        return False

    def entry_pace_stats(self, entry_id: str) -> dict[str, float]:
        """Aggregated pacing sleep for an entry's pipelines (1x hold-backs)."""
        totals: dict[str, float] = {"sleeps": 0, "total_ms": 0.0, "max_ms": 0.0}
        for stream_id in self.entry_stream_ids(entry_id):
            pipeline = self.pipeline_for_stream(stream_id)
            if pipeline is None:
                continue
            stats = pipeline.pace_stats()
            totals["sleeps"] += stats["sleeps"]
            totals["total_ms"] += stats["total_ms"]
            totals["max_ms"] = max(totals["max_ms"], stats["max_ms"])
        totals["total_ms"] = round(totals["total_ms"], 1)
        return totals

    def stream_served(self, stream_id: str) -> bool:
        """A speaker is connected AND bytes moved recently (``is_flowing``)."""
        return self._stream_server.client_count(stream_id) > 0 and (
            self._stream_server.is_flowing(stream_id)
        )

    async def rebuild_entry(self, entry_id: str) -> None:
        """Give an entry fresh pipelines (new PCM reader and tee). Idempotent."""
        async with self._restart_lock:
            if entry_id in self._airplay2_pipelines or entry_id in self._airplay2_sources:
                await self._rebuild_airplay2_instances_locked({entry_id})
            else:
                await self._rebuild_classic_entries_locked({entry_id})
        logger.info("Audio supervisor rebuilt entry %s", entry_id)

    async def recover_source(self, entry_id: str) -> None:
        """Restart the entry's PCM source without touching the speaker."""
        stream_ids = self.entry_stream_ids(entry_id)
        await self._recover_stalled_source(stream_ids[0] if stream_ids else entry_id)

    async def kick_entry_clients(self, entry_id: str) -> None:
        """Drop the entry's speaker connections so they re-attach fresh."""
        for stream_id in self.entry_stream_ids(entry_id):
            self._stream_server.kick_clients(stream_id)

    async def reissue_entry_play(self, entry_id: str) -> None:
        """Re-issue the entry's play command with a fresh cache-buster.

        Ownership is checked per speaker: a speaker that moved to another
        receiver (a protocol switch, say) must not be stolen back.
        """
        url = self._stream_url_for_entry(entry_id)
        if not url or self._device_manager is None:
            return
        for did in self.entry_targets(entry_id):
            owner = self._device_manager.owner_of(did)
            if owner not in (None, entry_id):
                continue
            suffix = settings.stream_suffix(entry_id, did)
            play_url = f"{url}{suffix}/for/{entry_id}/{did}?s={time.time_ns()}"
            try:
                await self._device_manager.play_stream(
                    did, play_url, owner=entry_id, force=True
                )
            except Exception:
                logger.exception("Supervisor play re-issue failed for %s on %s", entry_id, did)

    def _stream_url_for_entry(self, entry_id: str) -> str | None:
        for stream_id in self.entry_stream_ids(entry_id):
            url = f"http://{settings.effective_stream_host}:{settings.stream_port}/stream/{stream_id}"
            return url
        return None

    def set_entry_buffer(self, entry_id: str, seconds: float | None) -> None:
        """Widen (or release) one entry's delay-line reserve.

        Bursty sources are the stall cause no recovery action can fix; extra
        slack is the remedy, applied per entry so other speakers keep their
        latency. ``None`` restores the global default.
        """
        for stream_id in self.entry_stream_ids(entry_id):
            self._stream_server.set_buffer_override(stream_id, seconds)

    def drop_stream_clients(self, receiver_id: str) -> None:
        """Close speaker-side HTTP connections of a receiver's streams.

        Called when a session ends: a paused speaker otherwise keeps the socket
        open forever, silently waiting for data that will never come (and the
        topology would show a flow that isn't there)."""
        for stream_id in self._stream_server.stream_ids():
            if stream_id == receiver_id or stream_id.startswith(f"{receiver_id}-"):
                self._stream_server.kick_clients(stream_id)

    async def _sweep_stale_stream_clients(self) -> None:
        """Last-resort cleanup for speaker connections the teardown path missed.

        The normal disconnect flow (delayed stop → speaker stop + client kick)
        works, but a kicked speaker retries its last URL a few times and can
        re-attach AFTER the kick, and a canceled pending stop never kicks at
        all. Anything left with no data flow and no owning session is stale.
        """
        while True:
            await asyncio.sleep(STREAM_SWEEP_INTERVAL_SECONDS)
            try:
                self._sweep_stale_once()
            except Exception:
                logger.exception("Stale stream client sweep failed")

    def _sweep_stale_once(self) -> None:
        """Last-resort cleanup for speaker connections the teardown path missed.

        Session expiry is timed per OWNER from the moment NO member stream is
        flowing anymore: as long as any grouped sink still receives audio the
        session is alive, and one flaky member must not restart the group's
        timer nor let a paused group expire while another member plays.
        """
        now = time.monotonic()
        receiver_ids = [receiver.id for receiver in settings.active_receivers()]
        receiver_ids.extend(
            instance.id for instance in settings.airplay2_instances if instance.enabled
        )
        owners_with_clients: set[str] = set()
        flowing_owners: set[str] = set()
        idle_streams: dict[str, list[str]] = {}
        for stream_id in self._stream_server.stream_ids():
            owner = _stream_owner(stream_id, receiver_ids)
            if owner is None:
                continue
            if not self._stream_server.client_count(stream_id):
                continue
            owners_with_clients.add(owner)
            if self._stream_server.is_flowing(stream_id, window=STREAM_IDLE_KICK_SECONDS):
                flowing_owners.add(owner)
                continue
            idle_streams.setdefault(owner, []).append(stream_id)

        seen_idle: set[str] = set()
        expired: set[str] = set()
        for owner in sorted(owners_with_clients):
            if owner in flowing_owners:
                # A live member keeps the whole session alive; the next fully
                # silent window starts a fresh timer.
                self._stale_active_since.pop(owner, None)
                continue
            if owner not in self._active_sessions:
                continue
            seen_idle.add(owner)
            # Read per sweep so a settings change hot-applies without an
            # engine restart; 0 disables the expiry (pause indefinitely).
            stale_timeout = float(settings.stale_session_timeout)
            if stale_timeout <= 0:
                continue  # paused sender session kept by configuration
            started = self._stale_active_since.setdefault(owner, now)
            if now - started < stale_timeout:
                continue  # fully silent session: keep the waiting speakers briefly
            logger.warning(
                "Expiring stale active session %s after %.1fs without stream data",
                owner,
                now - started,
            )
            expired.add(owner)
            self._active_sessions.discard(owner)
            self._stale_active_since.pop(owner, None)
            if self.on_session_stop:
                self._spawn_aux(
                    self.on_session_stop(owner),
                    f"stale-session-stop:{owner}",
                )
        for owner in list(self._stale_active_since):
            if owner not in seen_idle:
                self._stale_active_since.pop(owner, None)
        # Idle members of an expired or ownerless session are stale; idle
        # members of a still-active session keep their connection — kicking
        # them would only trigger needless speaker reconnects (a paused sender
        # or a silent group sink while another member plays).
        for owner, stream_ids in idle_streams.items():
            if owner in self._active_sessions and owner not in expired:
                continue
            for stream_id in stream_ids:
                self._stream_server.kick_clients(stream_id)


def _stream_owner(stream_id: str, receiver_ids: list[str]) -> str | None:
    """Map a stream id back to its receiver: variants are `<receiver_id>-L/-R/-qN`."""
    matches = [rid for rid in receiver_ids if stream_id == rid or stream_id.startswith(f"{rid}-")]
    return max(matches, key=len) if matches else None


def _is_debounceable_eq_change(diff: PlanDiff) -> bool:
    """True when the diff is ONLY encoder restarts (EQ/loudness/gain tweaks).

    Format changes (audio_only) and structural changes must apply immediately —
    the caller is a settings toggle expecting the stream to flip now. Pure sound
    edits tolerate a 600 ms settle window and benefit hugely from coalescing.
    """
    return (
        bool(diff.encoder_restart)
        and not diff.audio_only
        and not diff.full_restart_required
        and not diff.classic_added
        and not diff.classic_removed
        and not diff.classic_rebuild
        and not diff.airplay2_added
        and not diff.airplay2_removed
        and not diff.airplay2_rebuild
    )


def _diff_summary(diff: PlanDiff) -> str:
    parts = []
    if diff.full_restart_required:
        parts.append("full-restart")
    if diff.classic_added:
        parts.append(f"classic+{sorted(diff.classic_added)}")
    if diff.classic_removed:
        parts.append(f"classic-{sorted(diff.classic_removed)}")
    if diff.classic_rebuild:
        parts.append(f"classic-rebuild{sorted(diff.classic_rebuild)}")
    if diff.audio_only:
        parts.append("audio-format")
    if diff.encoder_restart:
        parts.append(f"encoder-restart{sorted(diff.encoder_restart)}")
    if diff.airplay2_added:
        parts.append(f"airplay2+{sorted(diff.airplay2_added)}")
    if diff.airplay2_removed:
        parts.append(f"airplay2-{sorted(diff.airplay2_removed)}")
    if diff.airplay2_rebuild:
        parts.append(f"airplay2-rebuild{sorted(diff.airplay2_rebuild)}")
    if diff.external_airplay_changed:
        parts.append(f"airplay-targets{sorted(diff.external_airplay_changed)}")
    if diff.external_dlna_changed:
        parts.append(f"dlna-targets{sorted(diff.external_dlna_changed)}")
    if diff.membership_changed:
        parts.append(f"membership{sorted(diff.membership_changed)}")
    if diff.delay_only:
        parts.append("delay-only(live)")
    return ", ".join(parts)
