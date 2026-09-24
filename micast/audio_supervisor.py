"""Single authority for "is this entry delivering audio, and if not, fix it".

Before this module six independent observers each kept their own idea of
health and their own action: the source-stall watchdog, the encoder-exit
watcher, the ghost-client reaper, the config plan diff, the Xiaomi status
watchdog and the play-error retry loop. They overlapped, judged from
different evidence, were blind exactly where it mattered (a pipeline that
dies mid-session, a speaker whose control channel stops answering) and — the
decisive gap — never checked whether a recovery actually worked, so failures
were silent and permanent and only a manual pipeline rebuild helped.

The supervisor keeps ONE state per entry, derived from signals we own:

* ``session_active``  — a sender session is live for this entry
* ``source_flowing``  — the PCM source delivered real bytes recently
* ``stream_served``   — a speaker is connected *and* bytes are actually moving
* ``pipeline_usable`` — the entry's pipelines can serve the next session
* ``source_bursty``   — the source delivers in lumps rather than steadily

and drives a single escalation ladder, every step rate-limited, idempotent,
recorded in the audio black box and **verified** before deciding whether to
escalate or stand down. Entries in no-session state are left strictly alone:
idle churn was one of the field failures this replaces.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from micast.audio_metrics import metrics

logger = logging.getLogger(__name__)

TICK_SECONDS = 2.0
# A session that just started gets this long to deliver its first audio before
# any recovery is considered; phones routinely take a second or two to SETUP.
SESSION_GRACE_SECONDS = 6.0
# Source silence shorter than this is normal jitter between chunk deliveries.
SOURCE_FLOW_GRACE_SECONDS = 4.0
# Bytes must move within this window for a speaker to count as served.
SERVE_WINDOW_SECONDS = 6.0
# After an action, wait this long before judging whether it worked.
VERIFY_SECONDS = 8.0
# Re-issuing play is cheap but not free (cloud round trip); keep it rare.
PLAY_REISSUE_MIN_INTERVAL_SECONDS = 15.0
# Consecutive failed escalations before an entry is declared unhealthy.
MAX_ESCALATIONS = 3
# Unhealthy entries are probed this much less often (no hammering).
UNHEALTHY_BACKOFF_SECONDS = 120.0

STATE_IDLE = "idle"
STATE_STARTING = "starting"
STATE_HEALTHY = "healthy"
STATE_DEGRADED_OUR_SIDE = "degraded_our_side"
STATE_DEGRADED_SPEAKER = "degraded_speaker"
STATE_BURSTY = "bursty"
STATE_UNHEALTHY = "unhealthy"

LEVEL_SOURCE = "source_restart"
LEVEL_REBUILD = "pipeline_rebuild"
LEVEL_REISSUE = "play_reissue"
LEVEL_KICK = "client_kick"


@dataclass
class EntryHealth:
    entry_id: str
    state: str = STATE_IDLE
    since: float = field(default_factory=time.monotonic)
    reason: str = ""
    escalations: int = 0
    last_action: str = ""
    last_action_at: float = 0.0
    last_action_ok: bool | None = None
    play_reissued_at: float = 0.0
    session_started_at: float = 0.0
    unhealthy_since: float = 0.0
    bursty_since: float = 0.0
    buffer_override: float | None = None

    def set_state(self, state: str, reason: str = "") -> bool:
        if state == self.state:
            self.reason = reason or self.reason
            return False
        self.state = state
        self.since = time.monotonic()
        self.reason = reason
        metrics.record_event(
            "health",
            detail=f"{self.entry_id}={state}" + (f" {reason}" if reason else ""),
        )
        return True

    def as_dict(self) -> dict:
        return {
            "state": self.state,
            "for_s": round(max(0.0, time.monotonic() - self.since), 1),
            "reason": self.reason,
            "escalations": self.escalations,
            "last_action": self.last_action,
            "last_action_ok": self.last_action_ok,
            "buffer_override_s": self.buffer_override,
        }


class AudioSupervisor:
    """Per-entry health arbiter and recovery ladder.

    ``bridge``/``device_manager`` are the existing owners of the primitives
    (rebuild, play, kick); the supervisor only decides *when* to use them.
    """

    def __init__(self, bridge, device_manager, *, clock=time.monotonic):
        self._bridge = bridge
        self._device_manager = device_manager
        self._clock = clock
        self._entries: dict[str, EntryHealth] = {}
        self._last_tick: dict[str, float] = {}
        self._stopping = False

    # -- entry bookkeeping -------------------------------------------------
    def entries(self) -> list[str]:
        return list(self._entries)

    def health(self, entry_id: str) -> EntryHealth:
        entry = self._entries.get(entry_id)
        if entry is None:
            entry = EntryHealth(entry_id=entry_id)
            self._entries[entry_id] = entry
        return entry

    def snapshot(self) -> dict:
        return {entry_id: item.as_dict() for entry_id, item in self._entries.items()}

    def recovery_in_progress(self, device_id: str) -> bool:
        """True while an entry owning this speaker is being recovered.

        The Xiaomi watchdog defers to this so one symptom is not answered by
        two competing play commands.
        """
        for entry_id, item in self._entries.items():
            recovering = item.state in (STATE_DEGRADED_SPEAKER, STATE_UNHEALTHY)
            if recovering and item.escalations and device_id in self._bridge.entry_targets(
                entry_id
            ):
                return True
        return False

    # -- signals -----------------------------------------------------------
    def _signals(self, entry_id: str) -> dict:
        bridge = self._bridge
        stream_ids = bridge.entry_stream_ids(entry_id)
        source_idle_ms = bridge.entry_source_idle_ms(entry_id)
        served = False
        clients = 0
        for stream_id in stream_ids:
            clients += bridge.stream_client_count(stream_id)
            if bridge.stream_served(stream_id):
                served = True
        flowing = source_idle_ms is not None and source_idle_ms < SOURCE_FLOW_GRACE_SECONDS * 1000
        return {
            "session_active": bridge.is_session_active(entry_id),
            "source_idle_ms": source_idle_ms,
            "source_flowing": flowing,
            "stream_served": served,
            "clients": clients,
            "pipeline_usable": bridge.entry_pipeline_usable(entry_id),
            "source_bursty": bridge.entry_source_bursty(entry_id),
        }

    def classify(self, entry_id: str, signals: dict | None = None) -> str:
        """Map signals to exactly one state. Pure, so tests can drive it."""
        signals = signals or self._signals(entry_id)
        entry = self.health(entry_id)
        if not signals["session_active"]:
            entry.session_started_at = 0.0
            return STATE_IDLE
        now = self._clock()
        if not entry.session_started_at:
            entry.session_started_at = now
        if signals["stream_served"]:
            return STATE_BURSTY if signals["source_bursty"] else STATE_HEALTHY
        if now - entry.session_started_at < SESSION_GRACE_SECONDS:
            return STATE_STARTING
        if not signals["pipeline_usable"] or not signals["source_flowing"]:
            return STATE_DEGRADED_OUR_SIDE
        return STATE_DEGRADED_SPEAKER

    # -- ladder ------------------------------------------------------------
    async def tick(self) -> None:
        """Advance every entry by one step. Safe to call repeatedly."""
        for entry_id in self._bridge.entry_ids():
            entry = self.health(entry_id)
            now = self._clock()
            if entry.state == STATE_UNHEALTHY and (
                now - entry.last_action_at < UNHEALTHY_BACKOFF_SECONDS
            ):
                continue
            if now - self._last_tick.get(entry_id, 0.0) < TICK_SECONDS:
                continue
            self._last_tick[entry_id] = now
            try:
                await self._step(entry_id)
            except Exception:
                logger.exception("Audio supervisor step failed for %s", entry_id)

    async def _step(self, entry_id: str) -> None:
        entry = self.health(entry_id)
        signals = self._signals(entry_id)
        state = self.classify(entry_id, signals)
        entry.set_state(state, self._reason_for(state, signals))
        if state in (STATE_IDLE, STATE_STARTING):
            entry.escalations = 0
            return
        if state == STATE_HEALTHY:
            self._on_healthy(entry)
            return
        if state == STATE_BURSTY:
            entry.escalations = 0
            await self._adapt_buffer(entry)
            return
        # Verification window: judge the previous action before escalating.
        if entry.last_action_at and self._clock() - entry.last_action_at < VERIFY_SECONDS:
            return
        if entry.last_action_at:
            entry.last_action_ok = False
        await self._escalate(entry, signals)

    def _reason_for(self, state: str, signals: dict) -> str:
        if state == STATE_DEGRADED_OUR_SIDE:
            if not signals["pipeline_usable"]:
                return "管道不可用（已结束或未启动）"
            return f"音源 {int(signals['source_idle_ms'] or 0)}ms 无数据"
        if state == STATE_DEGRADED_SPEAKER:
            if signals["clients"] == 0:
                return "音箱未连接取流"
            return "音箱已连接但未取走数据"
        if state == STATE_BURSTY:
            return "音源成团供给"
        return ""

    def _on_healthy(self, entry: EntryHealth) -> None:
        if entry.escalations or entry.last_action:
            logger.info(
                "Audio supervisor: %s recovered (was %s)",
                entry.entry_id,
                entry.state,
            )
            metrics.record_event("recovered", detail=entry.entry_id)
        entry.escalations = 0
        entry.last_action_ok = True if entry.last_action else entry.last_action_ok
        entry.unhealthy_since = 0.0
        self._decay_buffer(entry)

    async def _escalate(self, entry: EntryHealth, signals: dict) -> None:
        entry.escalations += 1
        now = self._clock()
        if entry.state == STATE_DEGRADED_OUR_SIDE:
            if not signals["pipeline_usable"]:
                level = LEVEL_REBUILD
                await self._bridge.rebuild_entry(entry.entry_id)
            else:
                level = LEVEL_SOURCE
                await self._bridge.recover_source(entry.entry_id)
        else:
            if entry.escalations >= 2:
                level = LEVEL_KICK
                await self._bridge.kick_entry_clients(entry.entry_id)
            else:
                level = LEVEL_REISSUE
            if now - entry.play_reissued_at >= PLAY_REISSUE_MIN_INTERVAL_SECONDS:
                entry.play_reissued_at = now
                await self._bridge.reissue_entry_play(entry.entry_id)
            else:
                level = f"{level}(rate-limited)"
        entry.last_action = level
        entry.last_action_at = now
        entry.last_action_ok = None
        metrics.record_event("recovery", detail=f"{entry.entry_id} {level} #{entry.escalations}")
        logger.info(
            "Audio supervisor: %s %s (escalation %d, %s)",
            entry.entry_id,
            level,
            entry.escalations,
            entry.reason,
        )
        if entry.escalations >= MAX_ESCALATIONS:
            entry.unhealthy_since = now
            entry.set_state(STATE_UNHEALTHY, entry.reason or entry.last_action)
            metrics.record_event(
                "unhealthy", detail=f"{entry.entry_id} after {entry.escalations} escalations"
            )

    # -- adaptive prebuffer ------------------------------------------------
    async def _adapt_buffer(self, entry: EntryHealth) -> None:
        """Bursty source + a lagging speaker: widen that entry's delay line.

        Bursty delivery is the one stall cause no recovery action can fix; the
        remedy is more slack, applied per entry so other speakers keep their
        latency. The override decays as soon as the source behaves again.
        """
        now = self._clock()
        if not entry.bursty_since:
            entry.bursty_since = now
            return
        if now - entry.bursty_since < 30.0:
            return
        current = entry.buffer_override or 0.0
        if current >= 1.5:
            return
        entry.buffer_override = min(1.5, (current or 0.25) + 0.25)
        self._bridge.set_entry_buffer(entry.entry_id, entry.buffer_override)
        metrics.record_event(
            "buffer_raised",
            detail=f"{entry.entry_id} {entry.buffer_override:.2f}s",
        )
        logger.info(
            "Audio supervisor: widened %s delay line to %.2fs (bursty source)",
            entry.entry_id,
            entry.buffer_override,
        )
        entry.bursty_since = now

    def _decay_buffer(self, entry: EntryHealth) -> None:
        if entry.buffer_override:
            entry.buffer_override = None
            entry.bursty_since = 0.0
            self._bridge.set_entry_buffer(entry.entry_id, None)
