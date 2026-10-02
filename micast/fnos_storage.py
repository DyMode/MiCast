"""fnOS user-visible storage; credentials and recovery archives stay private."""

import logging
import os
import shutil
import sys
from pathlib import Path

from micast.config_store import write_json

LOG_NAMES = ("micast.log", "micast.log.1", "crash.log", "nqptp.log",
             "shairport-startup.log", "airplay2-startup.json")


def prepare(shared: Path, private: Path) -> None:
    for name in ("logs", "diagnostics", "backups"):
        (shared / name).mkdir(parents=True, exist_ok=True)
    for name in LOG_NAMES:
        source, target = private / name, shared / "logs" / name
        if source.is_file() and not target.exists():
            shutil.copy2(source, target)
            source.unlink()
    # This is a reference copy, not the private full upgrade recovery archive.
    from micast.diagnostics import public_settings, sanitize_obj
    write_json(shared / "backups" / "settings-reference.json", {
        "purpose": "脱敏配置参考副本，不包含登录凭据，不用于完整恢复",
        "settings": sanitize_obj(public_settings()),
    })


def save_report(filename: str, report: dict) -> None:
    shared = os.environ.get("MICAST_SHARED_DIR")
    if not shared:
        return
    try:
        write_json(Path(shared) / "diagnostics" / Path(filename).name, report)
    except OSError:
        logging.getLogger(__name__).warning("无法保存共享诊断副本", exc_info=True)


if __name__ == "__main__":
    prepare(Path(sys.argv[1]), Path(os.environ["MICAST_DATA_DIR"]))
