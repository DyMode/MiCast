"""Health check for deployments whose HTTP port can be allocated automatically."""

import json
import urllib.request

from micast.config import default_runtime_dir


def main():
    endpoint = json.loads(
        (default_runtime_dir() / "runtime-ports.json").read_text(encoding="utf-8")
    )
    with urllib.request.urlopen(
        f"http://127.0.0.1:{endpoint['port']}/app/micast/health", timeout=3
    ) as response:
        if response.status != 200:
            raise RuntimeError("MiCast 未就绪")


if __name__ == "__main__":
    main()
