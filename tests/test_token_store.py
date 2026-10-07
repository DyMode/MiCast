"""Token-store key derivation: a fabricated getnode() address must not decide the key."""

import uuid
from pathlib import Path

import micast.xiaomi.token_store as token_store


def _machine_id(monkeypatch, node, macs=(), machine_id=None):
    monkeypatch.setattr(uuid, "getnode", lambda: node)
    monkeypatch.setattr(token_store, "_real_macs", lambda: set(macs))
    monkeypatch.setattr(token_store.platform, "system", lambda: "Linux")
    if machine_id is not None:
        path = Path(machine_id)
        monkeypatch.setattr(Path, "exists", lambda self: self == path)
        monkeypatch.setattr(Path, "read_text", lambda self, *a, **k: "stable-machine-id" if self == path else "")
    return token_store._get_machine_id()


def test_key_uses_real_mac_when_nic_is_readable(monkeypatch):
    assert _machine_id(monkeypatch, 0xAABBCCDDEEFF, macs=("aa:bb:cc:dd:ee:ff",)) == "micast-aabbccddeeff"


def test_key_falls_back_to_machine_id_when_getnode_fabricates(monkeypatch):
    # getnode() returns a random per-process address that matches no real
    # interface; the key must then come from machine-id, not the random value.
    first = _machine_id(monkeypatch, 0x1234567890AB, macs=("aa:bb:cc:dd:ee:ff",),
                        machine_id="/etc/machine-id")
    second = _machine_id(monkeypatch, 0xFEDCBA098765, macs=("aa:bb:cc:dd:ee:ff",),
                         machine_id="/etc/machine-id")
    assert first == second
    assert "micast-" not in first


def test_windows_keeps_existing_real_mac_key(monkeypatch):
    monkeypatch.setattr(uuid, "getnode", lambda: 0xAABBCCDDEEFF)
    monkeypatch.setattr(token_store, "_real_macs", lambda: set())
    monkeypatch.setattr(token_store.platform, "system", lambda: "Windows")
    assert token_store._get_machine_id() == "micast-aabbccddeeff"
