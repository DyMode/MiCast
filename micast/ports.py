"""Dependency-free port allocation shared by launchers and installation checks."""

import socket
from dataclasses import dataclass

# The packaged Shairport Sync resets its port after parsing CLI/config values.
# Keep this runtime capability shared by installation, launchers and the UI.
AIRPLAY2_RECEIVER_PORT = 7000


def native_receiver_port(protocol: str = "airplay2") -> int:
    if protocol not in {"classic", "airplay2", "auto"}:
        raise ValueError(f"Unsupported native receiver protocol: {protocol}")
    return 5000 if protocol == "classic" else AIRPLAY2_RECEIVER_PORT


@dataclass
class PortLease:
    socket: socket.socket

    @property
    def port(self) -> int:
        return self.socket.getsockname()[1]

    def close(self) -> None:
        self.socket.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def candidates(preferred: int, attempts: int = 32, *, strict: bool = False):
    if not 1 <= preferred <= 65535 or attempts < 0:
        raise ValueError("端口必须为 1-65535，重试次数不能为负数")
    return range(preferred, min(65535, preferred + (0 if strict else attempts)) + 1)


def reserve_tcp(
    preferred: int, host: str = "0.0.0.0", *, strict: bool = False, attempts: int = 32, excluded=()
) -> PortLease:
    """Return an already listening socket; keep it until the server shuts down."""
    last_error = None
    for port in candidates(preferred, attempts, strict=strict):
        if port in excluded:
            continue
        sock = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM)
        try:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            else:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((host, port))
            sock.listen(socket.SOMAXCONN)
            sock.setblocking(False)
            return PortLease(sock)
        except OSError as exc:
            last_error = exc
            sock.close()
    suffix = "（显式指定，不会自动更换）" if strict else ""
    raise RuntimeError(
        f"端口 {preferred}-{min(65535, preferred + (0 if strict else attempts))} "
        f"不可用{suffix}: {last_error or '已被其他功能保留'}"
    )


def probe_udp(port: int, *, multicast: str | None = None) -> None:
    """Probe sharing and group membership, not just whether a listener exists."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        if multicast:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", port))
        if multicast:
            sock.setsockopt(
                socket.IPPROTO_IP,
                socket.IP_ADD_MEMBERSHIP,
                socket.inet_aton(multicast) + socket.inet_aton("0.0.0.0"),
            )


def probe_udp_group(base: int, width: int = 196) -> int:
    """Find one complete three-port group, holding all sockets during the check."""
    top = min(65535, base + width - 1)
    for candidate in range(base, top - 1, 3):
        sockets = []
        try:
            for port in range(candidate, candidate + 3):
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sockets.append(sock)
                sock.bind(("0.0.0.0", port))
            return candidate
        except OSError:
            pass
        finally:
            for sock in sockets:
                sock.close()
    raise RuntimeError(f"没有可用的 AirPlay UDP 三端口组（{base}-{top}）")
