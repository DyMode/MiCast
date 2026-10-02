"""Xiaomi account authentication: QR and cookie login."""

import asyncio
import base64
import hashlib
import json
import logging
import time
from urllib.parse import quote, urlencode

import aiohttp
from miservice import MiAccount, MiIOService, MiNAService, MiTokenStore

from micast.config import settings
from micast.config_store import write_json
from micast.net import new_session
from micast.xiaomi.token_store import TokenStore

logger = logging.getLogger(__name__)

# Consecutive failed cloud calls, still this fresh, mean the account cannot do
# anything useful right now (see cloud_degraded).
CLOUD_DEGRADED_FAILURES = 3
CLOUD_FAILURE_FRESH_SECONDS = 600.0
# One credential re-check per minute, even if calls keep failing. Every failed
# call used to trigger its own verification exchange, which doubled the cloud
# traffic exactly when the cloud was already unreachable.
CLOUD_VERIFY_MIN_INTERVAL_SECONDS = 60.0

UA = "APP/com.xiaomi.mihome APPV/6.0.103 iosPassportSDK/3.9.0 iOS/14.4 miHSTS"
SID = "micoapi"
QR_SID = "xiaomiio"  # QR login uses xiaomiio, then exchange for micoapi
ACCOUNT_HOST = "account.xiaomi.com"
# A QR request reaches Xiaomi's account servers, which may be out of reach from
# this device. Bounded here (and again in the page) so a failed attempt ends
# with a reason instead of an open request and a blank code.
QR_START_TIMEOUT_SECONDS = 12.0


def _parse_json(text: str) -> dict:
    if text.startswith("&&&START&&&"):
        text = text[11:]
    return json.loads(text)


class XiaomiAuthError(Exception):
    pass


