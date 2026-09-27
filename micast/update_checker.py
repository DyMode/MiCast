"""GitHub release update checks for MiCast builds.

Every deployment family can CHECK for a new release; only the packaged
Windows exe may download/apply it in-app (fnOS/Docker builds update through
their own channels and only get the notice).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path

import aiohttp

from micast import __version__
from micast.config import default_data_dir, storage_mode
from micast.deployment import update_download_supported
from micast.net import new_session

logger = logging.getLogger(__name__)

GITHUB_REPO = "DyMode/MiCast"
API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
RELEASES_URL = f"https://github.com/{GITHUB_REPO}/releases"

_CHECK_TIMEOUT = aiohttp.ClientTimeout(total=12, connect=8)
_DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(total=None, connect=15, sock_read=60)
_CACHE_TTL = 6 * 3600
# A failed check is cached much shorter: the UI surfaces the error, and the
# user retrying (force=True) or the next poll gets a fresh attempt soon.
_NEGATIVE_CACHE_TTL = 5 * 60

_cache: dict | None = None
# The original exception of the last failed check; replayed (not re-fetched)
# until the negative TTL expires so a GitHub outage cannot be hammered.
_cache_error: BaseException | None = None
_cache_at = 0.0
_cache_lock = asyncio.Lock()
# Serializes actual network fetches so a stampede of force/refresh callers
# still produces one GitHub round-trip at a time.
_fetch_lock = asyncio.Lock()

# Shared with routes/update.py so the status endpoint can report progress.
download_state: dict = {"state": "idle", "progress": 0, "total": 0, "path": None, "error": None}
_download_running = False
_download_task: asyncio.Task | None = None


def parse_version(text: str) -> tuple[int, ...]:
    """'v0.2.10-beta' -> (0, 2, 10); unparseable bits are ignored."""
    parts = re.findall(r"\d+", text)
    return tuple(int(p) for p in parts[:3]) if parts else (0,)


def is_newer(latest: str, current: str) -> bool:
    a, b = parse_version(latest), parse_version(current)
    length = max(len(a), len(b))
    return a + (0,) * (length - len(a)) > b + (0,) * (length - len(b))


def pick_windows_asset(assets: list[dict]) -> dict | None:
    """Choose the asset matching the current Windows install flavor."""
    mode = storage_mode()
    want_zip = mode == "portable"
    candidates = [
        a for a in assets if a.get("name", "").lower().endswith(".zip" if want_zip else ".exe")
    ]
    if not candidates and not want_zip:
        candidates = [a for a in assets if "setup" in a.get("name", "").lower()]
    return candidates[0] if candidates else None


def _cached_fresh() -> bool:
    ttl = _NEGATIVE_CACHE_TTL if _cache_error is not None else _CACHE_TTL
    return (time.time() - _cache_at) < ttl


def _raise_cached_error() -> None:
    """Replay the last failure; chain it so the original traceback is kept."""
    error = _cache_error
    assert error is not None
    if isinstance(error, RuntimeError):
        raise error
    raise RuntimeError(str(error)) from error


async def check_for_update(force: bool = False) -> dict:
    """Return release info vs the running version; cached for 6h.

    Failures are cached for 5 minutes and re-raised from the cache: the UI
    shows the error, no GitHub round-trip happens on every page open, and a
    ``force=True`` call (explicit user retry) always bypasses both caches.
    The network round-trip itself runs OUTSIDE ``_cache_lock`` so a slow or
    dead GitHub cannot block every other status/check call for the timeout;
    ``_fetch_lock`` still serializes fetches to one in flight.
    """
    global _cache, _cache_error, _cache_at
    async with _cache_lock:
        if not force and _cached_fresh():
            if _cache_error is not None:
                _raise_cached_error()
            if _cache:
                return _cache

    async with _fetch_lock:
        # Double-check: another caller may have refreshed the cache while we
        # waited for the fetch slot.
        async with _cache_lock:
            if not force and _cached_fresh():
                if _cache_error is not None:
                    _raise_cached_error()
                if _cache:
                    return _cache
        try:
            async with (
                new_session(timeout=_CHECK_TIMEOUT) as session,
                session.get(API_URL, headers={"Accept": "application/vnd.github+json"}) as resp,
            ):
                if resp.status != 200:
                    raise RuntimeError(f"GitHub 返回 {resp.status}")
                data = await resp.json()

            latest = str(data.get("tag_name") or "").lstrip("v")
            assets = [
                {
                    "name": a.get("name"),
                    "size": a.get("size"),
                    "download_url": a.get("browser_download_url"),
                    # GitHub API exposes "sha256:<hex>" per asset; used to verify
                    # the downloaded file before applying it.
                    "digest": a.get("digest"),
                }
                for a in data.get("assets", [])
            ]
            can_download = update_download_supported()
            result = {
                "current_version": __version__,
                "latest_version": latest,
                "update_available": bool(latest) and is_newer(latest, __version__),
                "release_url": data.get("html_url") or RELEASES_URL,
                "release_notes": (data.get("body") or "")[:2000],
                "published_at": data.get("published_at"),
                "can_download": can_download,
                "asset": pick_windows_asset(assets) if can_download else None,
                "checked_at": int(time.time()),
            }
        except Exception as exc:
            async with _cache_lock:
                _cache, _cache_error, _cache_at = None, exc, time.time()
            raise
        async with _cache_lock:
            _cache, _cache_error, _cache_at = result, None, time.time()
        return result


def _verify_digest(path: Path, digest: str | None) -> None:
    """Check the downloaded file against the asset's sha256 digest."""
    if not digest:
        logger.warning("Release asset has no digest; skipping integrity check")
        return
    algo, _, expected = digest.partition(":")
    if algo.lower() != "sha256" or not expected:
        raise RuntimeError(f"无法识别的校验和格式：{digest}")
    import hashlib

    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual.lower() != expected.lower():
        raise RuntimeError("安装包校验失败（SHA256 不匹配），请重新下载")


