"""GENA callbacks belong to the requesting control point, never arbitrary hosts."""

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit, urlunsplit


def peer_address(value):
    address = ipaddress.ip_address(value)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    if address.is_unspecified or address.is_multicast or address.is_link_local:
        raise ValueError("Invalid control point address")
    return address


async def pin_callback(callback, peer, *, allow_loopback=False):
    """Resolve once, require every answer to match the peer, and connect by IP."""
    address = peer_address(peer)
    if address.is_loopback and not allow_loopback:
        raise ValueError("Loopback callbacks are not exposed by the DLNA service")
    parsed = urlsplit(callback)
    host = parsed.hostname
    if not host or parsed.scheme not in {"http", "https"}:
        raise ValueError("Invalid callback")
    try:
        answers = {peer_address(host)}
    except ValueError:
        infos = await asyncio.wait_for(
            asyncio.to_thread(socket.getaddrinfo, host, parsed.port, type=socket.SOCK_STREAM),
            2,
        )
        answers = {peer_address(info[4][0]) for info in infos}
    if answers != {address}:
        raise ValueError("Callback must belong to the requesting control point")
    literal = f"[{address}]" if address.version == 6 else str(address)
    authority = literal + (f":{parsed.port}" if parsed.port is not None else "")
    pinned = urlunsplit((parsed.scheme, authority, parsed.path, parsed.query, ""))
    return pinned, parsed.netloc, host
