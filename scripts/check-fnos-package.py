"""Pre-flight validation for the fnOS package under ``packaging/fnos``.

``fnpack build`` reports content problems on stdout but still exits 0, so a
broken package silently produced no artifact and the previous ``.fpk`` stayed
behind in ``dist/``. This validator encodes the rules fnpack enforces — the
messages below mirror the ones its binary emits — so the build fails *before*
staging, with a file-level explanation, instead of pretending to succeed.

Run standalone (``python scripts/check-fnos-package.py``) or import
``validate`` from tests.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Item types fnpack accepts; anything else fails "wizard item type %s is invalid".
ITEM_TYPES = {"tips", "switch", "text", "checkbox", "radio", "select", "password"}
# Types whose options are a required, non-empty list.
OPTION_TYPES = {"checkbox", "radio", "select"}
# Types that address a wizard field by name.
FIELD_TYPES = {"switch", "text", "checkbox", "radio", "select", "password"}
# fnpack refuses longer text defaults.
TEXT_INIT_VALUE_LIMIT = 1024

REQUIRED_TOP_LEVEL = ("manifest",)
# Icons are generated from web/public/icons while staging, so they only have to
# exist in the staged package, not in packaging/fnos.
STAGED_TOP_LEVEL = ("ICON.PNG", "ICON_256.PNG")
REQUIRED_DIRS = ("app", "cmd", "config", "wizard")
REQUIRED_CMD_FILES = (
    "service-process.py",
    "lifecycle.py",
    "legal-version",
    "main",
    "install_init",
    "install_callback",
    "upgrade_init",
    "upgrade_callback",
    "uninstall_init",
    "uninstall_callback",
    "config_init",
    "config_callback",
)
REQUIRED_WIZARDS = ("install", "upgrade", "uninstall")
REQUIRED_JSON = (
    "config/privilege",
    "config/resource",
    "app/ui/config",
)
MANIFEST_KEYS = ("appname", "version", "platform", "install_dep_apps", "maintainer")


def parse_manifest(path: Path) -> dict[str, str]:
    """Parse key=value lines, honouring fnOS triple-quoted multiline values."""
    result: dict[str, str] = {}
    key = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if key is not None:
            if line.rstrip().endswith('"""'):
                key = None
            continue
        if not line or line.startswith("#"):
            continue
        name, _, value = line.partition("=")
        if value.startswith('"""') and not value.rstrip().endswith('"""'):
            key = name
        result[name] = value
    return result


def app_version(root: Path) -> str:
    import re

    source = (root / "micast" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'__version__\s*=\s*"([^"]+)"', source)
    return match.group(1) if match else ""


def _check_item(where: str, item: object, problems: list[str]) -> None:
    if not isinstance(item, dict):
        problems.append(f"{where}: 不是对象")
        return
    kind = item.get("type")
    if kind not in ITEM_TYPES:
        problems.append(f"{where}: 未知控件类型 {kind!r}")
        return

    if kind == "tips":
        if not str(item.get("helpText", "")).strip():
            problems.append(f"{where}: tips 的 helpText 为空")
    else:
        if not str(item.get("label", "")).strip():
            problems.append(f"{where}: {kind} 的 label 为空")

    if kind in FIELD_TYPES and not str(item.get("field", "")).strip():
        problems.append(f"{where}: {kind} 的 field 为空")

    if kind in OPTION_TYPES:
        options = item.get("options")
        if not isinstance(options, list) or not options:
            problems.append(f"{where}: {kind} 的 options 为空")
        else:
            for index, option in enumerate(options):
                if not isinstance(option, dict):
                    problems.append(f"{where}.options[{index}]: 不是对象")
                    continue
                if not str(option.get("value", "")).strip():
                    problems.append(f"{where}.options[{index}]: value 为空")
                if not str(option.get("label", "")).strip():
                    problems.append(f"{where}.options[{index}]: label 为空")
        # fnpack serialises these controls as a single string value; a list
        # (the natural "unchecked" spelling) fails content validation with the
        # generic JSON error that cost a whole release build.
        if "initValue" in item and not isinstance(item["initValue"], str):
            problems.append(
                f"{where}: {kind} 的 initValue 必须是字符串"
                f"（未勾选写 \"\"，已勾选写选项 value），当前为 "
                f"{type(item['initValue']).__name__}"
            )

    if kind == "text":
        value = item.get("initValue", "")
        if not isinstance(value, str):
            problems.append(f"{where}: text 的 initValue 必须是字符串")
        elif len(value) > TEXT_INIT_VALUE_LIMIT:
            problems.append(
                f"{where}: text 的 initValue 超过 {TEXT_INIT_VALUE_LIMIT} 字符上限"
            )

    for index, rule in enumerate(item.get("rules") or []):
        if not isinstance(rule, dict):
            problems.append(f"{where}.rules[{index}]: 不是对象")
            continue
        if rule.get("required") and not str(rule.get("message", "")).strip():
            problems.append(f"{where}.rules[{index}]: required 规则缺少 message")