async def download_update(asset: dict) -> None:
    """Background download of the release asset into <data dir>/updates."""
    global _download_running
    if _download_running:
        raise RuntimeError("已有下载任务进行中")
    url = asset.get("download_url")
    name = asset.get("name") or "micast-update"
    if not url:
        raise RuntimeError("缺少下载地址")
    _download_running = True
    download_state.update(
        {
            "state": "downloading",
            "progress": 0,
            "total": asset.get("size") or 0,
            "path": None,
            "error": None,
        }
    )
    target_dir = Path(default_data_dir()) / "updates"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / name
    try:
        async with (
            new_session(timeout=_DOWNLOAD_TIMEOUT) as session,
            session.get(url) as resp,
        ):
                if resp.status != 200:
                    raise RuntimeError(f"下载失败（HTTP {resp.status}）")
                total = int(resp.headers.get("Content-Length") or 0) or download_state["total"]
                download_state["total"] = total
                received = 0
                with target.open("wb") as fh:
                    async for chunk in resp.content.iter_chunked(1 << 16):
                        fh.write(chunk)
                        received += len(chunk)
                        download_state["progress"] = received
        _verify_digest(target, asset.get("digest"))
        download_state.update({"state": "done", "path": str(target)})
        logger.info("Update downloaded to %s", target)
    except Exception as e:
        logger.exception("Update download failed")
        download_state.update({"state": "error", "error": str(e)})
        target.unlink(missing_ok=True)
        raise
    finally:
        _download_running = False


def schedule_download(asset: dict) -> bool:
    """Atomically start one managed download task; return False if busy."""
    global _download_task
    if _download_task is not None and not _download_task.done():
        return False
    _download_task = asyncio.create_task(download_update(asset), name="update-download")

    def consume(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error:
            logger.warning("Managed update download failed: %s", error)

    _download_task.add_done_callback(consume)
    return True


def apply_update() -> str:
    """Launch the downloaded installer/zip and exit so files can be replaced."""
    import os
    import subprocess
    import threading

    if download_state.get("state") != "done" or not download_state.get("path"):
        raise RuntimeError("尚未完成下载")
    path = Path(download_state["path"])
    if not path.exists():
        raise RuntimeError("安装包不存在，请重新下载")

    if path.suffix.lower() == ".exe":
        # Detached so it survives this process exiting below.
        subprocess.Popen(  # noqa: S603
            [str(path)],
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            close_fds=True,
        )
    else:
        # Portable zip: just open its folder and let the user swap files.
        os.startfile(path.parent)  # type: ignore[attr-defined]  # noqa: S606

    def _quit():
        os._exit(0)

    threading.Timer(1.5, _quit).start()
    return str(path)
