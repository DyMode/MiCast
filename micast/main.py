"""FastAPI application entrypoint."""

import asyncio
import logging
import os
import sys
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from micast import __version__
from micast.access import COOKIE_NAME, AccessManager
from micast.audio_bridge import AudioBridge
from micast.audio_metrics import run_runtime_monitor
from micast.audio_supervisor import AudioSupervisor
from micast.config import default_runtime_dir, settings
from micast.config_store import write_json
from micast.deployment import airplay2_mode
from micast.dlna import DlnaService
from micast.media_playback import MediaPlayback
from micast.notify import Notifier, notify_expired_soon
from micast.paths import APP_BASE_PATH
from micast.playback_orchestrator import PlaybackOrchestrator
from micast.raop import server as raop_server
from micast.routes import (
    access,
    airplay2,
    airplay_devices,
    config,
    debug,
    devices,
    dlna,
    dlna_devices,
    playback,
    receivers,
    status,
    topology,
    tuning,
    update,
    ws,
    xiaomi,
)
from micast.runtime_log import install_asyncio_exception_filter, install_runtime_log
from micast.runtime_tasks import RuntimeTasks
from micast.track_metadata import TrackMetadataRegistry
from micast.xiaomi.auth import XiaomiAuth
from micast.xiaomi.device_manager import DeviceManager


