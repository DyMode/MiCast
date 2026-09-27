"""Probe which audio formats a speaker really accepts.

Two callers share this: the diagnostics page's "测试音频格式" button (audible
fixtures, all four formats, the user asked for it) and the background probe that
fills the capability table while nothing is playing (silent fixtures, framed
formats only).

"Supported" is never inferred from the cloud accepting a play command — that
says nothing. A fixture is served over the same HTTP port as the live stream and
the bytes the speaker actually reads are sampled twice: a decoder that rejects
the payload reads a burst and gives up, which request counting cannot tell from
healthy playback.
"""

from __future__ import annotations

import asyncio
import io
import logging
import secrets
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

import av

from micast.config import settings

logger = logging.getLogger(__name__)

# One fixture per format the app can serve. "pcm" is raw PCM (never
# transcoded): its support cannot be established remotely at all — a speaker
# that discards raw passthrough keeps reading the stream at full rate — so only
# the explicit, audible test probes it, and the background probe leaves it alone
# (see DeviceManager.note_codec_capability).
FIXTURE_SPECS: dict[str, tuple[str, str, str, bool]] = {
    "mp3": ("mp3", "libmp3lame", "audio/mpeg", True),
    "flac": ("flac", "flac", "audio/flac", True),
    "wav": ("wav", "pcm_s16le", "audio/wav", True),
    "pcm": ("wav", "pcm_s16le", "audio/wav", False),
}

# A pull shorter than this proves nothing about support; the same floor the
# live-playback verdict uses.
MIN_PULL_BYTES = 24_000
# How long one fixture gets to be read, in two samples.
SAMPLE_SECONDS = 2.0


def probe_directory() -> Path:
    """Where probe fixtures are written (rebuilt on every app start)."""
    directory = Path(tempfile.gettempdir()) / "micast-codec-probe"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def build_fixtures(
    stream_server,
    source_wav: bytes,
    directory: Path,
    prefix: str = "codec",
) -> dict[str, tuple[str, Path, str]]:
    """Register one fixture per format on the stream server; cached by caller."""
    directory.mkdir(parents=True, exist_ok=True)
    rate = int(settings.audio.sample_rate or 44100)
    fixtures: dict[str, tuple[str, Path, str]] = {}
    for fmt, (extension, codec, media_type, transcode) in FIXTURE_SPECS.items():
        token = f"{prefix}-{fmt}-{secrets.token_urlsafe(6)}"
        path = directory / f"{token}.{extension}"
        if transcode:
            with av.open(io.BytesIO(source_wav), mode="r") as source, av.open(
                str(path), mode="w", format=extension
            ) as target:
                stream = target.add_stream(codec, rate=rate)
                stream.layout = "stereo"
                for frame in source.decode(audio=0):
                    frame.sample_rate = rate
                    for packet in stream.encode(frame):
                        target.mux(packet)
                for packet in stream.encode(None):
                    target.mux(packet)
        else:
            path.write_bytes(source_wav)
        stream_server.register_diagnostic_media(token, path, media_type)
        fixtures[fmt] = (token, path, media_type)
    return fixtures


def fixture_url(token: str) -> str:
    """The URL a speaker must fetch; a cache-buster keeps firmware from reusing
    a stale response for the previous format."""
    return (
        f"http://{settings.effective_stream_host}:{settings.stream_port}"
        f"/diagnostic/media/{token}?probe={time.time_ns()}"
    )


async def probe_format(
    stream_server,
    device_manager,
    device_id: str,
    token: str,
    url: str,
    owner: str,
    expected_bytes: int = 0,
) -> bool | None:
    """Whether the speaker took this format — None when the probe proved nothing.

    True: the pull was still growing at the second sample (a decoder playing the
    file), or the speaker took essentially the whole file (a fast device can
    finish an 8-second fixture inside the first sample window, and "read all of
    it" is not evidence of giving up).

    False: the speaker fetched the URL and stopped after a prefix — it is not
    decoding this payload. Its byte count is both static and a small fraction of
    the file, so it fails both tests.

    None: the play command never landed (cloud hiccup, speaker offline). That is
    not a statement about the format, and recording it as "unsupported" would
    hide a working format for a day and mislead the group format advice.
    """
    before = stream_server.diagnostic_bytes(token)
    try:
        accepted = await device_manager.play_stream(
            device_id, url, owner=owner, force=True, audio_id=str(time.time_ns())
        )
        if accepted is False:
            return None
        await asyncio.sleep(SAMPLE_SECONDS)
        first = stream_server.diagnostic_bytes(token)
        await asyncio.sleep(SAMPLE_SECONDS)
        total = stream_server.diagnostic_bytes(token)
        pulled = total - before
        still_reading = (total - first) > 0 or pulled >= 8 * MIN_PULL_BYTES
        complete = expected_bytes > 0 and pulled >= expected_bytes * 0.9
        return still_reading or complete
    except Exception as exc:
        logger.info("格式检测失败 %s: %s", device_id, exc)
        return None


async def probe_device_formats(
    stream_server,
    device_manager,
    device_id: str,
    fixtures: dict[str, tuple[str, Path, str]],
    owner: str,
    formats: list[str] | None = None,
    should_continue: Callable[[], bool] | None = None,
    reason: str = "active_probe",
) -> dict[str, bool | None]:
    """Probe the given formats and record every verdict.

    Returns what was recorded per format — `None` means the probe ran but the
    verdict is withheld (raw pcm), so callers never mistake it for a "yes".
    `should_continue` lets a caller abort between formats, which is what keeps
    the background probe from fighting a playback that just started.
    """
    results: dict[str, bool | None] = {}
    for fmt in formats or list(fixtures):
        if should_continue is not None and not should_continue():
            break
        entry = fixtures.get(fmt)
        if entry is None:
            continue
        token, path, _media_type = entry
        try:
            expected_bytes = path.stat().st_size
        except OSError:
            expected_bytes = 0
        supported = await probe_format(
            stream_server,
            device_manager,
            device_id,
            token,
            fixture_url(token),
            owner,
            expected_bytes=expected_bytes,
        )
        if supported is None:
            # Nothing proven: keep whatever the table already says.
            results[fmt] = None
        else:
            results[fmt] = device_manager.note_codec_capability(
                device_id, fmt, supported, reason
            )
        await device_manager.stop_playback(device_id, owner=owner)
    return results
