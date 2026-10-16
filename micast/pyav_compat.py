"""Version-tolerant av.open wrappers and the DLNA client fingerprint.

PyAV 18.x accepts the ``metadata_errors`` keyword (needed so GBK-tagged DLNA
media does not fail metadata decoding); PyAV 19 removed it. The deployed fnOS
vendor ships 19.x while development environments may run 18.x, so the keyword
is applied best-effort: retry once without it when the build rejects it.
"""

from __future__ import annotations

from urllib.parse import urlsplit

import av

# DLNA client fingerprint for controller-hosted media servers (WMP
# MDEServer and friends): without these headers they answer the actual GET
# with a 4XX (typically 406), even though a bare HEAD probe returns 200.
# The user agent must ALSO travel via ffmpeg's `user_agent` option — the
# http protocol writes that as the request's primary User-Agent header,
# while a User-Agent line inside the custom `headers` string is overridden
# by ffmpeg's default (Lavf), which WMP's media sharing rejects.
DLNA_USER_AGENT = "Windows Media Player/12.0.7601.17514"
DLNA_HEADERS = {
    "User-Agent": DLNA_USER_AGENT,
    "getContentFeatures.dlna.org": "1",
    "Pragma": "getIfoFileURI.dlna.org",
    "Accept": "*/*",
    "Accept-Language": "en-US,*",
    "Accept-Encoding": "identity",
    # DLNA media transfer declaration — without it WMP's media sharing
    # answers the GET with 406 Not Acceptable.
    "transferMode.dlna.org": "Streaming",
}


def dlna_headers_string(url: str = "") -> str:
    """ffmpeg http protocol's ``headers`` option value: the DLNA fingerprint
    plus the origin Referer (some controllers 403 a GET without it)."""
    lines = [f"{key}: {value}" for key, value in DLNA_HEADERS.items()]
    try:
        parts = urlsplit(url)
    except ValueError:
        parts = None
    if parts is not None and parts.scheme in ("http", "https") and parts.netloc:
        lines.append(f"Referer: {parts.scheme}://{parts.netloc}/")
    return "".join(line + "\r\n" for line in lines)


def open_input(url: str, *, options: dict | None = None, timeout=None):
    """av.open for demuxing input, tolerating builds without metadata_errors."""
    kwargs = {}
    if options is not None:
        kwargs["options"] = options
    if timeout is not None:
        kwargs["timeout"] = timeout
    try:
        return av.open(url, metadata_errors="ignore", **kwargs)
    except TypeError as exc:
        if "metadata_errors" not in str(exc):
            raise
        return av.open(url, **kwargs)
