"""Linux regression for the runuser exec race; no root or NAS required."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(os.name != "posix" or not shutil.which("bash"), reason="requires Linux bash")
def test_start_waits_for_fresh_launcher_even_before_its_cmdline_is_ready(tmp_path):
    app, private = tmp_path / "app", tmp_path / "private"
    app.mkdir()
    private.mkdir()
    (private / "callback-token").write_text("test-only")
    # Load production functions without the command dispatcher.
    source = (ROOT / "packaging/fnos/cmd/main").read_text(encoding="utf-8")
    functions = tmp_path / "functions.sh"
    functions.write_text(source.rsplit('case "${1:-}" in', 1)[0])
    script = r"""
source "$FUNCTIONS"
PROCESS_HELPER="$REAL_HELPER"
chown() { return 0; }
stop_airplay2_runtime() { return 0; }
# Simulate nohup/runuser exec delay before the health socket exists.
nohup() { sleep 0.2; touch "$SOCKET_FILE.ready"; sleep 2; }
is_running() { return 1; }
is_healthy() { return 0; }
stop_service() { return 0; }
# Model only the -S file test, without creating any real network listener.
[() {
  if command test "${1:-}" = "-S"; then
    command test -f "$2.ready"
  else
    command test "$@"
  fi
}
start_service
result=$?
kill "$launch_pid" 2>/dev/null || true
wait "$launch_pid" 2>/dev/null || true
exit "$result"
"""
    result = subprocess.run(
        ["bash", "-c", script],
        env={
            **os.environ,
            "TRIM_PKGVAR": str(private),
            "TRIM_APPDEST": str(app),
            "TRIM_USERNAME": "unused",
            "TRIM_TEMP_LOGFILE": str(tmp_path / "error.log"),
            "FUNCTIONS": str(functions),
            "REAL_HELPER": str(ROOT / "packaging/fnos/cmd/service-process.py"),
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert (app / "app.sock.ready").exists()
