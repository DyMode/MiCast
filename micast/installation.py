"""Shared installation preflight. Only the standard library is required."""

import argparse
import contextlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

if __package__:
    from .config_store import write_json
    from .ports import AIRPLAY2_RECEIVER_PORT, probe_udp, probe_udp_group, reserve_tcp
else:
    from config_store import write_json
    from ports import AIRPLAY2_RECEIVER_PORT, probe_udp, probe_udp_group, reserve_tcp


def inspect_installation(
    *,
    host="0.0.0.0",
    unix_socket=False,
    airplay2=False,
    preferred=None,
    strict=(),
    enabled=None,
    runtime_check=None,
    classic_available=True,
    external_airplay2=None,
):
    preferred = preferred or {}
    enabled = enabled or {"airplay": True, "airplay2": True, "dlna": True}
    checks = {}
    leases = []
    selected = {}

    def check(key, operation):
        try:
            value = operation()
            checks[key] = {"available": True, "detail": "可用"}
            return value
        except (OSError, RuntimeError, ValueError) as exc:
            checks[key] = {"available": False, "detail": str(exc)}
            return None

    def tcp(key, default, attempts=32):
        lease = reserve_tcp(
            preferred.get(key) or default,
            host,
            strict=key in strict and key != "airplay_rtsp_port",
            attempts=attempts,
        )
        leases.append(lease)
        selected[key] = lease.port
        return lease.port

    try:
        if unix_socket:
            checks["management"] = {
                "available": True,
                "detail": "由平台 Unix socket 提供；启动时验证",
            }
        else:
            check("management", lambda: tcp("port", 42300))
        check("stream", lambda: tcp("stream_port", 42400))
        if (enabled.get("airplay") and classic_available) or (
            enabled.get("airplay2") and airplay2 and external_airplay2 is None
        ):
            check("mdns", lambda: probe_udp(5353, multicast="224.0.0.251"))
        if enabled.get("airplay") and classic_available:
            check("rtsp", lambda: tcp("airplay_rtsp_port", 42500, 31))
            check("rtp", lambda: probe_udp_group(preferred.get("airplay_udp_base") or 42600))
        if enabled.get("dlna") and classic_available:
            check("ssdp", lambda: probe_udp(1900, multicast="239.255.255.250"))
            if unix_socket:
                check("dlna_http", lambda: tcp("port", 42300))
        if airplay2 and enabled.get("airplay2") and external_airplay2 is not None:
            check("external_airplay2", external_airplay2)
        elif airplay2 and enabled.get("airplay2"):
            def fixed_receiver():
                lease = reserve_tcp(AIRPLAY2_RECEIVER_PORT, host, strict=True)
                leases.append(lease)
                selected["airplay2_port"] = lease.port
                return lease.port

            check("airplay2_tcp", fixed_receiver)
            check("ptp_event", lambda: probe_udp(319))
            check("ptp_general", lambda: probe_udp(320))
            check("ptp_internal", lambda: probe_udp(9000))
            check("airplay2_runtime", runtime_check or (lambda: None))

        def feature(name, dependencies, supported=True):
            if not supported:
                return {"status": "unsupported", "available": False, "detail": "当前安装方式不支持"}
            if not enabled.get(name):
                return {"status": "disabled", "available": False, "detail": "用户已关闭"}
            failures = [
                f"{key}: {checks[key]['detail']}"
                for key in dependencies
                if not checks[key]["available"]
            ]
            return {
                "status": "blocked" if failures else "ready",
                "available": not failures,
                "detail": "；".join(failures) if failures else "预检通过，启动时再次验证",
            }

        features = {
            "airplay": feature("airplay", ["mdns", "rtsp", "rtp"], classic_available),
            "airplay2": feature(
                "airplay2",
                ["external_airplay2"]
                if external_airplay2 is not None
                else [
                    "mdns",
                    "airplay2_tcp",
                    "ptp_event",
                    "ptp_general",
                    "ptp_internal",
                    "airplay2_runtime",
                ],
                airplay2,
            ),
            "dlna": feature(
                "dlna", ["ssdp", "dlna_http" if unix_socket else "management"], classic_available
            ),
        }
        core = checks["management"]["available"] and checks["stream"]["available"]
        return {
            "installable": core and any(item["available"] for item in features.values()),
            "core_available": core,
            "features": features,
            "checks": checks,
            "selected_ports": selected,
            "snapshot_only": True,
            "airplay2_runtime_checked": runtime_check is not None,
        }
    finally:
        for lease in leases:
            lease.close()


def check_fnos_runtime(runtime: Path, username: str):
    """Verify NQPTP under the app identity after install_callback applies caps."""
    import platform

    loader = (
        runtime
        / "lib"
        / (
            "ld-musl-aarch64.so.1"
            if platform.machine() in {"aarch64", "arm64"}
            else "ld-musl-x86_64.so.1"
        )
    )
    for executable in (
        loader,
        runtime / "bin/nqptp",
        runtime / "bin/shairport-sync",
        runtime / "run-shairport",
    ):
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise RuntimeError(f"AirPlay 2 运行时缺失或不可执行: {executable.name}")
    launcher = runtime / "run-shairport"
    first_line = launcher.read_bytes().split(b"\n", 1)[0]
    if first_line != b"#!/bin/sh":
        raise RuntimeError("AirPlay 2 启动脚本格式错误，请重新安装修复版（需要 LF 换行和 /bin/sh）")
    if not os.access("/bin/sh", os.X_OK):
        raise RuntimeError("AirPlay 2 启动脚本所需的 /bin/sh 不可执行")
    libraries = ":".join(
        str(runtime / directory) for directory in ("usr-lib/pulseaudio", "usr-lib", "usr-local-lib")
    )
    command = [str(loader), "--library-path", libraries, str(runtime / "bin/nqptp")]
    if username:
        command = ["runuser", "-u", username, "--", *command]
    process = subprocess.Popen(
        command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, start_new_session=True
    )
    try:
        time.sleep(0.7)
        if process.poll() is not None:
            detail = process.communicate()[1].decode(errors="replace")[-2000:]
            raise RuntimeError(f"应用用户无法启动 NQPTP（端口/权限/运行库）: {detail}")
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.communicate()


