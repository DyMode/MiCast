"""Xiaomi account login routes."""

import logging
from io import BytesIO

import qrcode
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from micast.xiaomi.auth import XiaomiAuth

logger = logging.getLogger(__name__)

# How stale the stored login must be before a status poll re-verifies it. The
# UI polls every 30s; re-exchanging the serviceToken that often is pure cloud
# churn (and looked like a failure in every field report).
STATUS_VERIFY_MAX_AGE_SECONDS = 1800.0

router = APIRouter(prefix="/api/xiaomi", tags=["xiaomi"])

# In-memory QR state (single active QR per process)
_qr_state: dict = {}


def install(auth: XiaomiAuth) -> APIRouter:
    @router.post("/login/qr/start")
    async def qr_start():
        try:
            result = await auth.start_qr_login()
            _qr_state["lp_url"] = result["scan_token"]
            _qr_state["device_id"] = result["device_id"]
            # Prefer the login URL itself: Xiaomi's outer QR image just encodes
            # it, and rendering locally avoids a second network round-trip (and
            # the image decode step) that used to leave the sheet blank whenever
            # fetching the outer QR stalled or failed.
            return {
                "qr_url": result["login_url"] or result["qr_url"],
                "scan_token": result["scan_token"],
            }
        except Exception as e:
            logger.exception("QR start failed")
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.get("/login/qr/image")
    async def qr_image(url: str = Query(...)):
        """Render a login QR for the given Xiaomi login URL, generated locally."""
        try:
            qr = qrcode.QRCode(version=None, box_size=10, border=2)
            qr.add_data(url)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white").convert("RGB")

            buf = BytesIO()
            img.save(buf, format="PNG")
            buf.seek(0)
            return StreamingResponse(buf, media_type="image/png")
        except Exception as e:
            logger.exception("QR image generation failed")
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.get("/login/qr/poll")
    async def qr_poll(scan_token: str = Query(...)):
        try:
            result = await auth.poll_qr_login(scan_token)
            if result.get("status") == "confirmed":
                _qr_state.clear()
            return {"status": result["status"]}
        except Exception as e:
            logger.exception("QR poll failed")
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.post("/login/cookie")
    async def cookie_login(payload: dict):
        user_id = payload.get("user_id")
        pass_token = payload.get("pass_token")
        if not user_id or not pass_token:
            raise HTTPException(status_code=400, detail="user_id and pass_token required")
        try:
            await auth.login_with_cookie(user_id, pass_token)
            return {"success": True}
        except Exception as e:
            logger.exception("Cookie login failed")
            raise HTTPException(status_code=401, detail=str(e)) from e

    @router.get("/status")
    async def xiaomi_status(verify: bool = False):
        """Return login state; optionally verify the stored passToken.

        The verification is gated on token age: a 30s poll must not re-exchange
        the serviceToken every time (see XiaomiAuth.verify_if_stale). Any real
        request that fails still heals the login immediately.
        """
        try:
            if verify and auth.stored_identity()[0] and not auth.cloud_degraded():
                # While the cloud is not answering, the state is already known
                # (`unstable`); asking again would just queue another lookup.
                await auth.verify_if_stale(STATUS_VERIFY_MAX_AGE_SECONDS)
            return auth.connection_state()
        except Exception as e:
            logger.exception("Failed to load token status")
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.post("/logout")
    async def xiaomi_logout():
        """Clear stored Xiaomi tokens."""
        try:
            auth.logout()
            return {"ok": True}
        except Exception as e:
            logger.exception("Logout failed")
            raise HTTPException(status_code=500, detail=str(e)) from e

    return router
