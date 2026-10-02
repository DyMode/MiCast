"""Storage behavior and distribution configuration contracts."""

import re
from pathlib import Path

from micast import config


def test_data_dir_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("MICAST_DATA_DIR", str(tmp_path / "data"))
    assert config.default_data_dir() == tmp_path / "data"


def test_data_dir_isolates_source_checkout(monkeypatch, tmp_path):
    monkeypatch.delenv("MICAST_DATA_DIR", raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setattr(config.sys, "frozen", False, raising=False)
    assert config.default_data_dir() == tmp_path / "MiCast-Dev"


def test_portable_build_uses_adjacent_data(monkeypatch, tmp_path):
    monkeypatch.delenv("MICAST_DATA_DIR", raising=False)
    executable = tmp_path / "MiCast.exe"
    (tmp_path / "portable.flag").touch()
    monkeypatch.setattr(config.sys, "frozen", True, raising=False)
    monkeypatch.setattr(config.sys, "executable", str(executable))
    monkeypatch.setattr(config.sys, "platform", "win32")

    assert config.storage_mode() == "portable"
    assert config.default_data_dir() == tmp_path / "data"


def test_data_dir_packaged_falls_back_to_user_dir(monkeypatch):
    monkeypatch.delenv("MICAST_DATA_DIR", raising=False)
    monkeypatch.setattr(config.sys, "frozen", True, raising=False)
    monkeypatch.setattr(config.sys, "executable", "/Applications/MiCast")
    monkeypatch.setattr(config.sys, "platform", "darwin")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: Path("/Users/tester")))
    assert config.default_data_dir() == Path("/Users/tester/Library/Application Support/MiCast")


def test_windows_distribution_definitions_cover_portable_and_installed_modes():
    root = Path(__file__).resolve().parents[1]
    build_script = (root / "scripts" / "build-windows-distributions.ps1").read_text(
        encoding="utf-8"
    )
    installer = (root / "packaging" / "windows" / "MiCast.iss").read_text(encoding="utf-8")

    assert "portable.flag" in build_script
    assert "MiCast-Portable.zip" in build_script
    assert "MB_YESNOCANCEL" in installer
    assert "{localappdata}\\MiCast" in installer
    assert "{userappdata}\\MiCast" in installer


def test_multiarch_docker_build_keeps_web_build_off_qemu():
    root = Path(__file__).resolve().parents[1]
    dockerfile = (root / "docker" / "Dockerfile").read_text(encoding="utf-8")
    workflow = (root / ".github" / "workflows" / "docker.yml").read_text(encoding="utf-8")

    assert re.search(
        r"^FROM\s+--platform=\$BUILDPLATFORM\s+\S+\s+AS\s+web-build\s*$",
        dockerfile, re.MULTILINE | re.IGNORECASE,
    )
    assert "group: docker-${{ github.sha }}" in workflow
