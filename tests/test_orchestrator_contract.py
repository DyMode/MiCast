import pytest
from pydantic import ValidationError

from micast.orchestrator_app import ReceiverSpec, _container_name, _spec_hash


def test_receiver_contract_rejects_shell_sensitive_names():
    with pytest.raises(ValidationError):
        ReceiverSpec(key="single", device_id="speaker-1", name='bad " name')


def test_container_name_and_spec_hash_are_stable():
    spec = ReceiverSpec(
        key="speaker-device-123",
        device_id="device-123",
        name="厨房小爱",
        protocol="auto",
    )

    assert _container_name(spec.key) == _container_name(spec.key)
    assert _container_name(spec.key).startswith("micast-receiver-")
    assert _spec_hash(spec) == _spec_hash(spec)


def test_disconnect_only_recreates_named_managed_receiver(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock

    import micast.orchestrator_app as service

    def container(key):
        return SimpleNamespace(
            reload=Mock(), remove=Mock(), labels={service.KEY_LABEL: key},
            attrs={"Config": {"Env": [
                f"MICAST_DEVICE_ID={key}", f"MICAST_AIRPLAY_NAME={key}",
                "MICAST_AIRPLAY_PROTOCOL=auto",
            ]}},
        )
    first, other = container("first"), container("other")
    client = SimpleNamespace(containers=SimpleNamespace(list=Mock(return_value=[first, other])),
                             close=Mock())
    monkeypatch.setattr(service.docker, "from_env", lambda: client)
    reconcile = Mock(return_value=[SimpleNamespace(key="first", status="running", epoch="new")])
    monkeypatch.setattr(service, "_reconcile_locked", reconcile)
    assert service._disconnect_receiver_sync("first") == {"ok": True, "epoch": "new"}
    first.remove.assert_called_once_with(force=True)
    other.remove.assert_not_called()
    assert {item.key for item in reconcile.call_args.args[1]} == {"first", "other"}
    client.containers.list.assert_called_once_with(
        all=True, filters={"label": f"{service.MANAGED_LABEL}=true"},
    )
    with pytest.raises(service.HTTPException) as error:
        service._disconnect_receiver_sync("unknown")
    assert error.value.status_code == 404
    assert first.remove.call_count == 1
