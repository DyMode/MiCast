"""Device discovery and playback control manager."""

import asyncio
import json
import logging
import os
import tempfile
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from micast.config import settings
from micast.xiaomi.auth import XiaomiAuth, XiaomiAuthError
from micast.xiaomi.mina_api import CLOUD_CONCURRENCY, MinaAPI

# Owner tag for playback started outside MiCast streams (debug test tone/URL).
# Watched but never restored: when the speaker goes quiet the state is cleared.
MANUAL_PLAY_OWNER = "debug"

logger = logging.getLogger(__name__)

# Minimum seconds between watchdog-triggered stream restores per speaker;
# tighter loops make the speaker announce 播放失败 repeatedly when the stream
# is genuinely unavailable (e.g. during a bridge restart).
RESTORE_MIN_INTERVAL_SECONDS = 15.0

# Seconds between retries for speakers that rejected the session-start play
# command (offline at start, cloud hiccup). Retries back off exponentially and
# stop after PLAY_ERROR_MAX_ATTEMPTS so a speaker that left the account or is
# permanently offline does not hammer the cloud forever; the recorded error
# stays visible until the session stops.
PLAY_ERROR_RETRY_SECONDS = 30.0
PLAY_ERROR_MAX_ATTEMPTS = 5
PLAY_ERROR_BACKOFF_FACTOR = 2.0
PLAY_ERROR_MAX_BACKOFF_SECONDS = 300.0

# Xiaomi's status helper performs an implicit device-list request before each
# player query. A two-second watchdog therefore doubled cloud traffic without
# improving recovery in practice; three five-second misses still fit the
# existing 15-second restore guard.
STATUS_CHECK_INTERVAL_SECONDS = 5.0
# The formats a speaker can be asked to play, in the app's own words:
#
#   mp3 / flac / wav — our encoder's output (settings.audio.format)
#   pcm             — raw PCM straight through, which the app serves as an
#                     ENDLESS wav stream when transcoding is off
#
# "wav" and "pcm" are separate capabilities on purpose: the same decoder can
# play a finite WAV file and still reject the live-stream container.
CODEC_FORMATS = ("mp3", "flac", "wav", "pcm")
CODEC_LABELS = {
    "mp3": "MP3",
    "flac": "FLAC",
    "wav": "WAV（转码）",
    "pcm": "PCM（直通）",
}
# Raw passthrough cannot be verified from the server side: some firmware fetches
# the stream, keeps reading every byte, and plays nothing at all. Field data
# (2026-09): 厨房小爱 (OH2P) pulled the live PCM stream at full rate for minutes
# and stayed silent while 四楼小爱 (OH2) decoded the same stream fine — so
# sustained bytes are NOT evidence for "pcm". A healthy pcm pull is therefore
# recorded as UNVERIFIED (no boolean verdict at all, so nothing downstream can
# call it confirmed), and models already known to discard raw passthrough are
# recorded as unsupported so no re-learned pull can resurrect a false "✓".
PCM_UNVERIFIED_REASON = "pcm_passthrough_unverifiable"
PCM_MODEL_UNSUPPORTED_REASON = "model_rejects_pcm_passthrough"
PCM_UNSUPPORTED_MODELS = frozenset({"OH2P"})
# Earlier builds recorded upper-case names and aliased "PCM/WAV" onto the WAV
# file probe, which mixed the two: the alias is dropped on load (the truthful
# answer is re-learned from playback) instead of being carried forward.
LEGACY_CODEC_KEYS = {"MP3": "mp3", "FLAC": "flac", "WAV": "wav", "PCM/WAV": None}
# A verdict older than this is shown, but no longer trusted for advice.
CODEC_CAPABILITY_TTL_SECONDS = 30 * 24 * 3600
# Quality/bandwidth order used to recommend a format for a group: raw PCM costs
# ~1.4 Mbit/s and exists as a fallback, never as a first choice.
CODEC_PREFERENCE = ("flac", "wav", "mp3", "pcm")
# A pull shorter than this (or one that has stopped) proves nothing: some
# firmware fetches the URL and rejects the payload immediately.
CODEC_PULL_MIN_BYTES = 24_000
# A cloud/ubus hiccup must not leave a speaker playing: retry the stop.
STOP_COMMAND_ATTEMPTS = 3
STOP_COMMAND_RETRY_SECONDS = 0.5

# Cloud device-list cache; see DeviceManager._devices_fetched_at.
DEVICE_LIST_CACHE_SECONDS = 30.0


