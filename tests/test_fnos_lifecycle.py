"""Exercise actual lifecycle data operations without a running NAS or network."""

import importlib.util
import io
import json
import os
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "fnos_lifecycle", ROOT / "packaging/fnos/cmd/lifecycle.py"
)
lifecycle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lifecycle)


@pytest.mark.parametrize("value", ["", "false", "true", "[]", '["old"]', '["2026-10-16", "old"]'])
def test_consent_rejects_missing_boolean_and_wrong_versions(value):
    with pytest.raises(ValueError):
        lifecycle.require_consent(value, "2026-10-16")


@pytest.mark.parametrize("value", ["2026-10-16", '["2026-10-16"]'])
def test_consent_accepts_exact_version(value):
    lifecycle.require_consent(value, "2026-10-16")


@pytest.mark.parametrize("policy", ["keep_config", "unknown"])
def test_default_uninstall_removes_all_credentials_preserves_audio(tmp_path, policy):
    profile = {
        "audio": {"format": "flac"},
        "speakers": [{"did": "s1", "alias": "Room"}],
        "notify_webhook_url": "https://secret",
        "provider_account_id": "private-user",
        "encryption_key": "secret",
        "xiaomi": {"token": "legacy"},
    }
    (tmp_path / "micast.json").write_text(json.dumps(profile))
    for name in (*lifecycle.PERSISTENT[1:], lifecycle.BACKUP, "callback-token"):
        (tmp_path / name).write_text("sensitive")
    lifecycle.uninstall(tmp_path, policy)
    assert {p.name for p in tmp_path.iterdir()} == {"micast.json"}
    assert json.loads((tmp_path / "micast.json").read_text()) == {
        "audio": profile["audio"],
        "speakers": profile["speakers"],
    }


@pytest.mark.parametrize("policy", ["keep_all", "remove_all"])
def test_explicit_retention_keeps_only_documented_private_data(tmp_path, policy):
    for name in (*lifecycle.PERSISTENT, lifecycle.BACKUP, "micast.pid", "micast.log"):
        (tmp_path / name).write_text(name)
    lifecycle.uninstall(tmp_path, policy)
    expected = set(lifecycle.PERSISTENT) if policy == "keep_all" else set()
    assert {p.name for p in tmp_path.iterdir()} == expected


def test_upgrade_restores_missing_files_but_not_newer_live_state(tmp_path):
    for name in lifecycle.PERSISTENT:
        (tmp_path / name).write_text("old")
    lifecycle.backup(tmp_path)
    archive = tmp_path / lifecycle.BACKUP
    if os.name != "nt":
        assert archive.stat().st_mode & 0o777 == 0o600
    (tmp_path / "micast.json").write_text("newer")
    (tmp_path / "xiaomi-tokens.enc").unlink()
    lifecycle.restore(tmp_path)
    assert (tmp_path / "micast.json").read_text() == "newer"
    assert (tmp_path / "xiaomi-tokens.enc").read_text() == "old"
    assert not archive.exists()
    lifecycle.restore(tmp_path)  # repeated callback is harmless


@pytest.mark.parametrize("bad_name", ["../escape", "/etc/passwd", "other.json"])
def test_upgrade_rejects_unexpected_paths_before_any_write(tmp_path, bad_name):
    archive = tmp_path / lifecycle.BACKUP
    with tarfile.open(archive, "w:gz") as tar:
        for name in ("micast.json", bad_name):
            info = tarfile.TarInfo(name)
            info.size = 3
            tar.addfile(info, io.BytesIO(b"bad"))
    with pytest.raises(ValueError):
        lifecycle.restore(tmp_path)
    assert archive.exists()
    assert not (tmp_path / "micast.json").exists()


def test_corrupt_backup_is_retained_for_recovery(tmp_path):
    archive = tmp_path / lifecycle.BACKUP
    archive.write_bytes(b"broken")
    with pytest.raises(tarfile.TarError):
        lifecycle.restore(tmp_path)
    assert archive.read_bytes() == b"broken"


@pytest.mark.parametrize("private", [Path("relative"), Path(Path.cwd().anchor)])
def test_uninstall_rejects_relative_paths_and_volume_root(private):
    with pytest.raises(ValueError):
        lifecycle.uninstall(private, "remove_all")


def test_generated_docs_and_install_ui_match_bundled_source():
    sync_spec = importlib.util.spec_from_file_location("sync_legal", ROOT / "scripts/sync-legal.py")
    sync = importlib.util.module_from_spec(sync_spec)
    sync_spec.loader.exec_module(sync)
    for path, expected in sync.outputs().items():
        assert path.read_text(encoding="utf-8") == expected
    wizard = json.loads((ROOT / "packaging/fnos/wizard/install").read_text(encoding="utf-8"))
    checkbox = wizard[-1]["items"][-1]
    # fnpack serialises the control as one string value: a list (the natural
    # "unchecked" spelling) is rejected by its content validation, so the
    # default stays the empty string and the option value marks consent.
    assert checkbox["initValue"] == ""
    assert isinstance(checkbox["initValue"], str)
    assert checkbox["options"][0]["value"] == "2026-10-16"
    assert checkbox["rules"][0]["required"] is True


def test_consent_receipt_contains_only_version_time_and_source(tmp_path, monkeypatch):
    monkeypatch.setenv("TRIM_PKGVAR", str(tmp_path))
    monkeypatch.setenv("wizard_legal_accept", "2026-10-16")
    monkeypatch.setattr("sys.argv", ["lifecycle", "record-consent"])
    lifecycle.main()
    receipt = json.loads((tmp_path / "legal-consent.json").read_text())
    assert set(receipt) == {"version", "accepted_at", "source"}
    assert receipt["version"] == "2026-10-16"
