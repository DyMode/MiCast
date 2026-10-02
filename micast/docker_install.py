"""Host-side Docker port planning; never probe host mappings from a container."""

import argparse
import os
import shlex
import subprocess
from pathlib import Path

from micast.config_store import write_bytes
from micast.ports import probe_udp, probe_udp_group, reserve_tcp


def read_port_preferences(path):
    values = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            key, separator, value = line.partition("=")
            key = key.strip().removeprefix("export ").strip()
            if separator and key in {"MICAST_PORT", "MICAST_STREAM_PORT"}:
                tokens = shlex.split(value, comments=True)
                if len(tokens) > 1:
                    raise ValueError(f"{key} 必须为单个端口值或 auto")
                values[key] = tokens[0] if tokens else ""
    return values


def plan_host_ports(values, *, host_network=False):
    leases = []
    result = {}
    try:
        for field, default in (("MICAST_PORT", 42300), ("MICAST_STREAM_PORT", 42400)):
            raw = values.get(field, "auto").strip()
            automatic = not raw or raw.lower() == "auto"
            preferred = default if automatic else int(raw)
            if not 1024 <= preferred <= 65535:
                raise ValueError(f"{field} 必须为 1024–65535 或 auto")
            lease = reserve_tcp(preferred, strict=not automatic)
            leases.append(lease)
            result[field] = str(lease.port)
        if host_network:
            available = []
            for port, group in ((5353, "224.0.0.251"), (1900, "239.255.255.250")):
                try:
                    probe_udp(port, multicast=group)
                    if port == 5353:
                        lease = reserve_tcp(42500, attempts=31)
                        leases.append(lease)
                        probe_udp_group(42600)
                    available.append(port)
                except (OSError, RuntimeError):
                    pass
            if not available:
                raise RuntimeError("AirPlay 和 DLNA 的端口/组播条件均不满足，没有可用投送方式")
        return result
    finally:
        for lease in leases:
            lease.close()


def main():
    parser = argparse.ArgumentParser(description="在 Docker 宿主机规划 MiCast 端口")
    parser.add_argument("--mode", choices=("classic", "single", "multi"), default="classic")
    parser.add_argument("--env-file", type=Path, default=Path("docker/.env"))
    parser.add_argument("--up", action="store_true", help="预检后启动容器；省略则只生成端口方案")
    args = parser.parse_args()
    values = read_port_preferences(args.env_file)
    for key in ("MICAST_PORT", "MICAST_STREAM_PORT"):
        if key in os.environ:
            values[key] = os.environ[key]
    planned = plan_host_ports(values, host_network=args.mode == "classic")
    output = args.env_file.parent / f".micast-ports-{args.mode}.env"
    lines = []
    effective = {}
    for key, value in planned.items():
        automatic = values.get(key, "auto").strip().lower() in {"", "auto"}
        effective[key] = "auto" if args.mode == "classic" and automatic else value
        lines.append(f"{key}={effective[key]}\n")
    write_bytes(output, "".join(lines).encode("utf-8"))
    compose = args.env_file.parent / (
        "experimental/docker-compose.multi.yml"
        if args.mode == "multi"
        else f"docker-compose.{args.mode}.yml"
    )
    command = ["docker", "compose"]
    if args.env_file.exists():
        command += ["--env-file", str(args.env_file.resolve())]
    command += ["--env-file", str(output.resolve()), "-f", str(compose.resolve()), "up", "-d"]
    print(f"管理端口：{planned['MICAST_PORT']}；音频端口：{planned['MICAST_STREAM_PORT']}")
    print(
        "预检只代表当前状态；Docker 将在启动时实际绑定。接收容器的组播、权限和时钟在其网络内验证。"
    )
    if args.up:
        subprocess.run(command, check=True, env={**os.environ, **effective})
    else:
        if os.name == "nt":
            assignments = "; ".join(f"$env:{key}='{value}'" for key, value in effective.items())
            rendered = "& " + " ".join("'" + arg.replace("'", "''") + "'" for arg in command)
            print(f"{assignments}; {rendered}")
        else:
            print(
                shlex.join(
                    ["env", *(f"{key}={value}" for key, value in effective.items()), *command]
                )
            )


if __name__ == "__main__":
    main()
