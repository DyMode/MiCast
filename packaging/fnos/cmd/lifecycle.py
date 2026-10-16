"""Stdlib-only data handling, usable before fnOS extracts app dependencies."""

import argparse
import datetime
import io
import json
import os
import shutil
import tarfile
import tempfile
from pathlib import Path

PERSISTENT = (
    "micast.json",
    "access.json",
    "xiaomi-account.json",
    "xiaomi-tokens.enc",
    "legal-consent.json",
)
BACKUP = "upgrade-backup.tgz"
CONFIG_FIELDS = {
    "audio",
    "app",
    "port",
    "stream_port",
    "airplay_rtsp_port",
    "airplay_udp_base",
    "airplay2_port",
    "receiver_mode",
    "airplay_protocol",
    "airplay_engine",
    "dlna_enabled",
    "airplay_enabled",
    "strict_ports",
    "sync_groups_enabled",
    "large_delay_enabled",
    "airplay2_enabled",
    "network_discovery_enabled",
    "touchscreen_lyrics",
    "default_volume",
    "default_volume_enabled",
    "stale_session_timeout",
    "client_max_lag_seconds",
    "sender_volume_mode",
    "selected_device_id",
    "speakers",
    "receivers",
    "groups",
    "airplay2_instances",
    "saved_curves",
}


def atomic_write(path, payload):
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(payload)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def require_consent(value, version):
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError):
        parsed = value
    if parsed != version and parsed != [version]:
        raise ValueError("请阅读并同意当前版本的用户协议与隐私政策后安装。")


def backup(private):
    archive = private / BACKUP
    files = [private / name for name in PERSISTENT if (private / name).is_file()]
    if not files:
        archive.unlink(missing_ok=True)
        return
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as out:
        for path in files:
            if path.is_symlink():
                raise ValueError("升级配置不能是符号链接。")
            out.add(path, arcname=path.name, recursive=False)
    atomic_write(archive, buffer.getvalue())


def restore(private):
    archive = private / BACKUP
    if not archive.exists():
        return
    pending, seen = [], set()
    with tarfile.open(archive, "r:gz") as source:
        for member in source.getmembers():
            if member.name not in PERSISTENT or not member.isfile() or member.name in seen:
                raise ValueError("升级备份含有非预期文件，已停止恢复。")
            seen.add(member.name)
            target = private / member.name
            if target.is_symlink():
                raise ValueError("升级配置不能是符号链接。")
            if not target.exists():
                with source.extractfile(member) as stream:
                    pending.append((target, stream.read()))
    for target, content in pending:
        atomic_write(target, content)
    archive.unlink()


def uninstall(private, policy):
    if not private.is_absolute():
        raise ValueError("应用私有数据路径无效，已停止清理。")
    private = private.resolve()
    if not private.is_absolute() or private == Path(private.anchor) or not private.is_dir():
        raise ValueError("应用私有数据路径无效，已停止清理。")
    if policy == "keep_all":
        keep = set(PERSISTENT)
    elif policy == "remove_all":
        keep = set()
    else:
        config = private / "micast.json"
        if config.is_file() and not config.is_symlink():
            data = json.loads(config.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("设备配置格式无效，已停止清理。")
            data = {key: value for key, value in data.items() if key in CONFIG_FIELDS}
            atomic_write(config, json.dumps(data, ensure_ascii=False, indent=2).encode())
        keep = {"micast.json"}
    for child in private.iterdir():
        if child.name not in keep or child.is_symlink():
            if child.is_symlink() or not child.is_dir():
                child.unlink(missing_ok=True)
            else:
                shutil.rmtree(child)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action", choices=("check-consent", "record-consent", "backup", "restore", "uninstall")
    )
    parser.add_argument(
        "--source", choices=("fnos-install", "fnos-upgrade"), default="fnos-install"
    )
    args = parser.parse_args()
    version = (Path(__file__).parent / "legal-version").read_text(encoding="utf-8").strip()
    if args.action in {"check-consent", "record-consent"}:
        require_consent(os.environ.get("wizard_legal_accept", ""), version)  # noqa: SIM112 -- fnOS field
    if args.action == "check-consent":
        return
    raw_private = Path(os.environ["TRIM_PKGVAR"])
    if not raw_private.is_absolute():
        raise ValueError("应用私有数据路径无效。")
    private = raw_private.resolve()
    if not private.is_absolute() or private == Path(private.anchor):
        raise ValueError("应用私有数据路径无效。")
    private.mkdir(parents=True, exist_ok=True)
    if args.action == "record-consent":
        atomic_write(
            private / "legal-consent.json",
            json.dumps(
                {
                    "version": version,
                    "accepted_at": datetime.datetime.now(datetime.UTC).isoformat(),
                    "source": args.source,
                }
            ).encode(),
        )
    elif args.action == "backup":
        backup(private)
    elif args.action == "restore":
        restore(private)
    else:
        uninstall(private, os.environ.get("wizard_data_policy", "keep_config"))  # noqa: SIM112


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(
            f"MiCast 数据处理未完成：{type(error).__name__}。"
            "请检查目录与配置；恢复失败时请保留备份。"
        )
        raise SystemExit(1) from None
