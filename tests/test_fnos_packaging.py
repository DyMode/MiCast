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


def test_fnos_upgrade_restores_the_pre_upgrade_backup():
    upgrade = (FNOS / "cmd" / "upgrade_callback").read_text(encoding="utf-8")
    # The backup written by upgrade_init must actually be restored, otherwise
    # the Xiaomi login and settings are lost on every package upgrade.
    assert 'lifecycle.py" restore' in upgrade
    assert "exit 1" in upgrade.split('lifecycle.py" restore', 1)[1]
    assert "chown" in upgrade


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
    assert 'lifecycle.py" backup' in upgrade
    assert 'lifecycle.py" uninstall' in uninstall
    assert '[ "$TRIM_PKGVAR" != "/" ]' in uninstall
    assert "共享目录与浏览器数据需另行清理" in uninstall


def test_fnos_launcher_has_linux_shebang_and_line_endings():
    contents = (FNOS / "app" / "airplay2-runtime" / "run-shairport").read_bytes()
    assert contents.startswith(b"#!/bin/sh\n")
    assert b"\r" not in contents


def _package_validator():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "check_fnos_package", ROOT / "scripts" / "check-fnos-package.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_fnos_package_validator_accepts_the_shipped_package():
    module = _package_validator()

    assert module.validate(FNOS, repo_root=ROOT) == []


def _validator_fixture(tmp_path: Path):
    """Copy packaging/fnos so a single mutation can be validated."""
    import shutil

    target = tmp_path / "fnos"
    shutil.copytree(FNOS, target, ignore=shutil.ignore_patterns("__pycache__"))
    return target


def _wizard(package: Path, name: str) -> list:
    return json.loads((package / "wizard" / name).read_text(encoding="utf-8"))


def _save_wizard(package: Path, name: str, payload: list) -> None:
    (package / "wizard" / name).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def test_fnos_package_validator_rejects_list_init_value_on_checkbox(tmp_path):
    """fnpack rejects a checkbox whose initValue is a list with a generic JSON
    error that silently skipped a whole release build, so the value shape has to
    be caught before staging."""
    module = _package_validator()
    package = _validator_fixture(tmp_path)
    payload = _wizard(package, "install")
    payload[-1]["items"][-1]["initValue"] = []
    _save_wizard(package, "install", payload)

    problems = module.validate(package)

    assert any("initValue 必须是字符串" in problem for problem in problems)
    assert all("wizard/install" in problem for problem in problems)


def test_fnos_package_validator_rejects_empty_options_and_unknown_types(tmp_path):
    module = _package_validator()
    package = _validator_fixture(tmp_path)
    payload = _wizard(package, "upgrade")
    payload[-1]["items"][-1]["options"] = []
    payload[-1]["items"].append({"type": "slider", "field": "wizard_x", "label": "x"})
    _save_wizard(package, "upgrade", payload)

    problems = module.validate(package)

    assert any("options 为空" in problem for problem in problems)
    assert any("未知控件类型" in problem for problem in problems)


def test_fnos_package_validator_rejects_empty_step_title_and_long_text(tmp_path):
    module = _package_validator()
    package = _validator_fixture(tmp_path)
    payload = _wizard(package, "uninstall")
    payload[0]["stepTitle"] = "  "
    payload[0]["items"].append(
        {
            "type": "text",
            "field": "wizard_note",
            "label": "备注",
            "initValue": "x" * (module.TEXT_INIT_VALUE_LIMIT + 1),
        }
    )
    _save_wizard(package, "uninstall", payload)

    problems = module.validate(package)

    assert any("stepTitle 为空" in problem for problem in problems)
    assert any("超过" in problem and "字符上限" in problem for problem in problems)


def test_fnos_package_validator_rejects_version_drift(tmp_path):
    import re

    module = _package_validator()
    package = _validator_fixture(tmp_path)
    manifest_path = package / "manifest"
    manifest = manifest_path.read_text(encoding="utf-8")
    manifest_path.write_text(
        re.sub(r"^version=.*$", "version=9.9.9", manifest, count=1, flags=re.MULTILINE),
        encoding="utf-8",
    )

    problems = module.validate(package, repo_root=ROOT)

    assert any("不一致" in problem for problem in problems)


def test_fnos_package_validator_requires_generated_icons_only_when_staged(tmp_path):
    module = _package_validator()
    package = _validator_fixture(tmp_path)

    assert module.validate(package) == []
    assert any("ICON" in problem for problem in module.validate(package, staged=True))


def test_fnos_builder_fails_loudly_when_fnpack_reports_failure():
    shell = (ROOT / "scripts" / "build-fnos.sh").read_text(encoding="utf-8")

    # fnpack exits 0 even when packing failed: the builder must inspect the
    # output and the artifact instead of trusting the exit code.
    assert "check-fnos-package.py" in shell
    assert "Packing successfully" in shell
    assert "fnpack 报告打包失败" in shell
    assert "未真正重建" in shell
    # vendor download retries instead of aborting the whole build.
    assert "MICAST_FNOS_VENDOR_ATTEMPTS" in shell
    assert "vendor 依赖下载失败" in shell


def test_fnos_health_probe_avoids_a_python_process_per_poll():
    """cmd/main polls /health while enabling; a Python interpreter per probe
    cost ~0.5s each on the NAS and dominated the perceived "enable" time."""
    main = (FNOS / "cmd" / "main").read_text(encoding="utf-8")

    assert "--unix-socket" in main
    assert 'command -v curl' in main
    # The Python fallback must stay for images without curl.
    assert '"$PYTHON_BIN" - "$SOCKET_FILE"' in main
    # curl branch has to come first.
    assert main.index("--unix-socket") < main.index('"$PYTHON_BIN" - "$SOCKET_FILE"')


def test_fnos_startup_grace_survives_a_racy_liveness_scan():
    """The grace window must not kill a starting instance because a single
    process scan failed: the PID file and the scan are both best-effort."""
    main = (FNOS / "cmd" / "main").read_text(encoding="utf-8")

    assert "service_alive()" in main
    # wait_ready consults service_alive instead of a bare scan...
    assert "service_alive || return 1" in main
    # ...and start_service probes health once more before giving up on it.
    grace = main.split("if is_running; then", 1)[1].split("stop_airplay2_runtime", 1)[0]
    assert "is_healthy" in grace
    assert "stop_service_internal" in grace