def main():
    parser = argparse.ArgumentParser(description="MiCast 统一安装预检")
    parser.add_argument("--fnos", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--write-config", type=Path)
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--runtime", type=Path)
    parser.add_argument("--username", default="")
    parser.add_argument("--automatic-env-ports", default="")
    args = parser.parse_args()
    profile = (
        json.loads(args.profile.read_text(encoding="utf-8"))
        if (args.profile and args.profile.exists())
        else {}
    )
    preferred = {
        key: profile[key]
        for key in ("port", "stream_port", "airplay_rtsp_port", "airplay_udp_base", "airplay2_port")
        if profile.get(key) is not None
    }
    strict = list(profile.get("strict_ports", []))
    enabled = {key: profile.get(f"{key}_enabled", True) for key in ("airplay", "airplay2", "dlna")}
    automatic_env = args.automatic_env_ports.split(",")
    for field in ("port", "stream_port", "airplay_rtsp_port", "airplay_udp_base", "airplay2_port"):
        value = os.environ.get(f"wizard_{field}", os.environ.get(f"MICAST_{field.upper()}", ""))
        if field in automatic_env and f"wizard_{field}" not in os.environ and field in preferred:
            continue
        if f"wizard_{field}" in os.environ:
            preferred.pop(field, None)
            strict = [key for key in strict if key != field]
        if value and value.lower() != "auto":
            try:
                preferred[field] = int(value)
            except ValueError:
                parser.error(f"{field} 必须为数字或 auto")
            if not 1024 <= preferred[field] <= 65535:
                parser.error(f"{field} 必须为 1024-65535")
            if field not in strict and (
                field not in automatic_env or f"wizard_{field}" in os.environ
            ):
                strict.append(field)
    for protocol in ("airplay", "airplay2", "dlna"):
        value = os.environ.get(
            f"wizard_{protocol}_enabled", os.environ.get(f"MICAST_{protocol.upper()}_ENABLED")
        )
        if value is not None:
            enabled[protocol] = value.lower() in {"true", "1", "yes"}
    mode = os.environ.get("MICAST_AIRPLAY2_MODE", "disabled").strip().lower()
    deployment = os.environ.get("MICAST_DEPLOYMENT", "").strip().lower()
    supported = args.fnos or mode in {"single", "multi"} or deployment in {"single", "integrated"}
    network = os.environ.get("MICAST_NETWORK_MODE", "").strip().lower()
    classic = network != "bridge" and (
        network in {"host", "native"} or deployment not in {"single", "integrated"}
    )
    external = None
    source = os.environ.get("MICAST_AIRPLAY2_PCM_SOURCE", profile.get("airplay2_pcm_source", ""))
    if supported and source.startswith("tcp:"):
        address, raw_port = source.removeprefix("tcp:").rsplit(":", 1)

        def external():
            # Connecting to socat's PCM listener would create another reader
            # of the shared audio pipe and could consume a live stream's data.
            socket.getaddrinfo(address, int(raw_port), type=socket.SOCK_STREAM)

    elif supported and (mode == "multi" or deployment == "integrated"):

        def external():
            import urllib.request

            url = os.environ.get("MICAST_ORCHESTRATOR_URL", "http://127.0.0.1:42900")
            with urllib.request.urlopen(url.rstrip("/") + "/health", timeout=2) as response:
                if response.status != 200:
                    raise RuntimeError("AirPlay 2 编排服务未就绪")

    report = inspect_installation(
        unix_socket=args.fnos,
        airplay2=supported,
        preferred=preferred,
        strict=strict,
        enabled=enabled,
        runtime_check=(lambda: check_fnos_runtime(args.runtime, args.username))
        if args.runtime
        else None,
        classic_available=classic,
        external_airplay2=external,
    )
    serialized = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        write_json(args.output, report)
    if sys.stdout is not None:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        print(serialized)
    if report["installable"] and args.write_config:
        # Merge only installation choices, preserving the existing profile.
        data = (
            json.loads(args.write_config.read_text(encoding="utf-8"))
            if args.write_config.exists()
            else {}
        )
        data.update(
            {
                key: value
                for key, value in report["selected_ports"].items()
                if key in {"port", "stream_port"}
            }
        )
        data.update(preferred)
        if args.fnos:
            # Migrate old auto/custom selections without clearing user data.
            data["airplay2_port"] = AIRPLAY2_RECEIVER_PORT
            strict = [key for key in strict if key != "airplay2_port"]
        data["strict_ports"] = strict
        for protocol, requested in enabled.items():
            data[f"{protocol}_enabled"] = requested
        write_json(args.write_config, data)
    return 0 if report["installable"] else 1


if __name__ == "__main__":
    sys.exit(main())
