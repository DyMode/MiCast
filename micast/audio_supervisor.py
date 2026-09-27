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
# A session that HAS delivered audio and then goes quiet is a pause or a track
# transition, not a fault: acting on it is not free, because restoring the
# source means restarting the receiver — for AirPlay 2 that is shairport-sync,
# and restarting it drops the phone's session outright. So a sender that has
# played may stay quiet this long before we call it broken. A session that
# never delivered anything is judged on the ordinary grace above.
SOURCE_PAUSE_GRACE_SECONDS = 20.0
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
STATE_QUIET = "quiet"
STATE_PAUSED = "paused"
STATE_DEGRADED_OUR_SIDE = "degraded_our_side"
STATE_DEGRADED_SPEAKER = "degraded_speaker"
STATE_BURSTY = "bursty"
STATE_UNHEALTHY = "unhealthy"


LEVEL_SOURCE = "source_restart"
LEVEL_REBUILD = "pipeline_rebuild"
LEVEL_REISSUE = "play_reissue"
LEVEL_KICK = "client_kick"

# User-facing wording for the diagnostics page (details stay engineer-facing).
STATE_LABELS = {
    STATE_IDLE: "空闲",
    STATE_STARTING: "启动中",
    STATE_HEALTHY: "正常",
    STATE_QUIET: "音源暂停",
    STATE_PAUSED: "已暂停",
    STATE_DEGRADED_OUR_SIDE: "MiCast 侧异常",
    STATE_DEGRADED_SPEAKER: "音箱侧异常",
    STATE_BURSTY: "音源成团",
    STATE_UNHEALTHY: "未能恢复",
}
LEVEL_LABELS = {
    LEVEL_SOURCE: "重启音源",
    LEVEL_REBUILD: "重建管道",
    LEVEL_REISSUE: "重发播放",
    LEVEL_KICK: "重连音箱",
}


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
    # Last time this session actually delivered source audio. Distinguishes a
    # sender that paused from one that never started (see STATE_QUIET).
    delivered_at: float = 0.0
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
            entry=self.entry_id,
            label=f"转为 {STATE_LABELS.get(state, state)}",
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
    def _targets_paused(self, entry_id: str) -> bool:
        """True when every speaker of this entry was paused from OUR UI.

        Deliberately paused is not a fault: the supervisor used to see "speaker
        connected but taking nothing" and re-issued the play command ~8s later,
        which resumed the speaker behind the user's back and cleared the paused
        flag — field data (0.4.0): "暂停后自己又播放了，控制就乱了".
        """
        manager = self._device_manager
        if manager is None:
            return False
        try:
            targets = self._bridge.entry_targets(entry_id)
            if not targets:
                return False
            return all(manager.is_paused(did) for did in targets)
        except Exception:  # pragma: no cover - a stub manager must never break ticks
            return False

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
            "paused": self._targets_paused(entry_id),
        }

    def classify(self, entry_id: str, signals: dict | None = None) -> str:
        """Map signals to exactly one state. Pure, so tests can drive it."""
        signals = signals or self._signals(entry_id)
        entry = self.health(entry_id)
        if not signals["session_active"]:
            entry.session_started_at = 0.0
            entry.delivered_at = 0.0
            return STATE_IDLE
        now = self._clock()
        if not entry.session_started_at:
            entry.session_started_at = now
        if signals["source_flowing"]:
            entry.delivered_at = now
        if signals["stream_served"]:
            return STATE_BURSTY if signals["source_bursty"] else STATE_HEALTHY
        if now - entry.session_started_at < SESSION_GRACE_SECONDS:
            return STATE_STARTING
        if signals.get("paused"):
            # Stopped on purpose from our own UI: the speaker is not pulling
            # because the listener asked it to stop, not because anything broke.
            return STATE_PAUSED
        if not signals["pipeline_usable"]:
            return STATE_DEGRADED_OUR_SIDE
        if not signals["source_flowing"]:
            silent_s = (signals["source_idle_ms"] or 0.0) / 1000
            if entry.delivered_at and silent_s < SOURCE_PAUSE_GRACE_SECONDS:
                return STATE_QUIET
            return STATE_DEGRADED_OUR_SIDE
        return STATE_DEGRADED_SPEAKER

    # -- ladder ------------------------------------------------------------
    async def tick(self) -> None:
        """Advance every entry by one step. Safe to call repeatedly."""
        live = set(self._bridge.entry_ids())
        for entry_id in list(live):
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
        # Streams the plan no longer publishes must not linger as phantom
        # entries: a stereo pair showed six rows for three real streams, and the
        # stale ones read like extra pipelines nobody had explained.
        for entry_id in [key for key in self._entries if key not in live]:
            self._entries.pop(entry_id, None)
            self._last_tick.pop(entry_id, None)

    async def _step(self, entry_id: str) -> None:
        entry = self.health(entry_id)
        signals = self._signals(entry_id)
        state = self.classify(entry_id, signals)
        entry.set_state(state, self._reason_for(state, signals))
        if state in (STATE_IDLE, STATE_STARTING, STATE_QUIET, STATE_PAUSED):
            # A paused sender (or a speaker we paused ourselves) is not a fault:
            # no action, and the ladder starts over if the entry really fails
            # later.
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
        if state == STATE_QUIET:
            return f"音源已静默 {int((signals['source_idle_ms'] or 0) / 1000)}s（暂停或换曲）"
        if state == STATE_PAUSED:
            return "已从 MiCast 暂停"
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
        if entry.escalations:
            logger.info(
                "Audio supervisor: %s recovered (was %s)",
                entry.entry_id,
                entry.state,
            )
            metrics.record_event(
                "recovered",
                detail=entry.entry_id,
                entry=entry.entry_id,
                label="已恢复",
            )
        entry.escalations = 0
        entry.last_action_ok = True if entry.last_action else entry.last_action_ok
        # Forget the action: a healthy entry that keeps its last_action logs
        # "recovered (was healthy)" on every 2s tick forever (field data: 500+
        # identical lines in 90 seconds, which buried the real events).
        entry.last_action = ""
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
        metrics.record_event(
            "recovery",
            detail=f"{entry.entry_id} {level} #{entry.escalations}",
            entry=entry.entry_id,
            label=f"自动{LEVEL_LABELS.get(level, level)}（第 {entry.escalations} 次）",
        )
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
        """Widen the delay line ONLY when the consumer, not the source, lags.

        The delay line decides how much audio we hold back before sending. That
        recovers a speaker that cannot keep up (its queue overflows) — but when
        the SOURCE is the lumpy side, holding more back makes delivery later and
        no smoother, and the reserve never fills anyway. A long bursty stretch
        with no queue drops is therefore reported, not "fixed".
        """
        now = self._clock()
        if not entry.bursty_since:
            entry.bursty_since = now
        if not self._bridge.entry_consumer_lagging(entry.entry_id):
            if now - entry.bursty_since > 60.0:
                metrics.record_event(
                    "bursty_source",
                    detail=entry.entry_id,
                    entry=entry.entry_id,
                    label="音源成团（供给侧，未调整缓冲）",
                )
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
            entry=entry.entry_id,
            label=f"缓冲加大到 {entry.buffer_override:.2f}s",
        )
        logger.info(
            "Audio supervisor: widened %s delay line to %.2fs (slow consumer)",
            entry.entry_id,
            entry.buffer_override,
        )
        entry.bursty_since = now

    def _decay_buffer(self, entry: EntryHealth) -> None:
        if entry.buffer_override:
            entry.buffer_override = None
            entry.bursty_since = 0.0
            self._bridge.set_entry_buffer(entry.entry_id, None)
