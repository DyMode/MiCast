import base64
import socket

import av
from cryptography.hazmat.primitives import serialization

from micast.raop.crypto import AIRPORT_KEY, alac_cookie, apple_response


def test_alac_cookie_has_expected_size():
    cookie = alac_cookie([96, 352, 0, 16, 40, 10, 14, 2, 255, 0, 0, 44100])
    assert len(cookie) == 36
    assert cookie[4:8] == b"alac"
    decoder = av.CodecContext.create("alac", "r")
    decoder.extradata = cookie
    decoder.open()


def test_apple_challenge_response_is_raw_rsa_signature():
    challenge = base64.b64encode(bytes(range(16))).decode()
    response = base64.b64decode(apple_response(challenge, "192.168.0.13", b"\x02MICAS") + "==")
    key = serialization.load_pem_private_key(AIRPORT_KEY, password=None)
    public = key.public_key().public_numbers()
    decoded = pow(int.from_bytes(response, "big"), public.e, public.n).to_bytes(
        key.key_size // 8, "big"
    )
    material = bytes(range(16)) + socket.inet_aton("192.168.0.13") + b"\x02MICAS"
    assert decoded.endswith(material.ljust(32, b"\0"))
