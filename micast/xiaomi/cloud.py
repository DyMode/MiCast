"""MiCast-owned Xiaomi cloud transport and speaker protocol.

Credential exchange and recovery belong to XiaomiAuth. This layer never
relogs with an empty password, changes credentials, or logs response bodies.
"""

import base64
import hashlib
import hmac
import json
import secrets
import time
from copy import deepcopy
from urllib.parse import urlencode

import aiohttp

MUSIC_MODELS = frozenset(
    {
        "LX04",
        "LX05",
        "L05B",
        "L05C",
        "L06",
        "L06A",
        "X08A",
        "X10A",
        "X08C",
        "X08E",
        "X8F",
        "X4B",
        "OH2",
        "OH2P",
        "X6A",
    }
)


class XiaomiCloudError(RuntimeError):
    """Safe error metadata, without credentials or server response bodies."""

    def __init__(self, operation: str, *, status: int = 0, code: int | None = None):
        self.status = status
        self.code = code
        super().__init__(f"小米云端请求失败（{operation}，HTTP {status}，code={code}）")


class MiAccount:
    """Immutable credential snapshot sharing the auth-owned HTTP session."""

    def __init__(self, session: aiohttp.ClientSession, tokens: dict, user_agent: str):
        self.session = session
        self.token = deepcopy(tokens)
        self.now_ua = user_agent

    async def mi_request(self, sid: str, url: str, data=None, headers=None) -> dict:
        pair = self.token.get(sid)
        if not self.token.get("userId") or not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise XiaomiCloudError(sid, status=401)
        cookies = {"userId": str(self.token["userId"]), "serviceToken": pair[1]}
        content = data(self.token, cookies) if callable(data) else data
        request_headers = {"User-Agent": self.now_ua, **(headers or {})}
        async with self.session.request(
            "GET" if data is None else "POST",
            url,
            data=content,
            headers=request_headers,
            cookies=cookies,
        ) as response:
            if response.status != 200:
                raise XiaomiCloudError(sid, status=response.status)
            try:
                result = await response.json(content_type=None)
            except (ValueError, UnicodeError) as exc:
                raise XiaomiCloudError(sid, status=200) from exc
            if not isinstance(result, dict) or result.get("code") != 0:
                code = result.get("code") if isinstance(result, dict) else None
                raise XiaomiCloudError(sid, status=200, code=code)
            return result


