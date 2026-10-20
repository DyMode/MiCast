"""Generate reviewable Markdown and the fnOS official LICENSE from one source."""

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def outputs():
    data = json.loads((ROOT / "web/src/legal-documents.json").read_text(encoding="utf-8"))
    result = {}
    result[ROOT / "packaging/fnos/cmd/legal-version"] = data["version"] + "\n"
    for key, name in (("agreement", "user-agreement.md"), ("privacy", "privacy-policy.md")):
        doc = data[key]
        lines = [f"# {doc['title']}", f"版本与更新日期：{data['version']}"]
        for section in doc["sections"]:
            lines.extend([f"## {section['title']}", *section["paragraphs"]])
        result[ROOT / "docs" / name] = "\n\n".join(lines) + "\n"

    def license_text():
        parts = []
        for key in ("agreement", "privacy"):
            doc = data[key]
            parts.append(f"{doc['title']}\n版本与更新日期：{data['version']}")
            for section in doc["sections"]:
                parts.append(f"{section['title']}\n" + "\n".join(section["paragraphs"]))
        return "\n\n".join(parts) + "\n"

    # fnOS shows the package-root LICENSE as the official agreement. No custom
    # install/upgrade wizard is shipped, so the installer goes straight from its
    # own pages to that agreement.
    result[ROOT / "packaging/fnos/LICENSE"] = license_text()
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
