"""Local receiver environment contract shared by runtime planning."""

import os
from pathlib import Path

from micast.ports import AIRPLAY2_RECEIVER_PORT


def local_receiver_environment(settings, instance_id, mode):
    env = {"MICAST_DEVICE_ID": instance_id}
    if settings.airplay2_port:
        env["MICAST_AIRPLAY2_PORT"] = str(settings.airplay2_port)
    env["MICAST_AIRPLAY2_PORT_STRICT"] = "1" if settings.port_is_strict("airplay2_port") else "0"
    runtime = os.environ.get("MICAST_AIRPLAY2_RUNTIME", "").strip()
    source = settings.airplay2_pcm_source
    if runtime and mode == "single" and source.startswith("local:"):
        bundled = Path(runtime) / "run-shairport"
        if source.removeprefix("local:").strip() == str(bundled):
            env["MICAST_AIRPLAY2_PORT"] = str(AIRPLAY2_RECEIVER_PORT)
            env["MICAST_AIRPLAY2_PORT_STRICT"] = "1"
            env["MICAST_AIRPLAY2_READY_FILE"] = str(
                settings.config_path.parent / "airplay2-ready.json"
            )
    return env
