import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FNOS = ROOT / "packaging" / "fnos"


def _manifest() -> dict[str, str]:
    """Parse key=value lines, honouring the triple-quoted multiline values
    (desc=\"\"\"...\"\"\") that fnOS manifests use for HTML descriptions."""
    result: dict[str, str] = {}
    key = None
    for line in (FNOS / "manifest").read_text(encoding="utf-8").splitlines():
        if key is not None:
            if line.rstrip().endswith('"""'):
                key = None
            continue
        if not line or line.startswith("#"):
            continue
        k, _, value = line.partition("=")
        if value.startswith('"""') and not value.rstrip().endswith('"""'):
            key = k
        result[k] = value
    return result


def test_fnos_package_declares_gateway_and_python_runtime():
    manifest = _manifest()
    entry = json.loads((FNOS / "app" / "ui" / "config").read_text(encoding="utf-8"))
    route = entry[".url"]["micast.main"]

    assert manifest["appname"] == "micast"
    assert manifest["install_dep_apps"] == "python312"
    assert manifest["maintainer"] == "Dy"
    assert route["gatewayPrefix"] == "/app/micast"
    assert route["gatewaySocket"] == "app.sock"
    assert route["url"] == "/app/micast"


def test_fnos_package_has_every_required_lifecycle_file():
    required = {
        "main",
        "install_init",
        "install_callback",
        "upgrade_init",
        "upgrade_callback",
        "uninstall_init",
        "uninstall_callback",
        "config_init",
        "config_callback",
    }

    assert required <= {path.name for path in (FNOS / "cmd").iterdir()}
    assert json.loads((FNOS / "config" / "privilege").read_text(encoding="utf-8"))
    assert json.loads((FNOS / "config" / "resource").read_text(encoding="utf-8")) == {"data-share": {"shares": [{"name": "micast"}]}}
    uninstall_wizard = json.loads((FNOS / "wizard" / "uninstall").read_text(encoding="utf-8"))
    policy = uninstall_wizard[0]["items"][0]
    assert policy["field"] == "wizard_data_policy"
    assert policy["initValue"] == "keep_config"


def test_fnos_runtime_uses_installed_target_layout():
    main = (FNOS / "cmd" / "main").read_text(encoding="utf-8")
    install = (FNOS / "cmd" / "install_callback").read_text(encoding="utf-8")

    assert "${TRIM_APPDEST}/vendor:${TRIM_APPDEST}" in main
    assert 'MICAST_UNIX_SOCKET="$SOCKET_FILE"' in main
    assert 'cd "$TRIM_APPDEST"' in main
    assert "${TRIM_APPDEST}/vendor" in install
    assert "${TRIM_APPDEST}/app/vendor" not in main + install
    # The built-in 8080 default must remain unpinned so a collision can slide
    # to the next free port. Only a real administrator-provided environment
    # variable is allowed to make the preference strict.
    assert 'export MICAST_STREAM_PORT="${MICAST_STREAM_PORT:-8080}"' not in main
    # fnOS surfaces this temporary log directly to users: keep tracebacks in
    # micast.log for diagnostics instead of copying source-level details here.
    assert "tail -n" not in main


def test_fnos_keeps_classic_airplay_and_starts_single_airplay2_on_demand():
    main = (FNOS / "cmd" / "main").read_text(encoding="utf-8")
    receiver = (FNOS / "app" / "airplay2-runtime" / "run-shairport").read_text(encoding="utf-8")

    assert 'MICAST_AIRPLAY_ENGINE="local"' in main
    assert 'MICAST_AIRPLAY_PROTOCOL="classic"' in main
    assert 'MICAST_AIRPLAY2_MODE="single"' in main
    assert 'MICAST_AIRPLAY2_MODE="disabled"' in main
    assert '[ -x "${runtime}/bin/shairport-sync" ]' in main
    assert 'MICAST_AIRPLAY2_PCM_SOURCE="local:' in main
    assert "runtime/bin/nqptp" not in main
    runtime = (ROOT / "micast" / "fnos_receiver.py").read_text(encoding="utf-8")
    assert "micast.fnos_receiver" in receiver
    assert "bin/nqptp" in runtime
    assert 'service_type = "airplay2"' in runtime
    # Bundled AirPlay 2 uses the fixed native port, without candidate scans.
    assert "preferred = AIRPLAY2_RECEIVER_PORT" in runtime
    assert "strict=True" in runtime


