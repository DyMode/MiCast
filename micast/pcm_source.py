"""PCM source abstraction for AirPlay audio input."""

import asyncio
import logging
import math
import re
import struct
import sys
from abc import ABC, abstractmethod
from collections import deque
from contextlib import suppress

logger = logging.getLogger(__name__)

# The orchestrator feeds PCM over TCP; a connect that never answers must not
# hold up the entry behind it.
PCM_CONNECT_TIMEOUT_SECONDS = 5.0

# Windowed (console=False) packaged builds must not let child processes
# allocate their own console — otherwise every capture command pops a
# terminal window.
SUBPROCESS_KWARGS: dict = {}
if sys.platform == "win32":
    SUBPROCESS_KWARGS["creationflags"] = 0x08000000  # CREATE_NO_WINDOW

SAMPLE_RATE = 48000
SAMPLE_WIDTH = 2  # S16_LE
CHANNELS = 2
FRAME_SIZE = SAMPLE_WIDTH * CHANNELS


def _parse_pcm_source(source: str) -> tuple[str, dict[str, str]]:
    """Parse a source string like 'tcp:192.168.0.12:42800' or 'local:shairport-sync'."""
    if source == "mock":
        return "mock", {}

    match = re.match(r"^(\w+):(.*)$", source)
    if not match:
        raise ValueError(f"Invalid PCM source: {source}")

    kind, rest = match.groups()
    if kind == "tcp":
        if ":" not in rest:
            raise ValueError(f"TCP source must be host:port, got: {rest}")
        host, port_str = rest.rsplit(":", 1)
        return "tcp", {"host": host, "port": port_str}

    if kind == "local":
        return "local", {"command": rest}

    raise ValueError(f"Unsupported PCM source kind: {kind}")


class PCMSource(ABC):
    """Abstract PCM source returning an asyncio StreamReader."""

    @property
    def pcm_format(self):
        from micast.pcm_format import PCMFormat

        return PCMFormat(48000)

    @abstractmethod
    async def start(self) -> asyncio.StreamReader:
        """Start the source and return a StreamReader of raw PCM bytes."""
        ...

    @abstractmethod
    async def stop(self) -> None:
        """Stop the source and release resources."""
        ...


class MockPCMSource(PCMSource):
    """Generate a 48kHz S16_LE stereo sine wave for testing."""

    def __init__(self, frequency: float = 440.0, amplitude: float = 0.3):
        self.frequency = frequency
        self.amplitude = amplitude
        self._task: asyncio.Task | None = None
        self._reader: asyncio.StreamReader | None = None

    async def start(self) -> asyncio.StreamReader:
        self._reader = asyncio.StreamReader()
        self._task = asyncio.create_task(self._generate())
        return self._reader

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
        if self._reader:
            self._reader.feed_eof()

    async def _generate(self) -> None:
        """Feed ~0.5s chunks of sine wave PCM."""
        chunk_frames = SAMPLE_RATE // 2
        phase = 0.0
        try:
            while True:
                frames = bytearray()
                for _ in range(chunk_frames):
                    sample = int(
                        self.amplitude * 32767 * math.sin(2 * math.pi * self.frequency * phase)
                    )
                    sample = max(-32768, min(32767, sample))
                    # Stereo: same sample in both channels, little-endian
                    frames.extend(struct.pack("<hh", sample, sample))
                    phase += 1.0 / SAMPLE_RATE
                    if phase >= 1.0:
                        phase -= 1.0
                self._reader.feed_data(bytes(frames))
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            logger.debug("Mock PCM generator cancelled")
        finally:
            if self._reader:
                self._reader.feed_eof()