class XiaomiAuth:
    """Handles Xiaomi QR/cookie login and provides a MiNAService instance."""

    def __init__(self):
        self._token_store = TokenStore()
        self._session: aiohttp.ClientSession | None = None
        self._cloud_failures = 0
        self._cloud_last_ok_at = 0.0
        self._cloud_last_failure_at = 0.0
        self._last_recovery_attempt_at = 0.0
        self._account: MiAccount | None = None
        self._miot_account: MiAccount | None = None
        self._service: MiNAService | None = None
        self._miot_service: MiIOService | None = None
        self.on_login_expired = None  # backwards-compatible single callback
        self._expiry_listeners: list = []
        self._state_path = settings.config_path.parent / "xiaomi-account.json"

    def _account_state(self) -> dict:
        try:
            value = json.loads(self._state_path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_account_state(self, status: str, user_id: str | None = None) -> None:
        value = self._account_state()
        value.update({"status": status, "ever_logged_in": True})
        if user_id:
            value["user_id"] = user_id
        write_json(self._state_path, value)

    def note_cloud_result(self, ok: bool) -> None:
        """Record whether a real cloud call just worked.

        Stored tokens prove nothing: the account can look "connected" while
        every request times out or is rejected, and then the speaker list is
        simply empty — with no way offered to log in again. Field report
        (0.5.2): "登录丢了没有弹出二维码让我重新登陆".
        """
        now = time.time()
        was_degraded = self.cloud_degraded()
        if ok:
            self._cloud_failures = 0
            self._cloud_last_ok_at = now
            if was_degraded:
                # The recovery moment belongs in the log: it is what tells a
                # reader when the account became usable again.
                logger.info("小米云端已恢复")
            return
        self._cloud_failures += 1
        self._cloud_last_failure_at = now
        if not was_degraded and self.cloud_degraded():
            # One line per outage, not one per failed call.
            logger.warning(
                "小米云端连续 %d 次无响应：音箱列表、播放控制与扫码登录都会失败。"
                "请检查这台设备的外网访问（DNS、防火墙、代理都会影响）；"
                "恢复后会自动重新连接，不需要重新登录。",
                self._cloud_failures,
            )

    def cloud_health(self) -> dict:
        """Consecutive cloud failures and when the last call worked."""
        return {
            "failures": self._cloud_failures,
            "last_ok_at": int(self._cloud_last_ok_at or 0),
            "last_failure_at": int(self._cloud_last_failure_at or 0),
        }

    def cloud_degraded(self) -> bool:
        """True when the account is unusable right now (cloud unreachable).

        Several consecutive failures, the latest still recent: one timeout is a
        hiccup, and a counter left over from an hour ago is no evidence about
        the cloud's state now. This signal decides whether the UI offers a
        fresh login.
        """
        if self._cloud_failures < CLOUD_DEGRADED_FAILURES:
            return False
        return (time.time() - self._cloud_last_failure_at) <= CLOUD_FAILURE_FRESH_SECONDS

    def connection_state(self) -> dict:
        logged_in, user_id = self.stored_identity()
        saved = self._account_state()
        saved_status = str(saved.get("status") or "never_connected")
        has_provider_history = bool(saved.get("ever_logged_in") or settings.speakers)
        # A previously connected account with unreadable/missing encrypted
        # tokens is an expired session, not a brand-new installation. This
        # lets the UI offer recovery after a machine-key or token-file change.
        if logged_in:
            # Tokens present, but say so honestly when the cloud is not
            # answering: "connected + no devices" is the state that left the
            # user with no way back in.
            status = "unstable" if self.cloud_degraded() else "connected"
        elif has_provider_history and saved_status != "disconnected":
            status = "expired"
        else:
            status = saved_status
        return {
            "logged_in": logged_in,
            "user_id": user_id or saved.get("user_id"),
            "status": status,
            "ever_logged_in": bool(saved.get("ever_logged_in")),
            "cloud": self.cloud_health(),
        }

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            # Through the shared factory: bounded requests *and* bounded name
            # resolution (see micast.net — an unbounded resolver took the whole
            # account offline on a real device).
            self._session = new_session()
        return self._session

    async def _build_account(self, tokens: dict) -> MiAccount:
        """Create a MiAccount pre-loaded with tokens for micoapi."""
        session = await self._get_session()

        class _MemStore(MiTokenStore):
            def __init__(self, data):
                self._data = data
                super().__init__("")

            def load_token(self):
                return self._data

            def save_token(self, token=None):
                pass

        account = MiAccount(session, tokens["userId"], "", _MemStore(tokens))
        account.token = tokens
        account.now_ua = UA
        return account

    async def ensure_service(self) -> MiNAService | None:
        """Return MiNAService if tokens are available and valid."""
        if self._service:
            return self._service

        tokens = self._token_store.load()
        if not tokens:
            return None

        account = await self._build_account(tokens)
        self._account = account
        self._service = MiNAService(account)
        return self._service

    async def ensure_miot_service(self) -> MiIOService | None:
        """Return an isolated MIoT service only when xiaomiio tokens exist.

        Passing a micoapi-only account to MiIOService makes miservice attempt a
        password login without a password/passToken. That failed login can
        mutate the otherwise healthy MiNA account, so the two services must not
        share a mutable MiAccount instance.
        """
        if self._miot_service:
            return self._miot_service
        tokens = self._token_store.load()
        pair = tokens.get(QR_SID) if tokens else None
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            return None
        self._miot_account = await self._build_account(tokens)
        self._miot_service = MiIOService(self._miot_account)
        return self._miot_service

    def stored_identity(self) -> tuple[bool, str | None]:
        """Return the persisted login identity without exposing token details."""
        tokens = self._token_store.load()
        user_id = str(tokens["userId"]) if tokens and tokens.get("userId") else None
        return user_id is not None, user_id

    def subscribe_expiry(self, callback) -> None:
        """Subscribe to the single account-expired event.

        Consumers receive the event without owning token cleanup. This keeps
        UI notifications, diagnostics and integrations from each reimplementing
        expiry handling.
        """
        if callback not in self._expiry_listeners:
            self._expiry_listeners.append(callback)

    def unsubscribe_expiry(self, callback) -> None:
        if callback in self._expiry_listeners:
            self._expiry_listeners.remove(callback)

    async def verify_credentials(self) -> str:
        """Classify the stored login after a cloud API failure.

        Returns "rejected" (passToken definitively dead — safe to invalidate),
        "healed" (passToken fine; a fresh serviceToken was exchanged, stored and
        the cached services reset — caller should retry its failed request), or
        "unknown" (verification itself failed, e.g. network — keep the login).
        """
        tokens = self._token_store.load()
        if not tokens or not tokens.get("userId") or not tokens.get("passToken"):
            return "unknown"
        user_id = str(tokens["userId"])
        device_id = (
            tokens.get("deviceId") or hashlib.md5(f"micast-{user_id}".encode()).hexdigest()[:16]
        )
        try:
            pair = await self._exchange_for_sid(user_id, tokens["passToken"], device_id, SID)
        except XiaomiAuthError as exc:
            logger.warning("passToken definitively rejected: %s", exc)
            return "rejected"
        except Exception as exc:
            # Say *why*: "inconclusive" alone made a DNS failure, a TLS failure
            # and a timeout look identical in the log.
            logger.warning(
                "passToken verification inconclusive (network error): %s: %s",
                type(exc).__name__,
                exc,
            )
            return "unknown"
        # passToken alive but the old serviceToken failed: store the fresh pair
        # and drop cached services so the next request uses it.
        tokens[SID] = pair
        tokens["deviceId"] = device_id
        tokens["refreshedAt"] = int(time.time())
        self._token_store.save(tokens)
        self._account = None
        self._service = None
        logger.info("serviceToken healed after API failure")
        return "healed"

    def invalidate_login(self) -> None:
        """Drop credentials and cached services after Xiaomi rejects the session."""
        _, user_id = self.stored_identity()
        self._save_account_state("expired", user_id)
        self._token_store.clear()
        self._account = None
        self._miot_account = None
        self._service = None
        self._miot_service = None
        callbacks = list(self._expiry_listeners)
        if self.on_login_expired and self.on_login_expired not in callbacks:
            callbacks.append(self.on_login_expired)
        for callback in callbacks:
            try:
                callback()
            except Exception:
                logger.exception("login-expired callback failed")

    async def recover_after_failure(self) -> str:
        """Classify a failed Xiaomi request and invalidate only when certain."""
        now = time.monotonic()
        if now - self._last_recovery_attempt_at < CLOUD_VERIFY_MIN_INTERVAL_SECONDS:
            # Throttled: keep the login and let the caller see the original
            # failure. Verifying every failure turned one unreachable cloud into
            # a storm of account exchanges.
            return "unknown"
        self._last_recovery_attempt_at = now
        verdict = await self.verify_credentials()
        logger.info("Xiaomi credential verification result: %s", verdict)
        if verdict == "rejected":
            self.invalidate_login()
        return verdict

    def logout(self) -> None:
        """Intentional disconnect; unlike expiry this must not prompt recovery."""
        _, user_id = self.stored_identity()
        self._save_account_state("disconnected", user_id)
        self._token_store.clear()
        self._account = None
        self._miot_account = None
        self._service = None
        self._miot_service = None

    async def refresh_service_tokens(self) -> bool:
        """Silently re-exchange stored passToken for fresh serviceTokens.

        serviceToken lives ~30 days; passToken much longer. Called periodically
        by the renewal loop so the login never ages out. A serviceLogin rejection
        means the passToken itself is dead -> invalidate_login. Network/other
        errors keep the old tokens and simply retry on the next cycle.
        """
        tokens = self._token_store.load()
        if not tokens or not tokens.get("userId") or not tokens.get("passToken"):
            return False
        user_id = str(tokens["userId"])
        device_id = (
            tokens.get("deviceId") or hashlib.md5(f"micast-{user_id}".encode()).hexdigest()[:16]
        )

        try:
            tokens[SID] = await self._exchange_for_sid(user_id, tokens["passToken"], device_id, SID)
        except XiaomiAuthError:
            logger.warning("passToken rejected during renewal; session is dead")
            self.invalidate_login()
            return False

        # xiaomiio (MIoT) pair is best-effort: keep the old one on failure.
        if isinstance(tokens.get(QR_SID), (list, tuple)):
            try:
                tokens[QR_SID] = await self._exchange_for_sid(
                    user_id, tokens["passToken"], device_id, QR_SID
                )
            except Exception:
                logger.exception("MIoT token renewal failed; keeping previous pair")

        tokens["deviceId"] = device_id
        tokens["refreshedAt"] = int(time.time())
        self._token_store.save(tokens)
        # Rebuild cached services so live sessions use the new serviceToken.
        self._account = None
        self._miot_account = None
        self._service = None
        self._miot_service = None
        self._save_account_state("connected", user_id)
        logger.info("Xiaomi serviceTokens renewed silently")
        return True

    def renewal_due(self, max_age_seconds: int) -> bool:
        """True when stored tokens are missing refreshedAt or older than max_age."""
        tokens = self._token_store.load()
        if not tokens or not tokens.get("passToken"):
            return False
        refreshed_at = tokens.get("refreshedAt")
        if not isinstance(refreshed_at, (int, float)):
            return True
        return (time.time() - refreshed_at) >= max_age_seconds

    async def verify_if_stale(self, max_age_seconds: float) -> str:
        """Verify the stored login only when it has not been checked recently.

        The UI polls the status endpoint every 30s, and that poll used to
        exchange the passToken for a fresh serviceToken every single time: two
        cloud round trips plus an encrypted token rewrite per poll, and a log
        line reading "serviceToken healed after API failure" every 30 seconds
        although nothing had failed — the loudest thing in a field report and
        pure noise. A real API failure still heals immediately through
        recover_after_failure(); token freshness has its own renewal loop.
        """
        if not self.renewal_due(int(max_age_seconds)):
            return "cached"
        return await self.verify_credentials()

    async def run_token_renewal(
        self, interval_seconds: int = 12 * 3600, max_age_seconds: int = 7 * 24 * 3600
    ) -> None:
        """Background loop: renew serviceToken when it grows older than max_age."""
        while True:
            try:
                if self.renewal_due(max_age_seconds):
                    await self.refresh_service_tokens()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Network errors etc: keep old tokens, retry next cycle.
                logger.exception("Token renewal cycle failed")
            await asyncio.sleep(interval_seconds)

    async def start_qr_login(self) -> dict:
        """Start QR login. Returns qr_url and scan_token (lp_url)."""
        session = await self._get_session()
        device_id = hashlib.md5(f"micast-qr-{int(time.time())}".encode()).hexdigest()[:16]

        params = urlencode(
            {
                "_qrsize": "480",
                "qs": f"%3Fsid%3D{QR_SID}%26_json%3Dtrue",
                "callback": "https://sts.api.io.mi.com/sts",
                "_hasLogo": "false",
                "sid": QR_SID,
                "serviceParam": "",
                "_locale": "zh_CN",
                "_dc": str(int(time.time() * 1000)),
            },
            quote_via=quote,
        )

        try:
            async with session.get(
                f"https://{ACCOUNT_HOST}/longPolling/loginUrl?{params}",
                headers={"User-Agent": UA},
                cookies={"sdkVersion": "accountsdk-18.8.15", "deviceId": device_id},
                timeout=aiohttp.ClientTimeout(total=QR_START_TIMEOUT_SECONDS),
            ) as resp:
                text = (await resp.read()).decode("utf-8")
        except (TimeoutError, aiohttp.ClientError) as exc:
            # The reason belongs in the log: the page can only show a sentence.
            logger.warning(
                "QR login could not reach %s: %s: %s", ACCOUNT_HOST, type(exc).__name__, exc
            )
            raise XiaomiAuthError(
                f"连不上小米账号服务器（{ACCOUNT_HOST}）。"
                "请确认这台设备能访问外网：DNS、防火墙或代理都会影响。"
            ) from exc
        try:
            result = _parse_json(text)
        except ValueError as exc:
            logger.warning("QR login got an unreadable reply from %s: %s", ACCOUNT_HOST, text[:200])
            raise XiaomiAuthError(
                "小米账号服务器的回应无法识别（可能被网络设备拦截，或需要代理）。"
            ) from exc

        qr_url = result.get("qr")
        lp_url = result.get("lp")
        login_url = result.get("loginUrl")
        if not lp_url or not (login_url or qr_url):
            raise XiaomiAuthError(f"Failed to get QR: {result}")

        return {
            "qr_url": qr_url,
            "login_url": login_url,
            "scan_token": lp_url,
            "device_id": device_id,
        }

    async def poll_qr_login(self, lp_url: str) -> dict:
        """Poll QR login status. Returns status and tokens on confirmed."""
        session = await self._get_session()
        try:
            async with session.get(
                lp_url,
                headers={"User-Agent": UA},
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status != 200:
                    return {"status": "waiting"}
                text = (await resp.read()).decode("utf-8")
                result = _parse_json(text)

                if result.get("passToken"):
                    user_id = str(result["userId"])
                    pass_token = result["passToken"]
                    extra_tokens = {}
                    if all(result.get(key) for key in ("ssecurity", "location", "nonce")):
                        service_token = await self._exchange_service_token(result)
                        extra_tokens[QR_SID] = (result["ssecurity"], service_token)
                    await self._login_with_pass_token(user_id, pass_token, extra_tokens)
                    return {"status": "confirmed"}

                if result.get("code") == 70016:
                    return {"status": "expired"}

                new_lp = result.get("lp")
                if new_lp and new_lp != lp_url:
                    return {"status": "waiting", "lp_url": new_lp}
                return {"status": "waiting"}
        except TimeoutError:
            return {"status": "waiting"}
        except Exception as e:
            logger.exception("QR poll error: %s", e)
            return {"status": "waiting"}

    async def login_with_cookie(self, user_id: str, pass_token: str) -> bool:
        """Login using existing userId and passToken."""
        await self._login_with_pass_token(user_id, pass_token)
        return True

    async def _exchange_for_sid(
        self, user_id: str, pass_token: str, device_id: str, sid: str
    ) -> tuple[str, str]:
        """Exchange userId+passToken for (ssecurity, serviceToken) of one service."""
        session = await self._get_session()
        cookies = {
            "sdkVersion": "accountsdk-18.8.15",
            "deviceId": device_id,
            "userId": user_id,
            "passToken": pass_token,
        }
        async with session.get(
            f"https://account.xiaomi.com/pass/serviceLogin?sid={sid}&_json=true",
            headers={"User-Agent": UA},
            cookies=cookies,
        ) as resp:
            text = (await resp.read()).decode("utf-8")
            result = _parse_json(text)

        if result.get("code") != 0 or not result.get("ssecurity"):
            raise XiaomiAuthError(f"Login failed for {sid}: {result}")
        service_token = await self._exchange_service_token(result)
        return result["ssecurity"], service_token

    async def _login_with_pass_token(
        self,
        user_id: str,
        pass_token: str,
        extra_tokens: dict | None = None,
    ) -> None:
        """Exchange userId+passToken for micoapi serviceToken and persist."""
        device_id = hashlib.md5(f"micast-{user_id}".encode()).hexdigest()[:16]
        pair = await self._exchange_for_sid(user_id, pass_token, device_id, SID)
        tokens = {
            "userId": user_id,
            "passToken": pass_token,
            "deviceId": device_id,
            "refreshedAt": int(time.time()),
            SID: pair,
        }
        tokens.update(extra_tokens or {})
        self._token_store.save(tokens)
        self._save_account_state("connected", user_id)
        self._account = await self._build_account(tokens)
        self._service = MiNAService(self._account)
        self._miot_account = None
        self._miot_service = None

    async def _exchange_service_token(self, login_result: dict) -> str:
        """Exchange login result for serviceToken."""
        session = await self._get_session()
        location = login_result["location"]
        nonce = login_result["nonce"]
        ssecurity = login_result["ssecurity"]

        nsec = f"nonce={nonce}&{ssecurity}"
        client_sign = base64.b64encode(hashlib.sha1(nsec.encode()).digest()).decode()
        url = f"{location}&clientSign={quote(client_sign)}"

        async with session.get(url, headers={"User-Agent": UA}) as resp:
            if "serviceToken" not in resp.cookies:
                raise XiaomiAuthError("serviceToken not found in response")
            return resp.cookies["serviceToken"].value

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
