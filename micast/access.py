"""Local administrator access and first-deployment state."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from contextlib import suppress
from pathlib import Path

from micast.config import settings
from micast.config_store import write_json

COOKIE_NAME = "micast_session"
SESSION_AGE = 30 * 24 * 60 * 60


class AccessManager:
    def __init__(self, path: Path | None = None):
        self.path = path or settings.config_path.parent / "access.json"
        self._data = self._load()

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (OSError, ValueError):
            pass
        return {
            "access_configured": False,
            "setup_complete": False,
            "auth_enabled": False,
            "username": "admin",
            "password_hash": "",
            "salt": "",
            "secret": secrets.token_hex(32),
            "auth_version": 1,
        }

    def _save(self) -> None:
        write_json(self.path, self._data)

    def reset(self) -> None:
        """Wipe credentials and setup flags (清空数据 → 回到引导页)."""
        with suppress(OSError):
            self.path.unlink()
        self._data = self._load()  # file is gone → fresh defaults

    @property
    def access_configured(self) -> bool:
        return bool(self._data.get("access_configured"))

    @property
    def setup_complete(self) -> bool:
        return bool(self._data.get("setup_complete"))

    @property
    def auth_enabled(self) -> bool:
        return bool(self._data.get("auth_enabled"))

    @property
    def username(self) -> str:
        return str(self._data.get("username") or "admin")

    @staticmethod
    def _hash(password: str, salt: bytes) -> str:
        raw = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
        return base64.urlsafe_b64encode(raw).decode()

    def configure(self, *, enabled: bool, username: str = "admin", password: str = "") -> None:
        username = username.strip() or "admin"
        if len(username) > 64:
            raise ValueError("用户名不能超过 64 个字符")
        if enabled and len(password) < 6:
            raise ValueError("密码至少需要 6 个字符")
        salt = secrets.token_bytes(16) if enabled else b""
        self._data.update(
            {
                "access_configured": True,
                "auth_enabled": enabled,
                "username": username,
                "salt": base64.urlsafe_b64encode(salt).decode() if salt else "",
                "password_hash": self._hash(password, salt) if enabled else "",
                "auth_version": int(self._data.get("auth_version", 0)) + 1,
            }
        )
        self._data.setdefault("secret", secrets.token_hex(32))
        self._save()

    def complete_setup(self) -> None:
        if not self.access_configured:
            raise ValueError("请先完成管理访问设置")
        self._data["setup_complete"] = True
        self._save()

    def verify_password(self, username: str, password: str) -> bool:
        if not self.auth_enabled:
            return True
        if not hmac.compare_digest(username, self.username):
            return False
        try:
            salt = base64.urlsafe_b64decode(self._data["salt"])
            candidate = self._hash(password, salt)
        except (KeyError, ValueError):
            return False
        return hmac.compare_digest(candidate, str(self._data.get("password_hash", "")))

    def issue_session(self) -> str:
        issued = int(time.time())
        payload = f"{self.username}|{self._data.get('auth_version', 1)}|{issued}"
        signature = hmac.new(
            str(self._data["secret"]).encode(), payload.encode(), hashlib.sha256
        ).hexdigest()
        return base64.urlsafe_b64encode(f"{payload}|{signature}".encode()).decode()

    def valid_session(self, token: str | None) -> bool:
        if not self.auth_enabled:
            return True
        if not token:
            return False
        try:
            decoded = base64.urlsafe_b64decode(token.encode()).decode()
            username, version, issued, signature = decoded.rsplit("|", 3)
            payload = f"{username}|{version}|{issued}"
            expected = hmac.new(
                str(self._data["secret"]).encode(), payload.encode(), hashlib.sha256
            ).hexdigest()
            return (
                hmac.compare_digest(signature, expected)
                and hmac.compare_digest(username, self.username)
                and int(version) == int(self._data.get("auth_version", 1))
                and time.time() - int(issued) <= SESSION_AGE
            )
        except (ValueError, TypeError):
            return False
