import json

from micast.fnos_storage import prepare, save_report


def test_share_migrates_only_logs_and_preserves_existing(tmp_path, monkeypatch):
    private, shared = tmp_path / "private", tmp_path / "shared"
    private.mkdir()
    (shared / "logs").mkdir(parents=True)
    for name in ("micast.log", "nqptp.log", "access.json", "upgrade-backup.tgz"):
        (private / name).write_text("private", encoding="utf-8")
    (shared / "logs" / "micast.log").write_text("newer", encoding="utf-8")
    monkeypatch.setattr("micast.diagnostics.public_settings", lambda: {"token": "secret", "name": "speaker"})
    prepare(shared, private)
    assert (shared / "logs" / "micast.log").read_text() == "newer"
    assert (private / "micast.log").exists()
    assert (shared / "logs" / "nqptp.log").exists()
    assert not (private / "nqptp.log").exists()
    assert (private / "access.json").exists()
    assert (private / "upgrade-backup.tgz").exists()
    assert not (shared / "access.json").exists()
    backup = (shared / "backups" / "settings-reference.json").read_text(encoding="utf-8")
    assert "secret" not in backup
    assert "speaker" in backup
    prepare(shared, private)


def test_report_saved_only_when_share_configured(tmp_path, monkeypatch):
    monkeypatch.delenv("MICAST_SHARED_DIR", raising=False)
    save_report("report.json", {"ok": True})
    monkeypatch.setenv("MICAST_SHARED_DIR", str(tmp_path))
    save_report("report.json", {"ok": True})
    assert json.loads((tmp_path / "diagnostics" / "report.json").read_text()) == {"ok": True}


def test_log_directory_override(tmp_path, monkeypatch):
    from micast.config import default_log_dir
    monkeypatch.setenv("MICAST_LOG_DIR", str(tmp_path))
    assert default_log_dir() == tmp_path
