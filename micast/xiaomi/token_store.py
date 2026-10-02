"""Encrypted persistent storage for Xiaomi tokens."""

import json
import logging
import platform
import subprocess
import uuid
from pathlib import Path

from cryptography.fernet import Fernet

from micast.config import settings
from micast.config_store import write_bytes

logger = logging.getLogger(__name__)


def _get_machine_id() -> str:
    """Return a stable machine identifier for key derivation."""
    # Try MAC address first (cross-platform)
    node = uuid.getnode()
    if node:
        return f"micast-{node:012x}"

    system = platform.system()
    try:
        if system == "Windows":
            result = subprocess.run(
                ["powershell", "-Command", "(Get-CimInstance Win32_ComputerSystemProduct).UUID"],
                capture_output=True,
                text=True,
                check=False,
            )
            uid = result.stdout.strip()
            if uid and uid != "FFFFFFFF-FFFF-FFFF-FFFF-FFFFFFFFFFFF":
                return uid
        elif system == "Linux":
            for path in ["/etc/machine-id", "/var/lib/dbus/machine-id"]:
                p = Path(path)
                if p.exists():
                    return p.read_text().strip()
        elif system == "Darwin":
            result = subprocess.run(
                ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                capture_output=True,
                text=True,
                check=False,
            )
            for line in result.stdout.splitlines():
                if "IOPlatformUUID" in line:
                    return line.split('"')[-2]
    except Exception:
        logger.exception("Failed to read machine id")
        logger.warning(
            "无法读取机器特征，加密密钥回退到内置默认 key：token 文件绑定此回退 key，"
            "换机、重装系统或更换安装目录后都会无法解密，届时需要重新扫码登录。"
            "如需跨机迁移，请设置 MICAST_ENCRYPTION_KEY 环境变量固定密钥。"
        )

    return "micast-default-key"


def _derive_key() -> bytes:
    """Derive a Fernet key from env var or machine id."""
    raw = settings.encryption_key or _get_machine_id()
    # Fernet keys must be 32 url-safe base64-encoded bytes
    from base64 import urlsafe_b64encode
    from hashlib import sha256

    hashed = sha256(raw.encode("utf-8")).digest()
    return urlsafe_b64encode(hashed)


class TokenStore:
    """Encrypted JSON token store."""

    def __init__(self, path: Path | None = None):
        self.path = path or (settings.config_path.parent / "xiaomi-tokens.enc")
        self._fernet = Fernet(_derive_key())

    def save(self, tokens: dict) -> None:
        """Encrypt and save tokens."""
        data = json.dumps(tokens, ensure_ascii=False).encode("utf-8")
        encrypted = self._fernet.encrypt(data)
        write_bytes(self.path, encrypted)

    def load(self) -> dict | None:
        """Load and decrypt tokens."""
        if not self.path.exists():
            return None
        try:
            encrypted = self.path.read_bytes()
            data = self._fernet.decrypt(encrypted)
            return json.loads(data.decode("utf-8"))
        except Exception:
            logger.exception("Failed to decrypt tokens")
            return None

    def clear(self) -> None:
        """Remove stored tokens."""
        if self.path.exists():
            self.path.unlink()