class MiNAService:
    """Cloud MiNA commands; returns complete responses for diagnostics."""

    def __init__(self, account: MiAccount):
        self.account = account
        self.device2hardware: dict[str, str] = {}

    async def mina_request(self, uri: str, data: dict | None = None) -> dict:
        request_id = "app_ios_" + secrets.token_hex(15)
        if data is None:
            uri += ("&" if "?" in uri else "?") + urlencode({"requestId": request_id})
        else:
            data = {**data, "requestId": request_id}
        return await self.account.mi_request("micoapi", "https://api2.mina.mi.com" + uri, data)

    def cache_devices(self, devices: list[dict]) -> None:
        self.device2hardware = {
            str(d["deviceID"]): str(d["hardware"])
            for d in devices
            if d.get("deviceID") and d.get("hardware")
        }

    async def device_list(self, master: int = 0) -> list[dict]:
        response = await self.mina_request(f"/admin/v2/device_list?master={master}")
        devices = response.get("data")
        if not isinstance(devices, list):
            raise XiaomiCloudError("device_list", status=200)
        self.cache_devices(devices)
        return devices

    async def ubus_request(self, deviceId: str, method: str, path: str, message: dict) -> dict:
        response = await self.mina_request(
            "/remote/ubus",
            {
                "deviceId": deviceId,
                "method": method,
                "path": path,
                "message": json.dumps(message, ensure_ascii=False),
            },
        )
        detail = response.get("data")
        if isinstance(detail, dict) and detail.get("code", 0) != 0:
            raise XiaomiCloudError(method, status=200, code=detail["code"])
        return response

    async def _player(self, did: str, method: str, **values) -> dict:
        return await self.ubus_request(did, method, "mediaplayer", {"media": "app_ios", **values})

    async def player_set_volume(self, deviceId: str, volume: int) -> dict:
        return await self._player(deviceId, "player_set_volume", volume=volume)

    async def player_pause(self, deviceId: str) -> dict:
        return await self._player(deviceId, "player_play_operation", action="pause")

    async def player_stop(self, deviceId: str) -> dict:
        return await self._player(deviceId, "player_play_operation", action="stop")

    async def player_play(self, deviceId: str) -> dict:
        return await self._player(deviceId, "player_play_operation", action="play")

    async def player_get_status(self, deviceId: str) -> dict:
        return await self._player(deviceId, "player_get_play_status")

    async def text_to_speech(self, deviceId: str, text: str) -> dict:
        return await self.ubus_request(deviceId, "text_to_speech", "mibrain", {"text": text})

    async def play_by_url(self, deviceId: str, url: str, _type: int = 2) -> dict:
        if self.device2hardware.get(deviceId) in MUSIC_MODELS:
            return await self.play_by_music_url(deviceId, url, _type)
        return await self.play_plain_url(deviceId, url, _type)

    async def play_plain_url(self, deviceId: str, url: str, _type: int = 2) -> dict:
        return await self._player(deviceId, "player_play_url", url=url, type=_type)

    async def play_by_music_url(
        self,
        deviceId: str,
        url: str,
        _type: int = 2,
        audio_id: str | None = "1582971365183456177",
        id: str = "355454500",
    ) -> dict:
        audio_id = audio_id or "1582971365183456177"
        music = {
            "payload": {
                "audio_type": "MUSIC" if _type == 1 else "",
                "audio_items": [
                    {
                        "item_id": {
                            "audio_id": audio_id,
                            "cp": {
                                "album_id": "-1",
                                "episode_index": 0,
                                "id": id,
                                "name": "xiaowei",
                            },
                        },
                        "stream": {"url": url},
                    }
                ],
                "list_params": {
                    "listId": "-1",
                    "loadmore_offset": 0,
                    "origin": "xiaowei",
                    "type": "MUSIC",
                },
            },
            "play_behavior": "REPLACE_ALL",
        }
        return await self.ubus_request(
            deviceId,
            "player_play_music",
            "mediaplayer",
            {
                "startaudioid": audio_id,
                "music": json.dumps(music),
            },
        )


class MiIOService:
    """Signed MIoT cloud calls, isolated from the MiNA credential snapshot."""

    def __init__(self, account: MiAccount):
        self.account = account

    @staticmethod
    def sign_data(uri: str, data: dict, security: str) -> dict:
        payload = json.dumps(data)
        nonce_bytes = secrets.token_bytes(8) + int(time.time() // 60).to_bytes(4, "big")
        nonce = base64.b64encode(nonce_bytes).decode()
        signed_nonce = hashlib.sha256(base64.b64decode(security) + nonce_bytes).digest()
        message = "&".join((uri, base64.b64encode(signed_nonce).decode(), nonce, "data=" + payload))
        signature = hmac.new(signed_nonce, message.encode(), hashlib.sha256).digest()
        return {"_nonce": nonce, "data": payload, "signature": base64.b64encode(signature).decode()}

    async def miio_request(self, uri: str, data: dict):
        def prepare(tokens, cookies):
            cookies["PassportDeviceId"] = tokens.get("deviceId", "")
            return self.sign_data(uri, data, tokens["xiaomiio"][0])

        response = await self.account.mi_request(
            "xiaomiio",
            "https://api.io.mi.com/app" + uri,
            prepare,
            {"x-xiaomi-protocal-flag-cli": "PROTOCAL-HTTP2"},
        )
        if "result" not in response:
            raise XiaomiCloudError(uri, status=200)
        return response["result"]

    async def miot_action(self, did: str, iid: tuple[int, int], args: list | None = None) -> int:
        result = await self.miio_request(
            "/miotspec/action",
            {
                "params": {"did": did, "siid": iid[0], "aiid": iid[1], "in": args or []},
            },
        )
        return result.get("code", -1)

    async def device_list(self) -> list[dict]:
        result = await self.miio_request(
            "/home/device_list",
            {
                "getVirtualModel": False,
                "getHuamiDevices": 0,
            },
        )
        devices = result.get("list")
        if not isinstance(devices, list):
            raise XiaomiCloudError("device_list", status=200)
        return devices