async def _run_audio_supervisor(supervisor: AudioSupervisor) -> None:
    """Drive the supervisor's ladder for the lifetime of the app."""
    while True:
        await asyncio.sleep(2.0)
        await supervisor.tick()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan events."""
    install_asyncio_exception_filter(asyncio.get_running_loop())
    settings.configure_airplay2_deployment(airplay2_mode())
    # Speakers pull audio from stream_port; if it's taken and the user didn't
    # pin it explicitly, slide to a free one rather than failing the session.
    raop_server.configure_ports(settings.airplay_rtsp_port, settings.airplay_udp_base)

    def expiry_notice() -> None:
        notify_expired_soon(app.state.notifier)

    app.state.auth.subscribe_expiry(expiry_notice)
    bridge: AudioBridge = app.state.bridge
    device_manager: DeviceManager = app.state.device_manager
    dlna_service: DlnaService = app.state.dlna
    bridge.dlna_service = dlna_service
    runtime_tasks = RuntimeTasks()
    orchestrator = None
    # receiver_id -> matched audioID (for /api/status now-playing display)
    bridge.lyrics_matched = {}
    # Session-scoped now-playing enrichment (library cover/duration); shared
    # between the lyrics matcher (writer), the status payload (reader) and the
    # cover endpoint (serving/redirect decision).
    track_metadata = TrackMetadataRegistry()
    bridge.track_metadata = track_metadata

    async def verify_saved_xiaomi_login() -> None:
        # Do not rely on the embedded webview's visibility state to discover
        # expiry. FNOS desktop containers can report themselves hidden even
        # while the app is visibly open.
        has_credentials, user_id = app.state.auth.stored_identity()
        if not has_credentials:
            state = app.state.auth.connection_state()
            logger.warning(
                "Xiaomi startup check: no readable credentials; status=%s, configured_speakers=%s",
                state["status"],
                len(settings.speakers),
            )
            return
        logger.info("Xiaomi startup check: validating saved account %s", user_id)
        try:
            verdict = await app.state.auth.recover_after_failure()
            logger.info("Xiaomi startup check completed: %s", verdict)
        except Exception:
            logger.exception("Initial Xiaomi login verification failed")

    start_background = runtime_tasks.start

    try:
        start_background(verify_saved_xiaomi_login(), name="verify-xiaomi-login")
        # Runtime health for the diagnostics page: CPU load of this process and how
        # late the event loop runs its own timer. A stutter report with every drop
        # counter at zero needs these two numbers to tell "the box is saturated"
        # from "the sender delivers in lumps".
        start_background(run_runtime_monitor(), name="runtime-monitor")

        orchestrator = PlaybackOrchestrator(
            bridge,
            device_manager,
            lambda coro, name: start_background(coro, name=name),
            track_metadata=track_metadata,
        )
        orchestrator.attach()
        # Routes reach the group-membership reconciler through app.state.
        app.state.reconcile_group = orchestrator.reconcile_group
        # Learn what formats each speaker accepts while the house is quiet. Starting
        # this here (rather than only after a session ends) means a fresh install
        # fills the table in without anyone pressing "test formats".
        orchestrator.schedule_codec_probe(delay=90.0)

        # One authority decides whether each entry is delivering audio and drives
        # the recovery ladder; see micast/audio_supervisor.py for why the previous
        # six independent observers were replaced.
        bridge.attach_device_manager(device_manager)
        supervisor = AudioSupervisor(bridge, device_manager)
        bridge.attach_supervisor(supervisor)
        app.state.audio_supervisor = supervisor
        start_background(_run_audio_supervisor(supervisor), name="audio-supervisor")

        await bridge.start()
        if os.environ.get("MICAST_UNIX_SOCKET", "").strip():
            from micast.dlna_listener import DlnaHttpListener

            app.state.dlna_listener = DlnaHttpListener(dlna_service)
            dlna_service.http_listener = app.state.dlna_listener
            if settings.dlna_enabled:
                await app.state.dlna_listener.start()
        await dlna_service.start()
        runtime_file = default_runtime_dir() / "runtime-ports.json"
        write_json(
            runtime_file,
            {
                "pid": os.getpid(),
                "port": settings.port,
                "stream_port": settings.stream_port,
            },
        )
        # Silently rotate Xiaomi serviceTokens in the background so the stored
        # passToken keeps the login alive past the ~30-day serviceToken expiry.
        start_background(app.state.auth.run_token_renewal(), name="xiaomi-token-renewal")
        yield
    finally:
        app.state.auth.unsubscribe_expiry(expiry_notice)
        async with AsyncExitStack() as cleanup:
            cleanup.push_async_callback(bridge.stop)
            cleanup.push_async_callback(app.state.notifier.close)
            cleanup.push_async_callback(app.state.auth.close)
            if getattr(app.state, "dlna_listener", None):
                cleanup.push_async_callback(app.state.dlna_listener.stop)
            cleanup.push_async_callback(dlna_service.stop)
            cleanup.push_async_callback(device_manager.close)
            if orchestrator:
                cleanup.push_async_callback(orchestrator.stop_all)
            cleanup.push_async_callback(runtime_tasks.close)


logger = logging.getLogger(__name__)
install_runtime_log()


app = FastAPI(
    title="MiCast",
    description="让智能音箱获得 AirPlay 2 接收能力",
    version=__version__,
    lifespan=lifespan,
)

# Shared state
app.state.bridge = AudioBridge()
app.state.access = AccessManager()
app.state.auth = XiaomiAuth()
app.state.device_manager = DeviceManager(app.state.auth)
app.state.device_manager.bridge = app.state.bridge
app.state.media_playback = MediaPlayback(app.state.bridge, app.state.device_manager)
app.state.bridge.media_playback = app.state.media_playback
app.state.dlna = DlnaService(
    app.state.device_manager, app.state.bridge.sessions, app.state.media_playback
)
app.state.bridge.dlna_service = app.state.dlna
app.state.device_manager.sessions = app.state.bridge.sessions
app.state.bridge.stream_server.sessions = app.state.bridge.sessions
app.state.bridge.stream_server.media_volume = app.state.dlna.media_volume
app.state.bridge.stream_server.media_session = app.state.dlna.media_token
app.state.notifier = Notifier()


def _stream_active_for(device_id: str) -> bool:
    from micast.stream_health import stream_active_for

    return stream_active_for(app.state.bridge, app.state.device_manager, device_id)


app.state.device_manager.stream_active = _stream_active_for

# API routes. /app/micast is canonical on every platform. The unprefixed
# aliases are transitional compatibility for older clients and cached pages.
api_routers = [
    access.install(app.state.access),
    config.install(
        app.state.bridge, app.state.dlna, app.state.auth, app.state.access, app.state.device_manager
    ),
    status.install(app.state.bridge),
    receivers.install(
        app.state.bridge,
        app.state.dlna,
        app.state.device_manager,
    ),
    devices.install(app.state.device_manager, app.state.bridge),
    xiaomi.install(app.state.auth),
    playback.install(app.state.bridge, app.state.device_manager),
    debug.install(app.state.bridge, app.state.device_manager),
    dlna.install(app.state.dlna),
    airplay2.install(app.state.bridge),
    airplay_devices.install(app.state.bridge),
    dlna_devices.install(app.state.bridge),
    topology.install(app.state.bridge, app.state.device_manager),
    tuning.install(app.state.bridge, app.state.device_manager),
    update.install(),
    ws.install(app.state.bridge, app.state.device_manager, app.state.access),
]
for api_router in api_routers:
    app.include_router(api_router, prefix=APP_BASE_PATH)
    app.include_router(api_router, include_in_schema=False)


@app.middleware("http")
async def require_local_access(request: Request, call_next):
    """Protect every API after setup while keeping the SPA and bootstrap API reachable."""
    path = request.url.path
    logical_path = path.removeprefix(APP_BASE_PATH) if path.startswith(APP_BASE_PATH) else path
    # Speakers fetch the local calibration tone themselves and cannot carry a
    # browser session cookie.  This read-only finite audio asset is safe to
    # expose on the LAN; every control endpoint remains authenticated.
    public = (
        logical_path == "/health"
        or logical_path == "/ready"
        or logical_path == "/api/debug/test-tone"
        or (request.method == "GET" and logical_path.startswith("/api/debug/media/"))
        or logical_path.startswith("/api/access/")
        or (
            request.method == "POST"
            and logical_path
            in {
                "/api/playback/start",
                "/api/playback/session/stop",
                "/api/playback/session/volume",
            }
        )
    )
    manager: AccessManager = app.state.access
    if (
        logical_path.startswith("/api/")
        and not public
        and (
            not manager.access_configured
            or (
                manager.auth_enabled and not manager.valid_session(request.cookies.get(COOKIE_NAME))
            )
        )
    ):
        return JSONResponse({"detail": "需要登录 MiCast"}, status_code=401)
    return await call_next(request)


@app.get(f"{APP_BASE_PATH}/health", tags=["system"])
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health", include_in_schema=False)
async def legacy_health() -> dict[str, str]:
    return await health()


@app.get(f"{APP_BASE_PATH}/ready", tags=["system"])
async def readiness():
    """Report whether the audio core can currently accept sessions."""
    state = app.state.bridge.status.get("status", "error")
    ready = state not in {"error", "idle", "stopping"}
    return JSONResponse(
        status_code=200 if ready else 503,
        content={"status": "ready" if ready else "not_ready", "audio": state},
    )


@app.get("/ready", include_in_schema=False)
async def legacy_readiness():
    return await readiness()


@app.get(f"{APP_BASE_PATH}/api/desktop/show", include_in_schema=False)
@app.get("/api/desktop/show", include_in_schema=False)
async def desktop_show() -> dict[str, bool]:
    """Raise the desktop window; a second-instance launch lands here.

    Lives in main.py (not desktop.py) because the SPA catch-all below swallows
    any route registered after import time. The callback is installed by
    micast.desktop in packaged desktop mode; elsewhere it's a no-op.
    """
    fn = getattr(app.state, "desktop_show", None)
    if fn:
        fn()
    return {"ok": True}


# Static web UI. Packaged (PyInstaller) builds carry web/dist inside the
# bundle (_MEIPASS); source checkouts resolve it from the repo root. The fnOS
# package imports micast from vendor/ but lays the built UI directly in
# <appdest>/web/ (no dist/ level), and cmd/main cd's into the app dir — so
# probe every plausible location instead of assuming one layout.
project_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
web_root = project_root / "web"
web_dist = web_root / "dist"
for candidate in (
    web_dist,
    web_root,
    Path.cwd() / "web" / "dist",
    Path.cwd() / "web",
    project_root.parent / "web" / "dist",
    project_root.parent / "web",
):
    if (candidate / "index.html").exists():
        web_root = candidate.parent if candidate.name == "dist" else candidate
        web_dist = candidate
        break
if web_dist.exists():
    app.mount(
        f"{APP_BASE_PATH}/assets",
        StaticFiles(directory=str(web_dist / "assets")),
        name="app-assets",
    )
    app.mount(
        f"{APP_BASE_PATH}/icons",
        StaticFiles(directory=str(web_dist / "icons")),
        name="app-icons",
    )
    # Cached pre-prefix pages may still request these during migration.
    app.mount("/assets", StaticFiles(directory=str(web_dist / "assets")), name="legacy-assets")
    app.mount("/icons", StaticFiles(directory=str(web_dist / "icons")), name="legacy-icons")

    @app.get(f"{APP_BASE_PATH}/site.webmanifest", include_in_schema=False)
    @app.get("/site.webmanifest", include_in_schema=False)
    async def web_manifest() -> FileResponse:
        return FileResponse(
            str(web_dist / "site.webmanifest"),
            media_type="application/manifest+json",
        )

    @app.get("/")
    async def root() -> FileResponse:
        """Serve the SPA when a reverse proxy strips APP_BASE_PATH.

        fnOS' Unix-socket gateway publishes the app at APP_BASE_PATH but
        forwards the remaining path ("/") to the package process.  Serving
        the document here avoids redirecting the browser back through the
        public prefix a second time.
        """
        return FileResponse(str(web_dist / "index.html"))

    @app.get(APP_BASE_PATH, include_in_schema=False)
    async def app_root_without_slash() -> RedirectResponse:
        return RedirectResponse(f"{APP_BASE_PATH}/", status_code=307)

    @app.get(f"{APP_BASE_PATH}/")
    async def app_root() -> FileResponse:
        index_file = web_dist / "index.html" if web_dist.exists() else web_root / "index.html"
        return FileResponse(str(index_file))

    @app.get(f"{APP_BASE_PATH}/{{path:path}}", include_in_schema=False)
    async def spa_fallback(path: str) -> FileResponse:
        """Serve the SPA for browser routes while never masking missing assets/API calls."""
        if path.startswith(("api/", "assets/")) or "." in Path(path).name:
            from fastapi import HTTPException

            raise HTTPException(status_code=404, detail="Not found")
        return FileResponse(str(web_dist / "index.html"))
