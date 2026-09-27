"""Sanitized diagnostic report and clipboard export.

One click in the diagnostics page downloads a JSON bundle with the settings,
live state and logs a maintainer needs to debug a report; the copy button takes
the same log selection as plain text. Everything passes through the sanitizer so
credentials (Xiaomi tokens, webhook secrets, orchestrator keys) never leave the
machine.

The log section is the one part whose size is not fixed, so it is always an
explicit selection (`LogQuery`: source/level plus an optional absolute
interval): the file carries every record in it and states in `log_scope` what
was asked for, what the buffer still had, and whether the first is not covered
by the second.
"""

from __future__ import annotations

import json
import logging
import platform
import re
import sys
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from micast import __version__
from micast.config import settings
from micast.runtime_log import (
    PREVIEW_LIMIT,
    LogQuery,
    LogSelection,
    head_reached_back,
    runtime_logs,
)

logger = logging.getLogger(__name__)

REDACTED = "***"

# key=value / JSON "key": "value" pairs for common credential names
_KV_PATTERN = re.compile(
    r"(?i)\b(pass[_-]?token|service[_-]?token|ssecurity|psecurity|c_user|"
    r"app[_-]?token|access[_-]?token|refresh[_-]?token|sessionToken|token|sid|uid|"
    r"authorization|password|secret|encryption[_-]?key)"
    r"([\"']?\s*[:=]\s*[\"']?)([^\s\"'&,;}]+)"
)
# Cookie headers carry several of the above in one blob
_COOKIE_PATTERN = re.compile(r"(?i)\b(cookie\s*[:=]\s*)([^\n\"']+)")
# Feishu bot webhooks put the secret in the URL path
_FEISHU_PATTERN = re.compile(r"(open\.feishu\.cn/open-apis/bot/v2/hook/)[\w-]+")
# WxPusher secrets ride in the query string
_WXPUSHER_PATTERN = re.compile(r"(?i)((?:appToken|uid|token)=)[\w-]{8,}")

# Settings fields dropped from the report entirely
_SECRET_FIELDS = {"orchestrator_token", "encryption_key"}


def _known_secrets() -> list[str]:
    """Concrete secret values worth redacting wherever they appear."""
    values: list[str] = []
    for raw in (settings.orchestrator_token, settings.encryption_key):
        if raw:
            values.append(raw)
    webhook = settings.notify_webhook_url.strip()
    if webhook:
        parts = urlsplit(webhook)
        tail = parts.path.rstrip("/").rsplit("/", 1)[-1]
        if tail:
            values.append(tail)
        values.extend(value for _, value in parse_qsl(parts.query) if value)
    return [value for value in dict.fromkeys(values) if len(value) >= 6]


def sanitize_text(text: str) -> str:
    text = _KV_PATTERN.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)
    text = _COOKIE_PATTERN.sub(lambda m: f"{m.group(1)}{REDACTED}", text)
    text = _FEISHU_PATTERN.sub(rf"\g<1>{REDACTED}", text)
    text = _WXPUSHER_PATTERN.sub(rf"\g<1>{REDACTED}", text)
    for secret in _known_secrets():
        text = text.replace(secret, REDACTED)
    return text


def sanitize_obj(obj: Any) -> Any:
    """Round-trip through JSON so every string leaf gets scrubbed."""
    try:
        return json.loads(sanitize_text(json.dumps(obj, ensure_ascii=False, default=str)))
    except (TypeError, ValueError):
        logger.exception("Failed to sanitize diagnostic payload")
        return {"error": "payload unavailable"}


def public_settings() -> dict[str, Any]:
    data = settings.model_dump(mode="json")
    for key in _SECRET_FIELDS:
        data.pop(key, None)
    webhook = str(data.get("notify_webhook_url") or "")
    if webhook:
        parts = urlsplit(webhook)
        data["notify_webhook_url"] = f"{parts.scheme}://{parts.netloc}/{REDACTED}"
    return data


