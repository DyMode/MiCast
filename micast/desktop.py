"""Desktop shell for packaged builds: native WebView2 window + system tray.

MiCast is a background service, so closing the window must NOT kill the
bridge — the window hides to the tray instead; the tray menu is the only
way out. Falls back to browser + tray if WebView2 is missing
(pre-2023 Windows 10 without the runtime).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

import uvicorn

from micast.config import default_log_dir, default_runtime_dir, settings

logger = logging.getLogger(__name__)

_LOCK_NAME = "micast.lock"


def _lock_path():
    return default_runtime_dir() / _LOCK_NAME


def _running_instance_port() -> int | None:
    """Port of an already-running desktop instance, or None.

    The lock file is advisory: a stale one (crashed/killed process) is
    detected via os.kill(pid, 0) and ignored.
    """
    try:
        data = json.loads(_lock_path().read_text(encoding="utf-8"))
        pid, port = int(data["pid"]), int(data["port"])
    except Exception:
        return None
    try:
        os.kill(pid, 0)
    except OSError:
        return None
    return port


def _claim_instance(port: int) -> None:
    with contextlib.suppress(OSError):
        _lock_path().parent.mkdir(parents=True, exist_ok=True)
        _lock_path().write_text(json.dumps({"pid": os.getpid(), "port": port}), encoding="utf-8")


def _acquire_singleton_mutex():
    """OS-level single-instance guard, held for the process lifetime.

    Unlike the lock file (written only after the server is up), the mutex
    exists from the very first moment — two rapid double-clicks can't race
    past it. Returns the handle, or None if another instance holds it.
    """
    if sys.platform != "win32":
        return True  # non-Windows skips the guard for now
    import ctypes

    # use_last_error is essential: plain windll calls clobber the thread's
    # last-error code, so GetLastError() would always read 0 and every
    # launch would think it's the first instance.
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.CreateMutexW(None, True, "Local\\MiCastDesktopSingleton")
    if not handle:
        return True  # couldn't create — don't block startup over this
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(handle)
        return None
    return handle


def _install_file_logging() -> None:
    """Windowed builds have no console — logs go to <data dir>/micast.log."""
    log_path = default_log_dir() / "micast.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger().addHandler(handler)


def _brand_asset(*parts: str) -> Path:
    """Resolve generated brand assets in source and single-file builds."""
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
    return root.joinpath("assets", "icons", *parts)


def _make_icon_image():
    """Load the approved MiCast brand icon for the system tray."""
    from PIL import Image

    with Image.open(_brand_asset("windows", "micast-64.png")) as source:
        return source.convert("RGBA")


def run_desktop() -> None:
    import webview  # pywebview; WebView2 backend on Windows
    from pystray import Icon, Menu, MenuItem

    _install_file_logging()

    # Single instance: the mutex is atomic from process start; a second launch
    # raises the existing window (via its canonical desktop endpoint) and exits.
    _mutex = _acquire_singleton_mutex()
    if _mutex is None:
        existing_port = None
        for _ in range(50):  # first instance may still be starting up
            existing_port = _running_instance_port()
            if existing_port:
                break
            time.sleep(0.2)
        logger.info("已有实例在运行（端口 %s），唤起其窗口后退出", existing_port)
        if existing_port:
            try:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{existing_port}/app/micast/api/desktop/show",
                    timeout=2,
                )
            except Exception:
                webbrowser.open(f"http://127.0.0.1:{existing_port}/app/micast/")
        return

    from micast.main import app  # noqa: PLC0415 — after logging is set up

    config = uvicorn.Config(app, host=settings.host, port=settings.port, log_level="info",
                            use_colors=False if sys.stdout is None else None)
    server = uvicorn.Server(config)
    from micast.ports import reserve_tcp

    lease = reserve_tcp(settings.preferred_port("port"), settings.host,
                        strict=settings.port_is_strict("port"))
    settings.apply_resolved_port("port", lease.port)
    config.port = lease.port
    server_thread = threading.Thread(
        target=server.run, kwargs={"sockets": [lease.socket]}, daemon=True
    )
    server_thread.start()
    for _ in range(100):  # wait until uvicorn accepts connections
        if server.started:
            break
        time.sleep(0.1)
    _claim_instance(settings.port)

    url = f"http://127.0.0.1:{settings.port}/app/micast/"

    shutting_down = False

    class _DesktopApi:
        """JS bridge: the in-page close dialog calls these."""

        def desktop_quit(self):
            quit_app()

        def desktop_hide(self):
            window.hide()

    window = webview.create_window(
        "MiCast",
        url,
        width=1120,
        height=780,
        min_size=(820, 600),
        js_api=_DesktopApi(),
    )
    app.state.desktop_show = lambda: window.show()

    def quit_app():
        nonlocal shutting_down
        shutting_down = True
        icon.stop()
        server.should_exit = True
        with contextlib.suppress(OSError):
            _lock_path().unlink(missing_ok=True)
        with contextlib.suppress(Exception):
            window.destroy()  # unblocks webview.start() on the main thread
        # destroy() is invoked off the GUI thread; if the loop ignores it the
        # process would linger invisibly — force-exit almost immediately.
        threading.Timer(0.3, lambda: os._exit(0)).start()

    prompt_open = False

    def on_closing():
        # The X never closes directly: ask via the in-page styled dialog
        # (退出 / 收进托盘 / 取消). destroy() during a confirmed quit fires
        # this event again — shutting_down lets that one through.
        nonlocal prompt_open
        if shutting_down:
            return None
        if prompt_open:
            return False
        prompt_open = True

        def prompt():
            nonlocal prompt_open
            try:
                handled = bool(
                    window.evaluate_js(
                        "Boolean(window.micastClosePrompt && (window.micastClosePrompt(), true))"
                    )
                )
            except Exception:
                handled = False
            prompt_open = False
            if not handled:
                # Page not ready (still loading) — just park in the tray.
                window.hide()

        threading.Thread(target=prompt, daemon=True).start()
        return False

    window.events.closing += on_closing

    icon = Icon(
        "micast",
        _make_icon_image(),
        "MiCast",
        menu=Menu(
            MenuItem("打开 MiCast", lambda: window.show(), default=True),
            MenuItem("退出 MiCast", lambda: quit_app()),
        ),
    )
    threading.Thread(target=icon.run, daemon=True).start()

    try:
        webview.start()
    except Exception:
        # No usable WebView2 — degrade to browser + tray only.
        logger.exception("WebView2 不可用，回退到浏览器模式")
        webbrowser.open(url)
        server_thread.join()
        return

    # Window destroyed via the tray's 退出 — shut everything down cleanly.
    icon.stop()
    server.should_exit = True
    server_thread.join(timeout=5)
