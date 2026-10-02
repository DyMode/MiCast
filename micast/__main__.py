"""`python -m micast` — run the MiCast server without a separate uvicorn call.

Shared by source checkouts and the PyInstaller-packaged app (frozen builds
import the app object directly; an import string would not survive freezing).
Frozen Windows builds get the desktop shell (WebView2 window + tray);
MICAST_NO_DESKTOP=1 forces plain server mode (useful for debugging).
"""

import os
import sys
import threading
import webbrowser
from pathlib import Path

import uvicorn

from micast.config import EDITABLE_PORTS, env_pinned, settings
from micast.ports import reserve_tcp


def main() -> None:
    if "--preflight-if-new" in sys.argv:
        if settings.config_path.exists():
            # An installed app must keep its management UI available when
            # optional protocols fail or the user has disabled all inputs.
            raise SystemExit(0)
        sys.argv[sys.argv.index("--preflight-if-new")] = "--preflight"
    if "--preflight" in sys.argv:
        from micast.installation import main as preflight

        sys.argv.remove("--preflight")
        if "--profile" not in sys.argv:
            sys.argv.extend(["--profile", str(settings.config_path)])
        if "--automatic-env-ports" not in sys.argv:
            sys.argv.extend(["--automatic-env-ports", ",".join(
                key for key, (env_var, _) in EDITABLE_PORTS.items()
                if env_var in os.environ and not env_pinned(env_var)
            )])
        raise SystemExit(preflight())
    unix_socket = os.environ.get("MICAST_UNIX_SOCKET", "").strip()
    # A normal user's machine may already have something on 42300 — slide to a
    # free port instead of failing. Explicit MICAST_PORT stays strict.
    frozen = getattr(sys, "frozen", False)
    if frozen and sys.platform == "win32" and os.environ.get("MICAST_NO_DESKTOP") != "1":
        from micast.desktop import run_desktop  # noqa: PLC0415 — desktop-only deps

        run_desktop()
        return

    from micast.main import app  # noqa: PLC0415 — deferred until settings load

    lease = None
    if not unix_socket:
        lease = reserve_tcp(settings.preferred_port("port"), settings.host,
                            strict=settings.port_is_strict("port"))
        settings.apply_resolved_port("port", lease.port)

    if frozen and not unix_socket:
        # Packaged non-Windows app: take the user straight to the UI.
        url = f"http://127.0.0.1:{settings.port}"
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()

    if unix_socket:
        socket_path = Path(unix_socket)
        socket_path.parent.mkdir(parents=True, exist_ok=True)
        socket_path.unlink(missing_ok=True)
        uvicorn.run(app, uds=str(socket_path), log_level="info")
    else:
        try:
            uvicorn.Server(uvicorn.Config(app, host=settings.host, port=settings.port,
                                         log_level="info")).run(sockets=[lease.socket])
        finally:
            lease.close()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Windowed (console=False) builds lose tracebacks entirely — drop them
        # in <data dir>/crash.log so users can actually report what happened.
        if getattr(sys, "frozen", False):
            import traceback

            from micast.config import default_log_dir

            crash = default_log_dir() / "crash.log"
            crash.parent.mkdir(parents=True, exist_ok=True)
            crash.write_text(traceback.format_exc(), encoding="utf-8")
        raise
