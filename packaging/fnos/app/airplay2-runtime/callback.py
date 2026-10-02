"""Small Unix-socket callback used by the bundled Shairport Sync process."""

import json
import os
import socket
import sys
import time


def main() -> int:
    action = sys.argv[1] if len(sys.argv) > 1 else ""
    receiver_id = sys.argv[2] if len(sys.argv) > 2 else "main"
    token = os.environ.get("MICAST_ORCHESTRATOR_TOKEN", "")
    routes = {
        "start": "/api/playback/start",
        "stop": "/api/playback/session/stop",
        "volume": "/api/playback/session/volume",
    }
    if action not in routes or not token:
        return 2
    payload: dict[str, object] = {"device_id": receiver_id, "token": token}
    payload["epoch"] = os.environ.get("MICAST_RECEIVER_EPOCH", "")
    payload["event_seq"] = time.monotonic_ns()
    if action == "volume":
        try:
            payload["db"] = float(sys.argv[3])
        except (IndexError, ValueError):
            return 2
    body = json.dumps(payload, separators=(",", ":")).encode()
    request = (
        f"POST {routes[action]} HTTP/1.1\r\nHost: localhost\r\n"
        f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n"
        "Connection: close\r\n\r\n"
    ).encode() + body
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(3)
        client.connect(os.environ["MICAST_UNIX_SOCKET"])
        client.sendall(request)
        response = client.recv(128)
    return 0 if b" 200 " in response else 1


if __name__ == "__main__":
    raise SystemExit(main())