async def collect_state(bridge, device_manager) -> dict[str, Any]:
    """Live state snapshot, without the log buffer.

    Local state only: this runs on the page's 1.5s poll and inside the report
    download, i.e. exactly when something is wrong. Asking the cloud for a fresh
    device list here (or building a service to get one) made both endpoints
    raise while the account was unreachable — the diagnostics page stayed empty
    and the report could not be downloaded at all, which is the one thing that
    would have explained why.

    Logs are attached by whoever answers: the page gets a bounded tail preview
    (`log_payload`), the report gets its own selection.
    """
    devices = device_manager.cached_devices()
    return {
        "logged_in": bool(device_manager.auth.stored_identity()[0]),
        "selected_device_id": device_manager.selected_device_id,
        "devices": [
            {
                "did": d.get("deviceID"),
                "name": d.get("name"),
                "hardware": d.get("hardware"),
                "presence": d.get("presence"),
                "miotDID": d.get("miotDID"),
            }
            for d in devices
        ],
        "devices_cached": True,
        "cloud": device_manager.auth.cloud_health(),
        "pcm_source": settings.pcm_source,
        "stream_url": bridge.status["stream_url"],
        "audio_config": settings.audio.model_dump(mode="json"),
        "bridge_status": bridge.status,
        "stream_clients": bridge._stream_server.total_flowing_clients(),
        "stream_bytes_sent": bridge._stream_server.total_bytes(),
        "diagnostics": bridge.diagnostics,
    }


def log_payload(query: LogQuery | None = None) -> dict[str, Any]:
    """The log slice the page polls: a bounded tail plus the numbers it needs.

    `items` is deliberately capped (PREVIEW_LIMIT) even when the range holds
    thousands of records: the panel cannot draw more, and shipping them on every
    1.5s poll would be the old "everything at once" in a new place. `total`,
    `truncated` and `buckets` are what let the UI say how much it is not showing
    and size up a duration before exporting it.
    """
    query = query or LogQuery()
    now = time.time()
    payload = runtime_logs.select(query, preview_limit=PREVIEW_LIMIT).payload(PREVIEW_LIMIT)
    payload["server_time"] = now
    payload["new_count"] = (
        runtime_logs.count_newer_than(query.until, query) if query.until is not None else 0
    )
    payload["buckets"] = runtime_logs.buckets(query, now)
    return payload


def build_log_text(query: LogQuery, selection: LogSelection) -> str:
    """Plain-text dump of a selection, for the clipboard.

    Sanitized like the report: the clipboard is a way off the machine too. The
    header carries the date and the scope so a pasted block explains itself.
    """
    source_label = "应用日志" if query.source == "app" else "全部来源"
    level_label = "仅警告与错误" if query.level == "warn" else "全部级别"
    header = [
        f"# MiCast {__version__} 运行记录 · {source_label} · {level_label}",
        f"# 区间 {_stamp(query.since)} – {_stamp(query.until)} · {selection.total} 条",
    ]
    if head_reached_back(selection, query.since):
        header.append(
            f"# 缓冲区只保存了最近 {selection.buffer_total} 条（上限 {selection.capacity}），"
            "更早的记录已被丢弃"
        )
    lines = [
        f"{item.get('time', '')} {item.get('level', ''):<7} {item.get('logger', '')} "
        f"{item.get('message', '')}"
        for item in selection.records
    ]
    return sanitize_text("\n".join([*header, *lines]))


def _stamp(at: float | None) -> str:
    if at is None:
        return "不限"
    return datetime.fromtimestamp(at).strftime("%Y-%m-%d %H:%M:%S")


async def build_report(
    bridge,
    device_manager,
    *,
    query: LogQuery | None = None,
) -> dict[str, Any]:
    query = query or LogQuery()
    selection = runtime_logs.select(query)
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "app": "MiCast",
        "version": __version__,
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        # Everything the reader needs to judge the log section: what was asked
        # for, what the buffer actually still had, and whether the first is not
        # covered by the second.
        "log_scope": {
            "source": query.source,
            "level": query.level,
            "requested": {"from": query.since, "to": query.until},
            "covered": {"from": selection.covered_from, "to": selection.covered_to},
            "count": selection.total,
            "buffer_total": selection.buffer_total,
            "buffer_capacity": selection.capacity,
            "truncated": head_reached_back(selection, query.since),
        },
        "settings": sanitize_obj(public_settings()),
        "state": sanitize_obj(await collect_state(bridge, device_manager)),
        "logs": sanitize_obj(selection.records),
    }
