"""Regression: fnOS must see live app processes even when cwd links are hidden."""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "fnos_service_process", ROOT / "packaging/fnos/cmd/service-process.py"
)
process = importlib.util.module_from_spec(spec)
spec.loader.exec_module(process)


@pytest.mark.parametrize(
    "argv,uid,expected",
    [
        (["/var/apps/python312/target/bin/python3", "-m", "micast"], 123, True),
        (["python3.12", "-m", "micast"], 123, True),
        (
            ["runuser", "-u", "micast", "--preserve-environment", "--", "python3", "-m", "micast"],
            0,
            True,
        ),
        (["python3", "-m", "micast"], 999, False),
        (["python3", "-m", "micast.fnos_receiver"], 123, False),
        (["python3", "-m", "micast.main"], 123, False),
        (["runuser", "-u", "other", "--", "python3", "-m", "micast"], 0, False),
        (["runuser", "-u", "micast", "--", "echo", "-m", "micast"], 0, False),
    ],
)
def test_only_exact_app_module_and_user_are_managed(argv, uid, expected):
    assert process.matches(argv, uid, "micast", 123) is expected


def test_recover_orphan_and_wrapper_without_pid_file_or_cwd_access(tmp_path):
    for pid, args, uid in [
        (40, ["python3", "-m", "micast"], 123),
        (41, ["runuser", "-u", "micast", "--", "python3", "-m", "micast"], 0),
        (42, ["python3", "-m", "micast"], 999),
        (43, ["python3", "-m", "micast.fnos_storage"], 123),
    ]:
        entry = tmp_path / str(pid)
        entry.mkdir()
        (entry / "cmdline").write_bytes(b"\0".join(a.encode() for a in args) + b"\0")
        (entry / "status").write_text(f"Name:\tpython\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n")
        # Deliberately no /proc/<pid>/cwd link, matching restricted /proc.
    (tmp_path / "44").mkdir()  # process exited while /proc was being scanned
    assert process.service_pids(tmp_path, "micast", 123) == [40, 41]
