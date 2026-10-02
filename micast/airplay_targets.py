"""Lifecycle of external AirPlay targets attached to a receiver's group.

The receiver's PCM tee offers ONE tap reader; a per-receiver hub task fans it
out to every target's personal StreamReader (targets race if they share one
reader). A failing target is downgraded to an error status for the rest of the
session — it must never affect the Xiaomi playback path.
"""

import asyncio
import contextlib
import logging
from array import array

from micast.airplay_discovery import AirPlayDiscovery
from micast.audio_dsp import PCMProcessor, build_audio_filter
from micast.config import settings
from micast.pcm_format import PCMFormat
from micast.pcm_tee import BoundedPCMReader
from micast.raop.alac_encoder import FRAME_SAMPLES, AlacPacketizer
from micast.raop.client import RaopError, RaopSender
from micast.volume import apply_pcm_gain

logger = logging.getLogger(__name__)

RECONNECT_DELAY_SECONDS = 3.0
MAX_ATTEMPTS = 2


def extract_channel(chunk: bytes, side: str) -> bytes:
    """Pull one channel out of interleaved s16 stereo and duplicate it to both
    sides — a target assigned "left" hears the left channel as a full mix."""
    samples = array("h")
    samples.frombytes(chunk[: len(chunk) - (len(chunk) % 2)])
    mono = samples[0::2] if side == "left" else samples[1::2]
    out = array("h", bytes(len(mono) * 4))
    out[0::2] = mono
    out[1::2] = mono
    return out.tobytes()


def _friendly_error(exc: Exception) -> str:
    """Raw socket errors mean little in the UI; translate the common ones."""
    if isinstance(exc, asyncio.TimeoutError):
        return "连接超时（设备无响应，可能已休眠）"
    if isinstance(exc, OSError) and (
        getattr(exc, "winerror", None) == 1225 or getattr(exc, "errno", None) in (61, 111)
    ):
        return "连接被拒绝（设备可能休眠、不在投屏界面，或正被其他设备占用）"
    return str(exc)


class _TargetRuntime:
    def __init__(self, device_id: str, name: str, delay_ms: int = 0, channel: str | None = None):
        self.device_id = device_id
        self.name = name
        self.delay_ms = delay_ms
        self.channel = channel  # None = full mix; "left"/"right" in stereo groups
        self.status = "idle"  # idle | connecting | streaming | error
        self.detail = ""
        self.sender: RaopSender | None = None
        self.task: asyncio.Task | None = None
        self.reader: asyncio.StreamReader | None = None
        # Data flows from the hub only while streaming — otherwise the
        # per-target buffer would fill with stale audio during reconnects.
        self.flowing = False
        self.desired_volume: int | None = None

    def snapshot(self) -> dict:
        stats = self.sender.stats if self.sender else {}
        return {
            "id": self.device_id,
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            **stats,
        }


class _Hub:
    """Fans the receiver's PCM tap out to all of its targets; drains the tap
    even with zero targets so the tee buffer never grows unbounded."""

    def __init__(self, tap_reader: asyncio.StreamReader):
        self.tap_reader = tap_reader
        self.targets: dict[str, _TargetRuntime] = {}
        self.task: asyncio.Task | None = None

    def start(self) -> None:
        if self.task is None:
            self.task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
            self.task = None

    async def _run(self) -> None:
        remainder = b""
        try:
            while True:
                chunk = await self.tap_reader.read(16384)
                if not chunk:
                    for runtime in self.targets.values():
                        if runtime.reader and not runtime.reader.at_eof():
                            runtime.reader.feed_eof()
                    return
                chunk = remainder + chunk
                aligned = len(chunk) - len(chunk) % 4
                chunk, remainder = chunk[:aligned], chunk[aligned:]
                if not chunk:
                    continue
                chunk = apply_pcm_gain(chunk, getattr(self, "input_volume", 100))
                for runtime in self.targets.values():
                    if runtime.flowing and runtime.reader and not runtime.reader.at_eof():
                        runtime.reader.feed_data(chunk)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("AirPlay target hub failed")


