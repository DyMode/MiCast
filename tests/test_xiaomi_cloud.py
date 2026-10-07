"""Wire contracts for the owned cloud client, without touching real accounts."""

import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from micast.xiaomi.cloud import MiAccount, MiIOService, MiNAService, XiaomiCloudError


class Response:
    def __init__(self, body, status=200):
        self.body, self.status = body, status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def json(self, **kwargs):
        return self.body


@pytest.mark.asyncio
async def test_request_failure_preserves_credentials_and_redacts_response():
    tokens = {"userId": "1", "passToken": "secret", "micoapi": ["security", "service"]}
    calls = []

    def request(*args, **kwargs):
        calls.append((args, kwargs))
        return Response({"code": 401, "message": "secret"}, 401)

    account = MiAccount(SimpleNamespace(request=request), tokens, "test-agent")
    with pytest.raises(XiaomiCloudError) as exc:
        await MiNAService(account).device_list()
    assert exc.value.status == 401
    assert "secret" not in str(exc.value)
    assert len(calls) == 1  # no password login or retry in this layer
    assert tokens["passToken"] == "secret"
    assert account.token == tokens


@pytest.mark.asyncio
async def test_missing_sid_makes_no_network_request():
    account = MiAccount(None, {"userId": "1", "micoapi": ["s", "t"]}, "agent")
    with pytest.raises(XiaomiCloudError):
        await MiIOService(account).device_list()


@pytest.mark.asyncio
async def test_cloud_request_uses_saved_service_token():
    calls = []

    def request(*args, **kwargs):
        calls.append((args, kwargs))
        return Response({"code": 0, "data": [{"deviceID": "speaker", "hardware": "OH2"}]})

    service = MiNAService(
        MiAccount(
            SimpleNamespace(request=request),
            {
                "userId": "1",
                "micoapi": ["security", "service-token"],
            },
            "agent",
        )
    )
    assert (await service.device_list())[0]["hardware"] == "OH2"
    assert calls[0][0][0] == "GET"
    assert calls[0][0][1].startswith("https://api2.mina.mi.com/admin/v2/device_list?")
    assert calls[0][1]["cookies"] == {"userId": "1", "serviceToken": "service-token"}
    assert service.device2hardware == {"speaker": "OH2"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model,method",
    [("OH2", "player_play_music"), ("OH2P", "player_play_music"), ("LX06", "player_play_url")],
)
async def test_model_routing_uses_cached_devices_without_extra_cloud_lookup(model, method):
    account = SimpleNamespace(mi_request=AsyncMock(return_value={"code": 0}))
    service = MiNAService(account)
    service.cache_devices([{"deviceID": "speaker", "hardware": model}])
    await service.play_by_url("speaker", "http://local/music.wav")
    assert account.mi_request.await_count == 1
    command = account.mi_request.await_args.args[2]
    assert command["method"] == method
    message = json.loads(command["message"])
    if model == "LX06":
        assert message["url"] == "http://local/music.wav"
    else:
        music = json.loads(message["music"])
        assert music["payload"]["audio_items"][0]["stream"]["url"] == "http://local/music.wav"


@pytest.mark.asyncio
async def test_nested_command_failure_is_not_reported_as_success():
    service = MiNAService(
        SimpleNamespace(
            mi_request=AsyncMock(
                return_value={
                    "code": 0,
                    "data": {"code": -1, "info": "private"},
                }
            )
        )
    )
    with pytest.raises(XiaomiCloudError) as exc:
        await service.player_pause("speaker")
    assert exc.value.code == -1
    assert "private" not in str(exc.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model,methods",
    [
        ("LX06", ["player_play_url", "player_play_music"]),
        ("OH2", ["player_play_music", "player_play_url"]),
        ("OH2P", ["player_play_music", "player_play_url"]),
    ],
)
async def test_manager_api_prefers_model_and_fallback_tries_other_method(model, methods):
    from micast.xiaomi.mina_api import MinaAPI

    account = SimpleNamespace(
        mi_request=AsyncMock(
            side_effect=[
                XiaomiCloudError("play", status=500),
                {"code": 0},
            ]
        )
    )
    service = MiNAService(account)
    service.cache_devices([{"deviceID": "speaker", "hardware": model}])
    api = MinaAPI(service, "speaker")
    with pytest.raises(XiaomiCloudError):
        await api.play_music_url("http://local/music.wav")
    await api.play_url("http://local/music.wav")
    assert [call.args[2]["method"] for call in account.mi_request.await_args_list] == methods


def test_miot_signature_matches_known_wire_vector(monkeypatch):
    from micast.xiaomi import cloud

    monkeypatch.setattr(cloud.secrets, "token_bytes", lambda n: bytes(range(n)))
    monkeypatch.setattr(cloud.time, "time", lambda: 1700000000)
    signed = MiIOService.sign_data(
        "/home/device_list", {"getVirtualModel": False}, base64.b64encode(b"test-security").decode()
    )
    assert signed["_nonce"] == "AAECAwQFBgcBsFUV"
    assert signed["signature"] == "D6y5zMR19u8hfz/qC05P2KHLwABhe+we6AjRzM1gsuU="


@pytest.mark.asyncio
async def test_miot_action_preserves_wire_shape_and_business_error():
    account = SimpleNamespace(
        mi_request=AsyncMock(return_value={"code": 0, "result": {"code": -4}})
    )
    service = MiIOService(account)
    assert await service.miot_action("speaker", (5, 3), ["hello"]) == -4
    args = account.mi_request.await_args.args
    cookies = {}
    signed = args[2](
        {"deviceId": "device", "xiaomiio": [base64.b64encode(b"s").decode(), "t"]}, cookies
    )
    assert json.loads(signed["data"])["params"] == {
        "did": "speaker",
        "siid": 5,
        "aiid": 3,
        "in": ["hello"],
    }
    assert cookies == {"PassportDeviceId": "device"}
