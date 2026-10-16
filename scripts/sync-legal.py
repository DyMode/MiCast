"""Generate reviewable Markdown and fnOS consent pages from one bundled source."""

import argparse
import html
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def outputs():
    data = json.loads((ROOT / "web/src/legal-documents.json").read_text(encoding="utf-8"))
    pages = []
    result = {}
    result[ROOT / "packaging/fnos/cmd/legal-version"] = data["version"] + "\n"
    for key, name in (("agreement", "user-agreement.md"), ("privacy", "privacy-policy.md")):
        doc = data[key]
        lines = [f"# {doc['title']}", f"版本与更新日期：{data['version']}"]
        items = [{"type": "tips", "helpText": f"版本与更新日期：{data['version']}"}]
        for section in doc["sections"]:
            lines.extend([f"## {section['title']}", *section["paragraphs"]])
            # Plain, escaped text works without script, external links or a running
            # app. Block-level markup (<p>/<ul>) is avoided: the fnOS wizard strips
            # the tags but keeps a blank line per block, which reads as loose,
            # paragraph-per-line prose. One line per paragraph keeps it tight.
            items.append(
                {
                    "type": "tips",
                    "helpText": "\n".join(
                        [html.escape(section["title"])]
                        + [html.escape(p) for p in section["paragraphs"]]
                    ),
                }
            )
        result[ROOT / "docs" / name] = "\n\n".join(lines) + "\n"
        pages.append({"stepTitle": doc["title"], "items": items})
    pages.append(
        {
            "stepTitle": "阅读并同意后安装",
            "items": [
                {
                    "type": "tips",
                    "helpText": (
                        "安装会自动检测投送功能并分配可调整端口。安装后可在设置中修改，"
                        "并在“关于与支持”中再次查阅上述文件。不同意请取消安装。"
                    ),
                },
                {
                    "type": "checkbox",
                    "field": "wizard_legal_accept",
                    "label": "用户协议与隐私政策",
                    "initValue": "",
                    "options": [
                        {
                            "label": "我已阅读并同意《MiCast 用户协议》和《MiCast 隐私政策》",
                            "value": data["version"],
                        }
                    ],
                    "rules": [
                        {"required": True, "message": "请阅读并同意用户协议与隐私政策后安装"}
                    ],
                },
            ],
        }
    )
    # The callback also checks the exact version, rather than relying only on UI validation.
    result[ROOT / "packaging/fnos/wizard/install"] = (
        json.dumps(pages, ensure_ascii=False, indent=2) + "\n"
    )
    upgrade = json.loads(json.dumps(pages))
    upgrade[-1]["stepTitle"] = "阅读并同意后升级"
    upgrade[-1]["items"][0]["helpText"] = (
        "升级将保留现有配置与登录。请阅读当前版本的用户协议与隐私政策；不同意请取消升级。"
        "共享文件和浏览器数据的清理范围见隐私政策。"
    )
    upgrade[-1]["items"][-1]["rules"][0]["message"] = "请阅读并同意用户协议与隐私政策后升级"
    result[ROOT / "packaging/fnos/wizard/upgrade"] = (
        json.dumps(upgrade, ensure_ascii=False, indent=2) + "\n"
    )
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    for path, content in outputs().items():
        if args.check:
            if not path.exists() or path.read_text(encoding="utf-8") != content:
                raise SystemExit(
                    f"Out of date: {path.relative_to(ROOT)}; run python scripts/sync-legal.py"
                )
        else:
            path.write_text(content, encoding="utf-8", newline="\n")