class LocalPCMSource(PCMSource):
    """Spawn a local process that outputs PCM on stdout."""

    def __init__(self, command: str, env: dict[str, str] | None = None):
        import uuid

        self.command = command
        # Extra environment for the child (e.g. MICAST_AIRPLAY2_PORT for the
        # bundled shairport launcher); merged over the inherited environment.
        self.epoch = uuid.uuid4().hex
        self.env = {**(env or {}), "MICAST_RECEIVER_EPOCH": self.epoch}
        self._process: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task | None = None
        self._stderr_tail = deque(maxlen=16)

    @property
    def alive(self) -> bool:
        """The child process is still running (so its reader is still fed).

        Plan-only rebuilds reuse a live receiver instead of restarting it; a
        dead process must always be respawned.
        """
        return self._process is not None and self._process.returncode is None

    async def start(self) -> asyncio.StreamReader:
        try:
            return await self._start()
        except BaseException:
            await self.stop()
            raise

    async def _start(self) -> asyncio.StreamReader:
        args = self.command.split()
        from pathlib import Path

        if args and Path(args[0]).name == "run-shairport":
            launcher = Path(args[0])
            if not launcher.is_file():
                raise RuntimeError("AirPlay 2 启动脚本缺失，请重新安装修复版")
            if launcher.read_bytes().split(b"\n", 1)[0] != b"#!/bin/sh":
                raise RuntimeError("AirPlay 2 启动脚本格式错误，请重新安装修复版（需要 LF 换行）")
        ready_path = self.env.get("MICAST_AIRPLAY2_READY_FILE")
        if ready_path:
            Path(ready_path).unlink(missing_ok=True)
        env = None
        if self.env:
            import os  # noqa: PLC0415 — only needed on the spawn path

            env = {**os.environ, **self.env}
        self._process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            **SUBPROCESS_KWARGS,
        )
        if self._process.stdout is None:
            raise RuntimeError("Subprocess stdout is not a pipe")
        self._stderr_tail.clear()
        if self._process.stderr is not None:
            self._stderr_task = asyncio.create_task(self._drain_stderr(self._process.stderr))
        # A local receiver can fail immediately (missing runtime library,
        # unavailable mDNS service, invalid config). Surface that failure
        # instead of marking the pipeline as running with an already-dead
        # process.
        if ready_path:
            # One fixed-port startup, including interpreter and child cleanup.
            deadline = asyncio.get_running_loop().time() + 30
            while self._process.returncode is None and not Path(ready_path).exists():
                if asyncio.get_running_loop().time() >= deadline:
                    await self.stop()
                    raise RuntimeError("AirPlay 2 接收端口或时钟服务未就绪")
                await asyncio.sleep(0.1)
        else:
            await asyncio.sleep(0.25)
        if self._process.returncode is not None:
            if self._stderr_task:
                await self._stderr_task
            detail = b"".join(self._stderr_tail).decode(errors="replace").strip()[-2000:]
            raise RuntimeError(
                f"Local PCM source exited with code {self._process.returncode}"
                + (f": {detail}" if detail else "")
            )
        logger.info("Started local PCM source: %s (pid %s)", self.command, self._process.pid)
        return self._process.stdout

    async def _drain_stderr(self, reader: asyncio.StreamReader) -> None:
        """Keep chatty receiver processes from blocking on a full stderr pipe."""
        try:
            while line := await reader.read(4096):
                self._stderr_tail.append(line)
                logger.debug("PCM source: %s", line.decode(errors="replace").rstrip())
        except asyncio.CancelledError:
            pass

    async def stop(self) -> None:
        if self._process and self._process.returncode is None:
            self._process.terminate()
            try:
                # The bundled supervisor may need two child shutdowns and a
                # stderr-reader join. Do not kill it before it cleans them up.
                timeout = 8 if self.env.get("MICAST_AIRPLAY2_READY_FILE") else 3
                await asyncio.wait_for(self._process.wait(), timeout=timeout)
            except TimeoutError:
                self._process.kill()
                await self._process.wait()
            logger.info("Stopped local PCM source (pid %s)", self._process.pid)
        if self._stderr_task:
            self._stderr_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._stderr_task
            self._stderr_task = None


class TCPPCMSource(PCMSource):
    """Read PCM from a remote TCP socket."""

    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def start(self) -> asyncio.StreamReader:
        last_error: OSError | None = None
        for attempt in range(20):
            try:
                # Bounded connect: an orchestrator that is gone must not leave
                # this source (and the entry behind it) waiting on a socket that
                # will never answer.
                self._reader, self._writer = await asyncio.wait_for(
                    asyncio.open_connection(self.host, self.port), PCM_CONNECT_TIMEOUT_SECONDS
                )
                break
            except (OSError, TimeoutError) as exc:
                last_error = exc if isinstance(exc, OSError) else OSError(str(exc))
                if attempt == 19:
                    raise last_error from None
                await asyncio.sleep(0.5)
        if self._reader is None or self._writer is None:
            raise RuntimeError(f"Could not connect to PCM source: {last_error}")
        logger.info("Connected to TCP PCM source at %s:%s", self.host, self.port)
        return self._reader

    async def stop(self) -> None:
        if self._writer:
            self._writer.close()
            await self._writer.wait_closed()
            logger.info("Disconnected from TCP PCM source %s:%s", self.host, self.port)


class ReaderPCMSource(PCMSource):
    """Wrap an existing StreamReader so an external process can feed it."""

    def __init__(self, reader: asyncio.StreamReader, sample_rate: int = 44100):
        self._reader = reader
        self.sample_rate = sample_rate

    @property
    def pcm_format(self):
        from micast.pcm_format import PCMFormat

        return PCMFormat(self.sample_rate)

    async def start(self) -> asyncio.StreamReader:
        return self._reader

    async def stop(self) -> None:
        # Lifecycle is managed by the owner of the StreamReader.
        pass


def create_pcm_source(source_string: str, env: dict[str, str] | None = None) -> PCMSource:
    """Factory for PCM sources."""
    kind, params = _parse_pcm_source(source_string)
    if kind == "mock":
        return MockPCMSource()
    if kind == "tcp":
        return TCPPCMSource(params["host"], int(params["port"]))
    if kind == "local":
        return LocalPCMSource(params["command"], env=env)
    raise ValueError(f"Unknown PCM source kind: {kind}")