def _check_wizard(where: str, payload: object, problems: list[str]) -> None:
    if not isinstance(payload, list) or not payload:
        problems.append(f"{where}: 步骤列表为空")
        return
    for step_index, step in enumerate(payload):
        step_where = f"{where}[{step_index}]"
        if not isinstance(step, dict):
            problems.append(f"{step_where}: 不是对象")
            continue
        if not str(step.get("stepTitle", "")).strip():
            problems.append(f"{step_where}: stepTitle 为空")
        items = step.get("items")
        if not isinstance(items, list) or not items:
            problems.append(f"{step_where}: items 为空")
            continue
        for item_index, item in enumerate(items):
            _check_item(f"{step_where}.items[{item_index}]", item, problems)


def validate(
    package: Path = ROOT / "packaging" / "fnos",
    *,
    repo_root: Path | None = None,
    staged: bool = False,
) -> list[str]:
    """Return human-readable problems; empty list means fnpack should accept it.

    ``package`` is the package directory itself — ``packaging/fnos`` before
    staging, or the staging directory afterwards. ``staged`` additionally
    requires the generated icons; ``repo_root`` enables the version cross-check
    against ``micast/__init__.py``.
    """
    problems: list[str] = []

    if not package.is_dir():
        return [f"{package}: 目录不存在"]

    for name in REQUIRED_TOP_LEVEL:
        if not (package / name).exists():
            problems.append(f"{name}: 缺少必需文件")
    if staged:
        for name in STAGED_TOP_LEVEL:
            if not (package / name).exists():
                problems.append(f"{name}: 缺少必需文件")
    for name in REQUIRED_DIRS:
        if not (package / name).is_dir():
            problems.append(f"{name}/: 缺少必需目录")
    for name in REQUIRED_CMD_FILES:
        if not (package / "cmd" / name).is_file():
            problems.append(f"cmd/{name}: 缺少生命周期脚本")

    manifest_path = package / "manifest"
    if manifest_path.is_file():
        manifest = parse_manifest(manifest_path)
        for key in MANIFEST_KEYS:
            if not manifest.get(key, "").strip():
                problems.append(f"manifest: 缺少字段 {key}")
        if repo_root is not None:
            expected = app_version(repo_root)
            if expected and manifest.get("version", "").strip() != expected:
                problems.append(
                    f"manifest: version={manifest.get('version', '')} 与 "
                    f"micast/__init__.py 的 {expected} 不一致"
                )

    for name in REQUIRED_JSON:
        path = package / name
        if not path.exists():
            continue
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            problems.append(f"{name}: JSON 无法解析（{exc}）")

    for name in REQUIRED_WIZARDS:
        path = package / "wizard" / name
        if not path.exists():
            problems.append(f"wizard/{name}: 缺少向导文件")
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            problems.append(f"wizard/{name}: JSON 无法解析（{exc}）")
            continue
        _check_wizard(f"wizard/{name}", payload, problems)

    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description="校验 fnOS 包内容是否会被 fnpack 接受")
    parser.add_argument(
        "--package",
        type=Path,
        default=ROOT / "packaging" / "fnos",
        help="包目录（源码目录或 staging 目录）",
    )
    parser.add_argument("--repo-root", type=Path, help="仓库根目录，用于版本一致性校验")
    parser.add_argument(
        "--staged",
        action="store_true",
        help="按 staging 目录校验（额外要求已生成的图标）",
    )
    args = parser.parse_args()

    problems = validate(
        args.package.resolve(),
        repo_root=args.repo_root.resolve() if args.repo_root else None,
        staged=args.staged,
    )
    if problems:
        print("fnOS 包校验失败：", flush=True)
        for problem in problems:
            print(f"  - {problem}", flush=True)
        return 1
    print("fnOS 包校验通过", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