class AirPlayTargetManager:
    def __init__(self, discovery: AirPlayDiscovery, sessions=None):
        self._discovery = discovery
        self.sessions = sessions
        self._hubs: dict[str, _Hub] = {}  # receiver_id -> hub
        self._input_volumes: dict[str, int] = {}
        self._device_volumes: dict[str, int] = {}
        self._linked_volumes: dict[str, int] = {}
        self._loudness_levels: dict[str, int] = {}

    def statuses(self) -> dict[str, dict]:
        return {
            receiver_id: {did: runtime.snapshot() for did, runtime in hub.targets.items()}
            for receiver_id, hub in self._hubs.items()
            if hub.targets
        }

    def streaming_targets(self, receiver_id: str) -> list[dict]:
        hub = self._hubs.get(receiver_id)
        if not hub:
            return []
        return [
            runtime.snapshot() for runtime in hub.targets.values() if runtime.status == "streaming"
        ]

    async def start_targets(
        self,
        receiver_id: str,
        device_ids: list[str],
        tap_reader: asyncio.StreamReader,
        delays: dict[str, int] | None = None,
        channels: dict[str, str] | None = None,
        initial_volume: int | None = None,
        sample_rate: int = 44100,
        steal: bool = True,
    ) -> None:
        """Connect and stream to each attached device; running ones keep playing."""
        # `delays` arrives already normalized — non-negative holds sharing the
        # group's single live edge with the Xiaomi pull path (see
        # SpeakerGroupConfig.delay_holds). A smaller value is the pull-earlier
        # path, realized by the reconnect below.
        delays = delays or {}
        channels = channels or {}
        hub = self._hubs.get(receiver_id)
        if hub is not None and hub.tap_reader is not tap_reader:
            await self.stop_targets(receiver_id)
            hub = None
        if hub is None:
            hub = _Hub(tap_reader)
            hub.input_volume = self._input_volumes.get(receiver_id, 100)
            self._hubs[receiver_id] = hub
            hub.start()
        # Detached targets go away; running ones are untouched — unless their
        # delay or channel changed, which requires a reconnect (the pre-buffer
        # lives at the head of the pump, the transform at the hub feed).
        for did in list(hub.targets):
            if did not in device_ids:
                await self._stop_owned(hub.targets.pop(did))
        for did in device_ids:
            if self.sessions is not None:
                lease = self.sessions.current(receiver_id)
                if lease is None or not self.sessions.valid(lease.token):
                    continue
            runtime = hub.targets.get(did)
            delay_ms = int(delays.get(did, 0))
            channel = channels.get(did) if channels.get(did) in ("left", "right") else None
            group = settings.group_for_receiver(receiver_id)
            audio_filter = build_audio_filter(
                settings.speaker_eq_curve(f"airplay:{did}"),
                settings.speaker_loudness(f"airplay:{did}"),
                self._loudness_levels.get(receiver_id, 100),
                channel,
                group.gains_db.get(did, 0) if group else 0,
            )
            if runtime and runtime.status in ("connecting", "streaming"):
                if (
                    runtime.delay_ms == delay_ms
                    and runtime.channel == channel
                    and getattr(runtime, "sample_rate", 44100) == sample_rate
                    and getattr(runtime, "audio_filter", None) == audio_filter
                    and (self.sessions is None or getattr(runtime, "token", None) == lease.token)
                ):
                    continue
                await self._stop_owned(hub.targets.pop(did))
                runtime = None
            device = self._discovery.resolve(did)
            if device is None:
                runtime = runtime or _TargetRuntime(did, did, delay_ms, channel)
                hub.targets[did] = runtime
                runtime.status = "error"
                runtime.detail = "设备不在线（未发现 mDNS 广播）"
                continue
            if device.needs_password:
                runtime = _TargetRuntime(did, device.name, delay_ms, channel)
                hub.targets[did] = runtime
                runtime.status = "error"
                runtime.detail = "设备需要密码，暂不支持"
                continue
            runtime = _TargetRuntime(did, device.name, delay_ms, channel)
            runtime.sample_rate = sample_rate
            runtime.audio_filter = audio_filter
            runtime.desired_volume = self._linked_volumes.get(receiver_id, initial_volume)
            runtime.reader = BoundedPCMReader(bytes_per_second=sample_rate * 4)
            if self.sessions is not None:
                runtime.token = lease.token
                if not await self.sessions.targets.acquire(
                    f"airplay:{did}", lease.token, lambda runtime=runtime: self._stop_one(runtime),
                    steal=steal,
                ):
                    continue
            hub.targets[did] = runtime
            runtime.task = asyncio.create_task(self._run(runtime))

    async def stop_targets(self, receiver_id: str) -> None:
        hub = self._hubs.pop(receiver_id, None)
        if not hub:
            return
        for runtime in list(hub.targets.values()):
            await self._stop_owned(runtime)
        hub.targets.clear()
        await hub.stop()

    async def stop_all(self) -> None:
        for receiver_id in list(self._hubs):
            await self.stop_targets(receiver_id)

    async def set_volume(self, receiver_id: str, percent: int) -> None:
        self._linked_volumes[receiver_id] = percent
        hub = self._hubs.get(receiver_id)
        if not hub:
            return
        for runtime in hub.targets.values():
            runtime.desired_volume = percent
            if runtime.sender and runtime.status == "streaming":
                try:
                    await runtime.sender.set_volume(percent)
                except Exception:
                    logger.exception("Failed to set volume on AirPlay target %s", runtime.name)

    def set_input_volume(self, receiver_id: str, percent: int) -> None:
        self._input_volumes[receiver_id] = percent
        if hub := self._hubs.get(receiver_id):
            hub.input_volume = percent

    def set_loudness_level(self, receiver_id: str, percent: int) -> None:
        self._loudness_levels[receiver_id] = percent
        hub = self._hubs.get(receiver_id)
        if hub is None:
            return
        group = settings.group_for_receiver(receiver_id)
        for runtime in hub.targets.values():
            runtime.audio_filter = build_audio_filter(
                settings.speaker_eq_curve(f"airplay:{runtime.device_id}"),
                settings.speaker_loudness(f"airplay:{runtime.device_id}"),
                percent,
                runtime.channel,
                group.gains_db.get(runtime.device_id, 0) if group else 0,
            )

    def independent_volume(self, receiver_id: str) -> None:
        self._linked_volumes.pop(receiver_id, None)

    async def set_device_volume(self, device_id: str, percent: int) -> int:
        for hub in self._hubs.values():
            runtime = hub.targets.get(device_id)
            if runtime and runtime.sender and runtime.status == "streaming":
                await runtime.sender.set_volume(percent)
                runtime.desired_volume = percent
                self._device_volumes[device_id] = percent
                return percent
        raise ValueError("音箱尚未连接，无法调整音量")

    async def get_volume(self, device_id: str, refresh: bool = False) -> int | None:
        # RAOP has no portable hardware-volume readback. Never invent a
        # physical value from the last command for relative adjustments.
        return None if refresh else self._device_volumes.get(device_id)

    async def _stop_owned(self, runtime: _TargetRuntime) -> None:
        if self.sessions is not None and hasattr(runtime, "token"):
            await self.sessions.targets.execute(
                f"airplay:{runtime.device_id}", runtime.token,
                lambda: self._stop_one(runtime),
            )
        else:
            await self._stop_one(runtime)

    async def _stop_one(self, runtime: _TargetRuntime) -> None:
        if (
            self.sessions is not None
            and hasattr(runtime, "token")
            and not self.sessions.targets.owns(f"airplay:{runtime.device_id}", runtime.token)
        ):
            return
        runtime.flowing = False
        if runtime.task:
            runtime.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await runtime.task
            runtime.task = None
        if runtime.sender:
            try:
                await runtime.sender.teardown()
            except Exception:
                logger.exception("Failed to tear down AirPlay target %s", runtime.name)
            runtime.sender = None
        runtime.status = "idle"
        if self.sessions is not None and hasattr(runtime, "token"):
            self.sessions.targets.forget(f"airplay:{runtime.device_id}", runtime.token)

    async def _run(self, runtime: _TargetRuntime) -> None:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            if (
                self.sessions is not None
                and hasattr(runtime, "token")
                and (
                    not self.sessions.valid(runtime.token)
                    or not self.sessions.targets.owns(f"airplay:{runtime.device_id}", runtime.token)
                )
            ):
                return
            # Re-resolve on every attempt: projectors/TVs re-announce with a
            # fresh ephemeral port after sleep, and retrying the stale address
            # just yields connection-refused.
            device = self._discovery.resolve(runtime.device_id)
            if device is None:
                runtime.detail = "设备不在线（未发现 mDNS 广播）"
                break
            runtime.name = device.name
            sender = RaopSender(device.host, device.port, runtime.name)
            runtime.sender = sender
            runtime.status = "connecting"
            runtime.detail = ""
            try:
                await sender.connect()
                if runtime.desired_volume is not None:
                    await sender.set_volume(runtime.desired_volume)
                runtime.status = "streaming"
                runtime.flowing = True
                logger.info(
                    "AirPlay target %s streaming (%s:%s)", runtime.name, device.host, device.port
                )
                await self._pump(sender, runtime)
                return  # clean EOF: session over, keep status until stopped
            except asyncio.CancelledError:
                raise
            except (TimeoutError, RaopError, OSError) as exc:
                runtime.flowing = False
                runtime.detail = _friendly_error(exc)
                logger.warning(
                    "AirPlay target %s attempt %s failed: %s", runtime.name, attempt, exc
                )
                await sender.close()
                runtime.sender = None
                if attempt < MAX_ATTEMPTS:
                    await asyncio.sleep(RECONNECT_DELAY_SECONDS)
            except Exception:
                logger.exception("AirPlay target %s pump failed", runtime.name)
                await sender.close()
                runtime.sender = None
                break
        runtime.flowing = False
        runtime.status = "error"

    async def _pump(self, sender: RaopSender, runtime: _TargetRuntime) -> None:
        """Forward PCM as it arrives (the phone paces realtime) through the
        ALAC packetizer into RTP packets. A configured delay is realized as a
        PCM pre-buffer: hold back delay_ms worth of audio before sending."""
        reader = runtime.reader
        packetizer = AlacPacketizer()
        source_format = PCMFormat(getattr(runtime, "sample_rate", 44100))
        pending = await self._prebuffer(reader, runtime.delay_ms, source_format)
        if pending is None:
            await sender.flush()
            return
        audio_filter = getattr(runtime, "audio_filter", None)
        resampler = PCMProcessor(source_format, PCMFormat(44100), audio_filter)
        for packet in packetizer.encode(resampler.convert(pending)):
            sender.send_alac(packet, FRAME_SAMPLES)
        while True:
            chunk = await reader.read(16384)
            if not chunk:
                for packet in packetizer.encode(resampler.flush()) + packetizer.flush():
                    sender.send_alac(packet, FRAME_SAMPLES)
                await sender.flush()
                return
            current_filter = getattr(runtime, "audio_filter", None)
            if current_filter != audio_filter:
                for packet in packetizer.encode(resampler.flush()):
                    sender.send_alac(packet, FRAME_SAMPLES)
                audio_filter = current_filter
                resampler = PCMProcessor(source_format, PCMFormat(44100), audio_filter)
            for packet in packetizer.encode(resampler.convert(chunk)):
                sender.send_alac(packet, FRAME_SAMPLES)

    @staticmethod
    async def _prebuffer(
        reader: asyncio.StreamReader, delay_ms: int, pcm_format: PCMFormat | None = None
    ) -> bytes | None:
        """Read delay_ms worth of PCM before letting anything through. Returns
        the buffered bytes, or None on EOF. 44100Hz stereo s16 = 176.4 B/ms."""
        wanted = (pcm_format or PCMFormat()).bytes_for_ms(delay_ms)
        started = asyncio.get_running_loop().time()
        buffered = bytearray()
        while len(buffered) < wanted:
            chunk = await reader.read(min(16384, wanted - len(buffered)))
            if not chunk:
                if buffered and len(buffered) % 4 == 0:
                    remaining = delay_ms / 1000 - (asyncio.get_running_loop().time() - started)
                    if remaining > 0:
                        await asyncio.sleep(remaining)
                    return bytes(buffered)
                return None
            buffered += chunk
        return bytes(buffered)