def test_fnos_setcap_is_arch_aware_and_warns_instead_of_aborting():
    for name in ("install_callback", "upgrade_callback"):
        script = (FNOS / "cmd" / name).read_text(encoding="utf-8")
        # Loader chosen by architecture (ARM FPK ships the aarch64 runtime).
        assert 'aarch64|arm64) loader="$runtime/lib/ld-musl-aarch64.so.1"' in script
        assert '*)             loader="$runtime/lib/ld-musl-x86_64.so.1"' in script
        # setcap failure / missing tooling degrades native AirPlay 2 with a
        # visible warning; it must not abort the (un)install.
        assert "exit 1" not in script.split("setcap", 1)[1].split('rm -f "${TRIM_PKGVAR}/micast.pid"', 1)[0]
        assert "警告" in script


def test_fnos_builders_support_arm_without_bundling_x86_airplay2():
    shell = (ROOT / "scripts" / "build-fnos.sh").read_text(encoding="utf-8")
    install = (FNOS / "cmd" / "install_callback").read_text(encoding="utf-8")

    assert "x86|arm" in shell
    assert "manylinux_2_28_aarch64" in shell
    assert 'if [ "$PLATFORM" = "arm" ]' in shell
    assert 'if [ -d "$runtime" ]' in install


def test_fnos_powershell_builder_is_a_thin_wrapper():
    powershell = (ROOT / "scripts" / "build-fnos.ps1").read_text(encoding="utf-8")

    # 真正的实现只在 build-fnos.sh；ps1 只是找到 bash.exe 并透传参数。
    assert "build-fnos.sh" in powershell
    assert "bash.exe" in powershell
    assert "manylinux_2_28_aarch64" not in powershell
    assert "pip install" not in powershell
    assert "fnpack-1.2.3" not in powershell


def test_fnos_builders_check_version_consistency():
    shell = (ROOT / "scripts" / "build-fnos.sh").read_text(encoding="utf-8")

    assert "__version__" in shell
    assert "version=" in shell
    assert "版本不一致" in shell

    # 双保险：运行时解析出的两侧版本必须真正相等。
    import re

    init_py = (ROOT / "micast" / "__init__.py").read_text(encoding="utf-8")
    app_version = re.search(r'__version__\s*=\s*"([^"]+)"', init_py).group(1)
    manifest_version = _manifest()["version"]
    assert app_version == manifest_version


def test_fnos_lifecycle_handles_health_upgrade_and_uninstall_policies():
    main = (FNOS / "cmd" / "main").read_text(encoding="utf-8")
    upgrade = (FNOS / "cmd" / "upgrade_init").read_text(encoding="utf-8")
    uninstall = (FNOS / "cmd" / "uninstall_callback").read_text(encoding="utf-8")

    assert "GET /health" in main
    assert "5242880" in main
    assert "upgrade-backup.tgz" in upgrade
    assert 'find "$TRIM_PKGVAR" -mindepth 1 -maxdepth 1' in uninstall
    assert '[ "$TRIM_PKGVAR" != "/" ]' in uninstall
    assert "keep_all)" in uninstall
    assert "remove_all)" in uninstall
    assert "! -name micast.json" in uninstall


def test_fnos_launcher_has_linux_shebang_and_line_endings():
    contents = (FNOS / "app" / "airplay2-runtime" / "run-shairport").read_bytes()
    assert contents.startswith(b"#!/bin/sh\n")
    assert b"\r" not in contents