class DeviceManager:
    """Manages Xiaomi speakers, aliases, enabled targets, and playback."""

    def __init__(self, auth: XiaomiAuth):
        self.auth = auth
        self._service = None
        self._devices: list[dict] = []
        # device_list hits Xiaomi's cloud; the 诊断 page alone would otherwise
        # hammer it every 1.5s and get the account throttled (devices then
        # "vanish"). Serve the cached list within the TTL; callers needing
        # ground truth (post-login, explicit refresh) pass force=True.
        self._devices_fetched_at = 0.0
        self._watchdog_tasks: dict[str, asyncio.Task] = {}
        self._playing: set[str] = set()
        self._paused: set[str] = set()
        self._stream_urls: dict[str, str] = {}
        self._volumes: dict[str, int] = {}
        # Web-UI physical mute: muted speakers and the level to restore on unmute.
        self._muted: set[str] = set()
        self._pre_mute_volumes: dict[str, int] = {}
        # Serializes cloud commands per speaker so pause/play cannot interleave,
        # and tracks which receiver currently owns each speaker.
        self._locks: dict[str, asyncio.Lock] = {}
        # One slot pool for every cloud call this manager makes: see
        # CLOUD_CONCURRENCY for what unbounded calls do to a slow resolver.
        self._cloud_gate = asyncio.Semaphore(CLOUD_CONCURRENCY)
        # Ownership keys are namespaced per ingress: bare ``receiver_id`` for
        # AirPlay/local sessions, ``dlna:{receiver_id}`` for the DLNA ingress
        # (see DlnaService._owner), and MANUAL_PLAY_OWNER ("debug") for the
        # manual test-tone/URL player.
        self._owners: dict[str, str] = {}
        self.sessions = None
        self._speaker_tokens: dict[str, object] = {}
        self._last_restore: dict[str, float] = {}
        # Speakers that rejected the play command at session start (offline,
        # cloud timeout…). Retried periodically while the session lives.
        self._play_errors: dict[str, dict] = {}
        # Failed-retry count per device, driving the backoff above.
        self._play_error_attempts: dict[str, int] = {}
        # Learned from real HTTP pulls, not cloud command acknowledgements.
        # Kept in memory because firmware updates can change decoder behavior.
        self._codec_capabilities: dict[str, dict[str, bool]] = {}
        self._codec_capability_meta: dict[str, dict[str, dict]] = {}
        self._codec_capability_path = settings.config_path.parent / "xiaomi-codec-capabilities.json"
        self._codec_save_scheduled = False
        self._codec_save_deadline = 0.0
        self._load_codec_capabilities()
        self._error_retry_task: asyncio.Task | None = None
        # Optional ground-truth hook (wired by main): True when the speaker is
        # actually pulling its stream. The cloud reports "playing" even when
        # the speaker fetches nothing, so the watchdog double-checks with this.
        self.stream_active: Callable[[str], bool] | None = None
        # Groups paused because their anchor speaker went offline (keyed by
        # group id). The anchor's own watchdog keeps polling to detect recovery.
        self._anchor_paused: set[str] = set()
        # Wired by main.py: pause/resume the whole group when its anchor drops.
        self.on_anchor_offline: Callable[[str], Awaitable[None]] | None = None
        self.on_anchor_recovered: Callable[[str], Awaitable[None]] | None = None

    @property
    def selected_device_id(self) -> str | None:
        return settings.selected_device_id

    def reset(self) -> None:
        """Drop every cached device/playback state (清空数据 → 回到引导页)."""
        for task in self._watchdog_tasks.values():
            task.cancel()
        self._watchdog_tasks.clear()
        self._devices = []
        self._service = None
        self._playing.clear()
        self._paused.clear()
        self._stream_urls.clear()
        self._volumes.clear()
        self._muted.clear()
        self._pre_mute_volumes.clear()
        self._owners.clear()
        self._last_restore.clear()
        self._play_errors.clear()
        self._play_error_attempts.clear()
        self._codec_capabilities.clear()
        self._codec_capability_meta.clear()
        # Engine restarts land here; an unconditional mkdir+fsync on that hot
        # path is wasteful. The debounced flush covers it (sync fallback when
        # no loop is running), and close() force-flushes before shutdown.
        self._schedule_codec_capability_save()
        self._anchor_paused.clear()
        if self._error_retry_task:
            self._error_retry_task.cancel()
            self._error_retry_task = None

    async def close(self) -> None:
        """Cancel and join every manager-owned background task."""
        # Persist any capability record still waiting in the debounce window.
        self._codec_save_deadline = 0.0
        self._save_codec_capabilities()
        tasks = [task for task in self._watchdog_tasks.values() if not task.done()]
        if self._error_retry_task and not self._error_retry_task.done():
            tasks.append(self._error_retry_task)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._watchdog_tasks.clear()
        self._error_retry_task = None

    @selected_device_id.setter
    def selected_device_id(self, value: str | None) -> None:
        settings.select_device(value)

    def cloud_api(self, device_id: str) -> MinaAPI:
        """A cloud API handle sharing this manager's concurrency slot.

        Every Xiaomi call goes through here so the process-wide bound (see
        CLOUD_CONCURRENCY) cannot be side-stepped by a new call site.
        """
        return MinaAPI(self._service, device_id, gate=self._cloud_gate)

    def cloud_degraded(self) -> bool:
        """True while the cloud is unreachable (see XiaomiAuth.cloud_degraded).

        Everything scheduled in the background checks this before spending a
        round trip: during an outage those calls can only add queue pressure to
        the same resolver the user's next login has to go through.
        """
        # getattr: hand-built instances (tests) may not have an auth at all.
        auth = getattr(self, "auth", None)
        return bool(auth is not None and auth.cloud_degraded())

    async def refresh_service(self) -> bool:
        """Ensure MiNAService is available."""
        # XiaomiAuth replaces its cached service after silent token renewal.
        # Always take its current instance so a long-running DeviceManager
        # never keeps issuing commands with the retired serviceToken.
        service = await self.auth.ensure_service()
        if self._service is not service:
            self._service = service
        return self._service is not None

    async def list_devices(self, force: bool = False) -> list[dict]:
        _, account_id = self.auth.stored_identity()
        settings.bind_provider_account(account_id)
        if (
            not force
            and self._devices
            and (time.monotonic() - self._devices_fetched_at < DEVICE_LIST_CACHE_SECONDS)
        ):
            return self._devices
        if not await self.refresh_service():
            return []
        api = self.cloud_api("")
        try:
            self._devices = await api.device_list()
            self.auth.note_cloud_result(True)
        except Exception as exc:
            self.auth.note_cloud_result(False)
            # Never trust miservice's error text: "Login failed" also wraps
            # pure network errors, and an expired serviceToken can surface as
            # an opaque {"code": ...} body. Ask Xiaomi directly: a rejected
            # passToken kills the login, a working one heals the serviceToken
            # and earns one retry, and an inconclusive check keeps everything.
            logger.warning("device_list failed (%s); verifying login", exc)
            verdict = await self.auth.recover_after_failure()
            if verdict == "rejected":
                self._service = None
                self._devices = []
                raise XiaomiAuthError("小米登录已失效，请重新登录") from exc
            if verdict == "healed" and await self.refresh_service():
                self._devices = await self.cloud_api("").device_list()
            else:
                raise
        self._devices_fetched_at = time.monotonic()
        settings.merge_speakers(self._devices)
        return self._devices

    def consume_merge_rewrite(self) -> bool:
        """True (once) when the last device merge migrated an obsolete device
        id — callers must then rebuild affected pipelines (stream ids still
        reference the old did until they are)."""
        return settings.consume_merge_rewrite()

    def get_alias(self, device_id: str) -> str:
        speaker = settings.get_speaker(device_id)
        if speaker and speaker.alias:
            return speaker.alias
        device = next((d for d in self._devices if d.get("deviceID") == device_id), None)
        if device:
            return device.get("name") or device.get("hardware") or device_id
        return device_id

    def set_alias(self, device_id: str, alias: str) -> None:
        settings.set_alias(device_id, alias)

    def is_enabled(self, device_id: str) -> bool:
        speaker = settings.get_speaker(device_id)
        return speaker.enabled if speaker else False

    def set_enabled(self, device_id: str, enabled: bool) -> None:
        settings.set_enabled(device_id, enabled)

    def select_device(self, device_id: str | None) -> bool:
        if device_id is None:
            self.selected_device_id = None
            return True
        if any(d.get("deviceID") == device_id for d in self._devices):
            self.selected_device_id = device_id
            return True
        return False

    def _lock_for(self, device_id: str) -> asyncio.Lock:
        lock = self._locks.get(device_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[device_id] = lock
        return lock

    def owner_of(self, device_id: str) -> str | None:
        return self._owners.get(device_id)

    def owned_targets(self, receiver_id: str, owner: str) -> list[str]:
        """Xiaomi speakers of `receiver_id` currently owned by `owner`.

        Ownership is an exact-string match. AirPlay/local sessions store the
        bare ``receiver_id``; the DLNA ingress namespaces its owner as
        ``dlna:{receiver_id}`` (see DlnaService._owner) so both can target the
        same group without stealing each other's speakers.
        """
        return [
            did for did in settings.receiver_targets(receiver_id) if self.owner_of(did) == owner
        ]

    def stream_url_of(self, device_id: str) -> str | None:
        return self._stream_urls.get(device_id)

    def anchor_group_of(self, did: str) -> str | None:
        """Return the id of the group whose anchor speaker is `did`, if any."""
        for group in settings.groups:
            if group.anchor_did == did:
                return group.id
        return None

    async def play_stream(
        self,
        device_id: str,
        url: str,
        owner: str | None = None,
        force: bool = False,
        audio_id: str | None = None,
        steal: bool = True,
    ) -> bool:
        """Start a stream on a speaker; serialized per device and idempotent.

        ``owner`` is the receiver driving this playback. A different owner steals
        control (latest AirPlay session wins); repeats from the current owner
        with the same URL are skipped so session churn does not restart audio.
        ``force`` bypasses the idempotency check (used by the watchdog restore).
        ``audio_id`` attaches a Xiaomi library song (cover + lyrics on
        touch-screen speakers).
        """
        async with self._lock_for(device_id):
            if not await self.refresh_service():
                return False
            lease = None
            if self.sessions is not None and owner is not None:
                lease = self.sessions.current(owner)
                if lease is None:
                    if not steal:
                        return False
                    protocol = (
                        "dlna"
                        if owner.startswith("dlna:")
                        else "manual"
                        if owner == MANUAL_PLAY_OWNER
                        else "airplay"
                    )
                    lease = self.sessions.begin(owner, protocol)
                if not self.sessions.valid(lease.token):
                    return False
            current_owner = self._owners.get(device_id)
            if not steal and current_owner not in (None, owner):
                return False
            if (
                not force
                and owner is not None
                and device_id in self._playing
                and self._stream_urls.get(device_id) == url
                and (current_owner is None or current_owner == owner)
            ):
                logger.debug("Speaker %s already playing %s; skipping", device_id, url)
                return True
            if owner is not None and current_owner and current_owner != owner:
                logger.info("Speaker %s ownership: %s -> %s", device_id, current_owner, owner)
            api = self.cloud_api(device_id)
            try:
                await api.play_music_url(url, audio_id=audio_id)
            except Exception:
                logger.warning("play_music_url failed for %s, falling back to play_url", device_id)
                await api.play_url(url)
            if lease is not None and not self.sessions.valid(lease.token):
                # Stop won while the cloud play was in flight; do not publish a
                # resurrected speaker or leave its acknowledged URL running.
                await asyncio.gather(api.pause(), api.stop(), return_exceptions=True)
                return False
            self._playing.add(device_id)
            self._paused.discard(device_id)
            self._stream_urls[device_id] = url
            if owner is not None:
                self._owners[device_id] = owner
            if lease is not None:
                previous_token = self._speaker_tokens.get(device_id)
                if previous_token is not None and previous_token != lease.token:
                    self.sessions.target_taken_over(previous_token, f"speaker:{device_id}")
                self._speaker_tokens[device_id] = lease.token
                self.sessions.targets.record(
                    f"speaker:{device_id}",
                    lease.token,
                    lambda: self.stop_playback(device_id, owner=owner, session_token=lease.token),
                )
                self.sessions.register(
                    lease.token,
                    f"speaker:{device_id}",
                    lambda: self.stop_playback(device_id, owner=owner, session_token=lease.token),
                    kind="speaker",
                )
            self._start_watchdog(device_id)
            return True

    async def search_track(
        self, title: str, artist: str = "", fuzzy_fallback: bool = True
    ) -> dict | None:
        """Search the music library; hit dict {audio_id, cover_url, duration}."""
        if not await self.refresh_service():
            return None
        api = self.cloud_api(next(iter(self._playing), ""))
        return await api.search_track(title, artist, fuzzy_fallback)

    async def search_audio_id(
        self, title: str, artist: str = "", fuzzy_fallback: bool = True
    ) -> str:
        """Search Xiaomi's music library for a song's audioID ("" if no hit)."""
        hit = await self.search_track(title, artist, fuzzy_fallback)
        return hit["audio_id"] if hit else ""

    async def resume(self, device_id: str) -> bool:
        """Resume a paused speaker by re-pushing its stream URL.

        A plain player "play" is acknowledged by the cloud but the speaker
        rarely re-pulls the URL it dropped when paused — it reports "playing"
        while fetching nothing. Our streams are live, so replaying the URL
        simply rejoins the live edge."""
        async with self._lock_for(device_id):
            if not await self.refresh_service():
                return False
            token = self._speaker_tokens.get(device_id)
            if token is not None and self.sessions is not None:
                lease = self.sessions.current(token.owner)
                if (
                    lease is None
                    or lease.token != token
                    or lease.state.value not in ("active", "paused")
                ):
                    return False
                self.sessions.begin(token.owner, lease.protocol, lease.identity)
                self._owners[device_id] = token.owner
            api = self.cloud_api(device_id)
            url = self._stream_urls.get(device_id)
            try:
                if url:
                    await api.play_music_url(url)
                else:
                    await api.play()
            except Exception:
                if not url:
                    raise
                logger.warning("resume failed for %s, re-issuing stream URL", device_id)
                await api.play_url(url)
            self._playing.add(device_id)
            self._paused.discard(device_id)
            self._start_watchdog(device_id)
            return True

    async def stop(self, device_id: str, owner: str | None = None) -> None:
        """Pause a speaker; ignored when a different receiver owns it now."""
        async with self._lock_for(device_id):
            if owner is not None and self._owners.get(device_id) not in (None, owner):
                logger.info(
                    "Ignoring stop from %s on %s: owned by %s",
                    owner,
                    device_id,
                    self._owners.get(device_id),
                )
                return
            if not self._service:
                return
            api = self.cloud_api(device_id)
            try:
                await api.pause()
            except Exception as exc:
                logger.warning("pause failed for %s: %s", device_id, exc)
            self._playing.discard(device_id)
            self._paused.add(device_id)
            self._play_errors.pop(device_id, None)
            self._play_error_attempts.pop(device_id, None)
            if owner is not None and self._owners.get(device_id) == owner:
                self._owners.pop(device_id, None)
            self._stop_watchdog(device_id)

    async def stop_playback(
        self,
        device_id: str,
        owner: str | None = None,
        *,
        keep_error: bool = False,
        session_token=None,
    ) -> None:
        """Fully stop a speaker (not pause) and forget its stream state.

        Unlike pause, this unloads the stream URL — a paused speaker keeps the
        URL and retries it on its own, which leaves ghost connections on the
        stream server long after the phone is gone.

        ``keep_error`` preserves a recorded session-start error (callers that
        stop the speaker BECAUSE of that error want the error to stay visible)."""
        async with self._lock_for(device_id):
            if session_token is not None and self._speaker_tokens.get(device_id) != session_token:
                return
            if owner is not None and self._owners.get(device_id) not in (None, owner):
                logger.info(
                    "Ignoring stop_playback from %s on %s: owned by %s",
                    owner,
                    device_id,
                    self._owners.get(device_id),
                )
                return
            service_ready = await self.refresh_service()
            if session_token is not None and not service_ready:
                raise RuntimeError("Speaker service unavailable during session cleanup")
            if service_ready:
                api = self.cloud_api(device_id)
                # player_stop alone is ignored by some firmware during
                # player_play_music playback; pause actually cuts the audio.
                #
                # Retried: the cloud answers these with an occasional
                # "ubus server internal error ... Timed out waiting 2000.00ms"
                # (field data 0.4.1), and a stop that never landed leaves the
                # speaker playing a stream that is still reachable — connected
                # and silent, which looks like the app is stuck.
                for attempt in range(STOP_COMMAND_ATTEMPTS):
                    failed: list[str] = []
                    for command in (api.pause, api.stop):
                        try:
                            await command()
                        except Exception as exc:
                            failed.append(command.__name__)
                            logger.warning("%s failed for %s: %s", command.__name__, device_id, exc)
                    if not failed:
                        break
                    if attempt + 1 < STOP_COMMAND_ATTEMPTS:
                        await asyncio.sleep(STOP_COMMAND_RETRY_SECONDS * (attempt + 1))
                    else:
                        logger.warning(
                            "Giving up stopping %s after %d attempts (%s)",
                            device_id,
                            STOP_COMMAND_ATTEMPTS,
                            ", ".join(failed),
                        )
                if failed and session_token is not None:
                    raise RuntimeError("Speaker did not acknowledge session cleanup")
            self._playing.discard(device_id)
            self._paused.discard(device_id)
            if not keep_error:
                self._play_errors.pop(device_id, None)
                self._play_error_attempts.pop(device_id, None)
            self._stream_urls.pop(device_id, None)
            self._owners.pop(device_id, None)
            token = self._speaker_tokens.pop(device_id, None)
            if token is not None and self.sessions is not None:
                self.sessions.forget(token, f"speaker:{device_id}")
                self.sessions.targets.forget(f"speaker:{device_id}", token)
            self._stop_watchdog(device_id)

    def playing_ids(self) -> list[str]:
        return list(self._playing)

    def note_play_error(
        self,
        device_id: str,
        receiver_id: str,
        error: str,
        url: str | None = None,
        *,
        retry: bool = True,
    ) -> None:
        """Record a session-start failure so it can be retried and displayed."""
        attempts_map = getattr(self, "_play_error_attempts", None)
        if attempts_map is None:  # tolerate __new__-built instances in tests
            attempts_map = self._play_error_attempts = {}
        self._play_errors[device_id] = {
            "receiver": receiver_id,
            "error": error,
            "url": url or self._stream_urls.get(device_id),
        }
        attempts_map[device_id] = 0
        if retry and (self._error_retry_task is None or self._error_retry_task.done()):
            self._error_retry_task = asyncio.create_task(self._error_retry_loop())

    def clear_play_error(self, device_id: str) -> None:
        self._play_errors.pop(device_id, None)
        self._play_error_attempts.pop(device_id, None)

    def play_errors(self) -> dict[str, str]:
        return {did: entry["error"] for did, entry in self._play_errors.items()}

    @staticmethod
    def canonical_codec_format(fmt: str) -> str | None:
        """Map any recorded/legacy format name onto the canonical one."""
        if not fmt:
            return None
        if fmt in CODEC_FORMATS:
            return fmt
        return LEGACY_CODEC_KEYS.get(fmt)

    def note_codec_capability(
        self, device_id: str, fmt: str, supported: bool, reason: str = "stream_verified"
    ) -> bool | None:
        """Record a format verdict; returns what was stored (None = withheld).

        Withheld only happens for a successful raw-pcm pull, which proves
        nothing (see PCM_UNVERIFIED_REASON): the format keeps no boolean
        verdict, so no group can be told it is "confirmed" for it.
        """
        canonical = self.canonical_codec_format(fmt)
        if canonical is None:
            logger.debug("Ignoring codec capability for unknown format %r", fmt)
            return None
        verdict: bool | None = supported
        if canonical == "pcm" and supported:
            if self._hardware_of(device_id) in PCM_UNSUPPORTED_MODELS:
                verdict, reason = False, PCM_MODEL_UNSUPPORTED_REASON
            else:
                verdict, reason = None, PCM_UNVERIFIED_REASON
        capabilities = self._codec_capabilities.setdefault(device_id, {})
        if verdict is None:
            capabilities.pop(canonical, None)
            status = "unverified"
        else:
            capabilities[canonical] = verdict
            status = "supported" if verdict else "unsupported"
        self._codec_capability_meta.setdefault(device_id, {})[canonical] = {
            "status": status,
            "verified_at": int(time.time()),
            "reason": reason,
            "label": CODEC_LABELS.get(canonical, canonical),
        }
        self._schedule_codec_capability_save()
        return verdict

    def _hardware_of(self, device_id: str) -> str:
        """Native model code for a speaker, "" when it is not known yet.

        settings.speakers carries the hardware the cloud device list reported;
        the live list is the same data, one cache fill later.
        """
        for speaker in settings.speakers:
            if getattr(speaker, "did", None) == device_id:
                return str(getattr(speaker, "hardware", "") or "").upper()
        for device in self._devices or []:
            if str(device.get("deviceID") or "") == device_id:
                return str(device.get("hardware") or "").upper()
        return ""

    def cached_devices(self) -> list[dict]:
        """Last known device list, never a cloud round trip.

        Diagnostics needs the device list on a 1.5s poll and inside the report —
        the exact moments when the cloud may be unreachable. Fetching there made
        the page that explains a failure fail with it.
        """
        return list(self._devices or [])

    def codec_capabilities(self, device_id: str) -> dict[str, bool]:
        return dict(self._codec_capabilities.get(device_id, {}))

    def codec_capability_details(self, device_id: str) -> dict[str, dict]:
        return {
            fmt: dict(meta) for fmt, meta in self._codec_capability_meta.get(device_id, {}).items()
        }

    def _load_codec_capabilities(self) -> None:
        try:
            raw = json.loads(self._codec_capability_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return
            for did, formats in raw.items():
                if not isinstance(formats, dict):
                    continue
                for fmt, meta in formats.items():
                    if not isinstance(meta, dict) or meta.get("status") not in {
                        "supported",
                        "unsupported",
                        "unverified",
                    }:
                        continue
                    canonical = self.canonical_codec_format(str(fmt))
                    if canonical is None:
                        # The retired WAV/PCM alias: unknowable which of the two
                        # it meant, so the record is dropped, not guessed at.
                        continue
                    meta = dict(meta)
                    meta["label"] = CODEC_LABELS.get(canonical, canonical)
                    if canonical == "pcm" and meta["status"] == "supported":
                        # Older builds wrote a "✓" for pcm from the byte pull
                        # alone, which is exactly the claim that turned into a
                        # silent speaker (see PCM_UNVERIFIED_REASON). Re-decide
                        # it on load so an existing file cannot keep showing it.
                        if self._hardware_of(str(did)) in PCM_UNSUPPORTED_MODELS:
                            meta["status"], meta["reason"] = (
                                "unsupported",
                                PCM_MODEL_UNSUPPORTED_REASON,
                            )
                        else:
                            meta["status"], meta["reason"] = "unverified", PCM_UNVERIFIED_REASON
                    self._codec_capability_meta.setdefault(str(did), {})[canonical] = meta
                    if meta["status"] == "unverified":
                        # No verdict to carry: only the record that it was tried.
                        continue
                    supported = meta["status"] == "supported"
                    self._codec_capabilities.setdefault(str(did), {})[canonical] = supported
        except FileNotFoundError:
            return
        except Exception:
            logger.exception("Failed to load persisted Xiaomi codec capabilities")

    def _schedule_codec_capability_save(self) -> None:
        """Debounce disk writes: a session-start probe can record several
        speakers within milliseconds, and each is not worth its own fsync."""
        self._codec_save_deadline = time.monotonic() + 2.0
        if self._codec_save_scheduled:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._save_codec_capabilities()
            return
        self._codec_save_scheduled = True
        loop.call_later(2.0, self._flush_codec_capability_save)

    def _flush_codec_capability_save(self) -> None:
        self._codec_save_scheduled = False
        # A newer record arrived after this flush was scheduled: slide once more.
        remaining = self._codec_save_deadline - time.monotonic()
        if remaining > 0:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                self._save_codec_capabilities()
                return
            self._codec_save_scheduled = True
            loop.call_later(remaining, self._flush_codec_capability_save)
            return
        self._save_codec_capabilities()

    def _save_codec_capabilities(self) -> None:
        try:
            self._codec_capability_path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(self._codec_capability_meta, ensure_ascii=False, indent=2)
            # Same atomic pattern as the main config: a half-written file on
            # NAS storage must never be picked up on the next boot.
            fd, temporary = tempfile.mkstemp(
                prefix=f".{self._codec_capability_path.name}.",
                suffix=".tmp",
                dir=self._codec_capability_path.parent,
                text=True,
            )
            temporary_path = Path(temporary)
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary_path, self._codec_capability_path)
            finally:
                temporary_path.unlink(missing_ok=True)
        except Exception:
            logger.exception("Failed to persist Xiaomi codec capabilities")

    def codec_compatibility(self, device_ids: list[str]) -> dict:
        """Which format a group of speakers can share.

        ``possible`` only excludes formats a member is KNOWN to reject;
        ``confirmed`` requires every member to have actually played it. A
        verdict older than CODEC_CAPABILITY_TTL_SECONDS counts as unknown, so a
        firmware update (or a fix on our side) is never blocked by an ancient
        "unsupported" — and the diagnosis explains itself through
        ``unknown_members`` / ``stale_formats``.
        """
        ids = list(dict.fromkeys(device_ids))
        now = time.time()
        members = {did: self.codec_capabilities(did) for did in ids}
        stale: dict[str, list[str]] = {}
        details = {did: self.codec_capability_details(did) for did in ids}
        for did, meta in details.items():
            aged = [
                fmt
                for fmt, item in meta.items()
                if now - float(item.get("verified_at") or 0) > CODEC_CAPABILITY_TTL_SECONDS
            ]
            if aged:
                stale[did] = sorted(aged)
        known = {
            did: {fmt: value for fmt, value in caps.items() if fmt not in stale.get(did, [])}
            for did, caps in members.items()
        }
        possible = [
            fmt for fmt in CODEC_FORMATS if all(cap.get(fmt) is not False for cap in known.values())
        ]
        confirmed = [
            fmt
            for fmt in CODEC_FORMATS
            if ids and all(cap.get(fmt) is True for cap in known.values())
        ]
        recommended = next(
            (fmt for fmt in CODEC_PREFERENCE if fmt in confirmed),
            None,
        ) or next((fmt for fmt in CODEC_PREFERENCE if fmt in possible), None)
        status = "confirmed" if confirmed else "incompatible" if not possible else "needs_check"
        return {
            "members": members,
            "labels": dict(CODEC_LABELS),
            "formats": list(CODEC_FORMATS),
            "possible_common_formats": possible,
            "confirmed_common_formats": confirmed,
            "recommended_format": recommended,
            "status": status,
            "unknown_members": [did for did, cap in known.items() if not cap],
            "stale_formats": stale,
        }

    async def _error_retry_loop(self) -> None:
        """Re-issue the stream to speakers that failed at session start.

        Backs off exponentially per device and gives up after
        PLAY_ERROR_MAX_ATTEMPTS: a speaker removed from the account or powered
        off for good must not be retried (and cloud-hammered) forever. The
        recorded error is dropped together with the retry state; normal clears
        (session stop, manual stop, successful retry) also prune the counter.
        """
        attempts_map = getattr(self, "_play_error_attempts", None)
        if attempts_map is None:  # tolerate __new__-built instances in tests
            attempts_map = self._play_error_attempts = {}
        while self._play_errors:
            pending = [did for did in self._play_errors if did in attempts_map]
            delays = [
                min(
                    PLAY_ERROR_RETRY_SECONDS
                    * (PLAY_ERROR_BACKOFF_FACTOR ** max(0, attempts_map[did] - 1)),
                    PLAY_ERROR_MAX_BACKOFF_SECONDS,
                )
                for did in pending
            ]
            await asyncio.sleep(min(delays) if delays else PLAY_ERROR_RETRY_SECONDS)
            if self.cloud_degraded():
                # Retrying into an unreachable cloud changes nothing and adds
                # lookups to the resolver pool every other caller waits on.
                continue
            for did, entry in list(self._play_errors.items()):
                url = entry.get("url")
                if not url:
                    self._play_errors.pop(did, None)
                    attempts_map.pop(did, None)
                    continue
                if attempts_map.get(did, 0) >= PLAY_ERROR_MAX_ATTEMPTS:
                    logger.warning(
                        "Speaker %s still not playing after %d retries; "
                        "giving up until the next session",
                        did,
                        PLAY_ERROR_MAX_ATTEMPTS,
                    )
                    self._play_errors.pop(did, None)
                    attempts_map.pop(did, None)
                    continue
                if self._stream_urls.get(did) != url or self._owners.get(did) != entry.get(
                    "receiver"
                ):
                    # Stale entry: the speaker has moved on since the failure
                    # (ownership transferred — the phone switched AirPlay 1/2 —
                    # or the stream URL was re-issued). Replaying the recorded
                    # url with the recorded owner would steal the speaker back
                    # to a dead stream: silent speaker, cloud still "playing".
                    logger.info(
                        "Dropping stale play error for %s: owner/url moved on",
                        did,
                    )
                    self._play_errors.pop(did, None)
                    attempts_map.pop(did, None)
                    continue
                try:
                    await self.recover_play_stream(
                        did, url, owner=entry.get("receiver"), force=True
                    )
                except Exception as exc:
                    logger.debug("Retry of failed speaker %s: %s", did, exc)
                    attempts_map[did] = attempts_map.get(did, 0) + 1
                    continue
                logger.info("Speaker %s recovered after a failed start", did)
                self._play_errors.pop(did, None)
                attempts_map.pop(did, None)

    async def set_volume(self, device_id: str, volume: int) -> int:
        """Set and cache speaker volume after the device accepts it."""
        if not 0 <= volume <= 100:
            raise ValueError("volume must be 0-100")
        if not await self.refresh_service():
            raise XiaomiAuthError("小米登录已失效，请重新登录")
        api = self.cloud_api(device_id)
        await api.set_volume(volume)
        self._volumes[device_id] = volume
        return volume

    async def get_volume(self, device_id: str, refresh: bool = False) -> int | None:
        """Fresh reads never disguise stale cache as the current device level."""
        if not refresh and device_id in self._volumes:
            return self._volumes[device_id]
        if not await self.refresh_service():
            return None if refresh else self._volumes.get(device_id)
        try:
            status = await self.cloud_api(device_id).get_status()
            volume = _find_volume(status)
            if volume is not None:
                self._volumes[device_id] = volume
            return volume if refresh or volume is not None else self._volumes.get(device_id)
        except Exception as exc:
            logger.debug("Unable to read volume for %s: %s", device_id, exc)
            return None if refresh else self._volumes.get(device_id)

    def is_playing(self, device_id: str) -> bool:
        return device_id in self._playing

    def is_paused(self, device_id: str) -> bool:
        return device_id in self._paused

    def is_muted(self, device_id: str) -> bool:
        return device_id in self._muted

    def muted_devices(self) -> list[str]:
        return sorted(self._muted)

    def note_manual_play(self, device_id: str, url: str) -> None:
        """Record playback started outside the stream pipeline (debug tests).

        Unlike play_stream this only *observes*: the watchdog clears the state
        when the speaker goes quiet instead of restoring the URL, so one-shot
        media (test tones) is not replayed in a loop. Existing receiver
        ownership is kept — a manual test does not steal the speaker.
        """
        self._playing.add(device_id)
        self._paused.discard(device_id)
        self._stream_urls.setdefault(device_id, url)
        if self._owners.get(device_id) is None:
            self._owners[device_id] = MANUAL_PLAY_OWNER
        self._start_watchdog(device_id)

    def _start_watchdog(self, device_id: str) -> None:
        task = self._watchdog_tasks.get(device_id)
        if task and not task.done():
            return
        self._watchdog_tasks[device_id] = asyncio.create_task(self._watchdog_loop(device_id))

    def _stop_watchdog(self, device_id: str) -> None:
        task = self._watchdog_tasks.pop(device_id, None)
        if task and not task.done():
            task.cancel()

    async def _watchdog_loop(self, device_id: str) -> None:
        """Restore an active AirPlay stream after voice-assistant interruptions."""
        inactive_checks = 0
        while device_id in self._playing:
            try:
                await asyncio.sleep(STATUS_CHECK_INTERVAL_SECONDS)
                if not self._service:
                    continue
                if self.cloud_degraded():
                    # Polling a cloud that is not answering only adds lookups to
                    # the resolver pool the user's login has to pass through.
                    continue
                api = self.cloud_api(device_id)
                status = await api.get_status()
                logger.debug("Speaker %s status: %s", device_id, status)
                play_status = _find_play_status(status)
                owner = self._owners.get(device_id, "")
                finite_media = owner == MANUAL_PLAY_OWNER or owner.startswith("dlna:")
                media = getattr(getattr(self, "bridge", None), "media_playback", None)
                if media is not None and owner in media.sources:
                    # Common media EOF/hold drain belongs to the session registry.
                    # While decoding, interruptions use the same recovery as live input.
                    finite_media = False
                active = play_status is None or play_status == 1
                if (
                    active
                    and play_status == 1
                    and self.stream_active is not None
                    and not finite_media
                ):
                    try:
                        active = self.stream_active(device_id)
                    except Exception:
                        active = True  # never restore on a checker failure
                if active:
                    inactive_checks = 0
                    anchor_group = self.anchor_group_of(device_id)
                    if (
                        anchor_group is not None
                        and anchor_group in self._anchor_paused
                        and self.on_anchor_recovered is not None
                    ):
                        self._anchor_paused.discard(anchor_group)
                        try:
                            await self.on_anchor_recovered(anchor_group)
                        except Exception:
                            logger.exception("anchor-recovered hook failed for %s", anchor_group)
                    continue
                inactive_checks += 1
                if inactive_checks < 3:
                    continue
                if finite_media:
                    token = self._speaker_tokens.pop(device_id, None)
                    self._playing.discard(device_id)
                    self._owners.pop(device_id, None)
                    self._stream_urls.pop(device_id, None)
                    if token is not None and self.sessions is not None:
                        self.sessions.forget(token, f"speaker:{device_id}")
                        self.sessions.targets.forget(f"speaker:{device_id}", token)
                        lease = self.sessions.current(token.owner)
                        if (
                            lease is not None
                            and lease.token == token
                            and not any(key.startswith(("speaker:", "airplay:", "dlna-target:"))
                                        for key in lease.resources)
                        ):
                            self.sessions.end(token, "media_finished", immediate=True)
                    break
                anchor_group = self.anchor_group_of(device_id)
                if (
                    anchor_group is not None
                    and anchor_group not in self._anchor_paused
                    and self.on_anchor_offline is not None
                ):
                    # The anchor dropped: pause the whole group instead of
                    # restoring just this speaker. Its watchdog keeps polling
                    # and fires the recovered hook when it comes back.
                    self._anchor_paused.add(anchor_group)
                    try:
                        await self.on_anchor_offline(anchor_group)
                    except Exception:
                        logger.exception("anchor-offline hook failed for %s", anchor_group)
                    inactive_checks = 0
                    continue
                url = self._stream_urls.get(device_id)
                if not url or device_id not in self._playing:
                    continue
                if self._owners.get(device_id) == MANUAL_PLAY_OWNER:
                    # One-shot manual playback ended on its own; forget it.
                    self._playing.discard(device_id)
                    self._owners.pop(device_id, None)
                    self._stream_urls.pop(device_id, None)
                    break
                now = time.monotonic()
                if now - self._last_restore.get(device_id, 0.0) < RESTORE_MIN_INTERVAL_SECONDS:
                    continue
                self._last_restore[device_id] = now
                logger.info(
                    "Speaker %s was interrupted (status=%s); restoring AirPlay stream",
                    device_id,
                    play_status,
                )
                await self.recover_play_stream(
                    device_id, url, owner=self._owners.get(device_id), force=True
                )
                inactive_checks = 0
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Watchdog error for %s: %s", device_id, e)

    async def recover_play_stream(self, device_id, url, owner=None, force=True):
        recovery = getattr(self, "recovery", None)
        if recovery is None:
            return await self.play_stream(device_id, url, owner=owner, force=force, steal=False)
        return await recovery.run(
            owner or device_id,
            "speaker",
            lambda: self.play_stream(device_id, url, owner=owner, force=force, steal=False),
        )

    def get_active_targets(self) -> list[dict]:
        """Return devices that should currently output audio.

        In single mode this is the selected device. In multi mode it is all enabled devices.
        """
        if settings.receiver_mode == "multi":
            return [d for d in self._devices if self.is_enabled(d.get("deviceID", ""))]
        selected = self.selected_device_id
        if selected:
            return [d for d in self._devices if d.get("deviceID") == selected]
        return []

    def get_control_targets(self) -> list[dict]:
        """Prefer speakers in the current session for playback controls."""
        if self._playing:
            return [device for device in self._devices if device.get("deviceID") in self._playing]
        if self._paused:
            return [device for device in self._devices if device.get("deviceID") in self._paused]
        return self.get_active_targets()

    def list_targets(self) -> list[dict]:
        """Return devices enriched with alias, enabled, and selected fields."""
        result = []
        for device in self._devices:
            did = device.get("deviceID", "")
            speaker = settings.get_speaker(did)
            result.append(
                {
                    **device,
                    "did": did,
                    "alias": self.get_alias(did),
                    "enabled": speaker.enabled if speaker else False,
                    "selected": did == self.selected_device_id,
                    "play_error": self._play_errors.get(did, {}).get("error"),
                    "codec_capabilities": self.codec_capabilities(did),
                }
            )
        return result


def _find_volume(value) -> int | None:
    """Extract volume from the different MiNA status response shapes."""
    if isinstance(value, str):
        import json

        try:
            return _find_volume(json.loads(value))
        except (json.JSONDecodeError, TypeError):
            return int(value) if value.isdigit() and 0 <= int(value) <= 100 else None
    if isinstance(value, dict):
        for key in ("volume", "volume_level", "volumeLevel"):
            candidate = value.get(key)
            if isinstance(candidate, (int, float)):
                return max(0, min(100, int(candidate)))
            if isinstance(candidate, str) and candidate.isdigit():
                return max(0, min(100, int(candidate)))
        for nested in value.values():
            found = _find_volume(nested)
            if found is not None:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _find_volume(nested)
            if found is not None:
                return found
    return None


def _find_play_status(value) -> int | None:
    """Extract MiNA's player status, including its JSON-encoded ``data.info`` value."""
    if isinstance(value, str):
        import json

        try:
            return _find_play_status(json.loads(value))
        except (json.JSONDecodeError, TypeError):
            return None
    if isinstance(value, dict):
        info = value.get("info")
        if info is not None:
            found = _find_play_status(info)
            if found is not None:
                return found
        status = value.get("status")
        if isinstance(status, int) and not isinstance(status, bool):
            return status
        if isinstance(status, str) and status.isdigit():
            return int(status)
        for key, nested in value.items():
            if key in {"info", "status"}:
                continue
            found = _find_play_status(nested)
            if found is not None:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _find_play_status(nested)
            if found is not None:
                return found
    return None
