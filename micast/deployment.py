"""Deployment feature gates shared by API, runtime and UI state."""

import os
import sys


def deployment_mode() -> str:
    """Return the normalized package/runtime family exposed to the UI."""
    explicit = os.environ.get("MICAST_DEPLOYMENT", "").strip().lower()
    if explicit:
        return explicit
    # A frozen Windows build without an explicit tag is the desktop exe.
    if is_windows_exe():
        return "windows"
    return "development"


def is_windows_exe() -> bool:
    """True when running as the PyInstaller-packaged Windows desktop app."""
    return bool(getattr(sys, "frozen", False)) and sys.platform == "win32"


def update_download_supported() -> bool:
    """Only the Windows exe may download updates; fnOS/Docker builds update
    through their own package channels, so they only get the notice."""
    return is_windows_exe()


def integrated_airplay2_available() -> bool:
    """Only the full MiCast compose stack owns an AirPlay 2 orchestrator."""
    return os.environ.get("MICAST_DEPLOYMENT", "").strip().lower() == "integrated"


def airplay2_mode() -> str:
    """Return the deployment capability: disabled, single, or multi."""
    explicit = os.environ.get("MICAST_AIRPLAY2_MODE", "").strip().lower()
    if explicit in {"disabled", "single", "multi"}:
        return explicit
    deployment = deployment_mode()
    if deployment == "fnos":
        return "single"
    if deployment == "integrated":
        return "multi"
    return "disabled"


def airplay2_available() -> bool:
    return airplay2_mode() != "disabled"


def classic_ingress_available() -> bool:
    """The bridged controller cannot advertise LAN multicast receivers."""
    network = os.environ.get("MICAST_NETWORK_MODE", "").strip().lower()
    if network in {"host", "native"}:
        return True
    if network == "bridge":
        return False
    return deployment_mode() not in {"single", "integrated"}
