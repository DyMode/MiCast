"""Classic RAOP session cryptography and ALAC stream configuration."""

import base64
import socket
import struct
from functools import lru_cache

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

AIRPORT_KEY = b"""-----BEGIN RSA PRIVATE KEY-----
MIIEpQIBAAKCAQEA59dE8qLieItsH1WgjrcFRKj6eUWqi+bGLOX1HL3U3GhC/j0Q
g90u3sG/1CUtwC5vOYvfDmFI6oSFXi5ELabWJmT2dKHzBJKa3k9ok+8t9ucRqMd6
DZHJ2YCCLlDRKSKv6kDqnw4UwPdpOMXziC/AMj3Z/lUVX1G7WSHCAWKf1zNS1eLv
qr+boEjXuBOitnZ/bDzPHrTOZz0Dew0uowxf/+sG+NCK3eQJVxqcaJ/vEHKIVd2M
+5qL71yJQ+87X6oV3eaYvt3zWZYD6z5vYTcrtij2VZ9Zmni/UAaHqn9JdsBWLUEp
VviYnhimNVvYFZeCXg/IdTQ+x4IRdiXNv5hEewIDAQABAoIBAQDl8Axy9XfWBLmk
zkEiqoSwF0PsmVrPzH9KsnwLGH+QZlvjWd8SWYGN7u1507HvhF5N3drJoVU3O14n
DY4TFQAaLlJ9VM35AApXaLyY1ERrN7u9ALKd2LUwYhM7Km539O4yUFYikE2nIPsc
EsA5ltpxOgUGCY7b7ez5NtD6nL1ZKauw7aNXmVAvmJTcuPxWmoktF3gDJKK2wxZu
NGcJE0uFQEG4Z3BrWP7yoNuSK3dii2jmlpPHr0O/KnPQtzI3eguhe0TwUem/eYSd
yzMyVx/YpwkzwtYL3sR5k0o9rKQLtvLzfAqdBxBurcizaaA/L0HIgAmOit1GJA2s
aMxTVPNhAoGBAPfgv1oeZxgxmotiCcMXFEQEWflzhWYTsXrhUIuz5jFua39GLS99
ZEErhLdrwj8rDDViRVJ5skOp9zFvlYAHs0xh92ji1E7V/ysnKBfsMrPkk5KSKPrn
jndMoPdevWnVkgJ5jxFuNgxkOLMuG9i53B4yMvDTCRiIPMQ++N2iLDaRAoGBAO9v
//mU8eVkQaoANf0ZoMjW8CN4xwWA2cSEIHkd9AfFkftuv8oyLDCG3ZAf0vrhrrtk
rfa7ef+AUb69DNggq4mHQAYBp7L+k5DKzJrKuO0r+R0YbY9pZD1+/g9dVt91d6LQ
NepUE/yY2PP5CNoFmjedpLHMOPFdVgqDzDFxU8hLAoGBANDrr7xAJbqBjHVwIzQ4
To9pb4BNeqDndk5Qe7fT3+/H1njGaC0/rXE0Qb7q5ySgnsCb3DvAcJyRM9SJ7OKl
Gt0FMSdJD5KG0XPIpAVNwgpXXH5MDJg09KHeh0kXo+QA6viFBi21y340NonnEfdf
54PX4ZGS/Xac1UK+pLkBB+zRAoGAf0AY3H3qKS2lMEI4bzEFoHeK3G895pDaK3TF
BVmD7fV0Zhov17fegFPMwOII8MisYm9ZfT2Z0s5Ro3s5rkt+nvLAdfC/PYPKzTLa
lpGSwomSNYJcB9HNMlmhkGzc1JnLYT4iyUyx6pcZBmCd8bD0iwY/FzcgNDaUmbX9
+XDvRA0CgYEAkE7pIPlE71qvfJQgoA9em0gILAuE4Pu13aKiJnfft7hIjbK+5kyb
3TysZvoyDnb3HOKvInK7vXbKuU4ISgxB2bB3HcYzQMGsz1qJ2gG0N5hvJpzwwhbh
XqFKA4zaaSrw622wDniAK5MlIE0tIAKKP4yxNGjoD2QYjhBGuhvkWKY=
-----END RSA PRIVATE KEY-----"""


def decode_b64(value: str) -> bytes:
    return base64.b64decode(value + "=" * (-len(value) % 4))


@lru_cache(maxsize=1)
def _airport_key():
    return serialization.load_pem_private_key(AIRPORT_KEY, password=None)


def aes_cbc(data: bytes, key: bytes, iv: bytes, *, encrypt: bool = False) -> bytes:
    """Classic RAOP encrypts full blocks only; callers preserve the tail."""
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    operation = cipher.encryptor() if encrypt else cipher.decryptor()
    return operation.update(data) + operation.finalize()


def apple_response(challenge: str, local_ip: str, mac: bytes) -> str:
    material = (decode_b64(challenge) + socket.inet_aton(local_ip) + mac).ljust(32, b"\0")
    key = _airport_key()
    numbers = key.private_numbers()
    size = key.key_size // 8
    encoded = b"\0\1" + b"\xff" * (size - len(material) - 3) + b"\0" + material
    signed = pow(int.from_bytes(encoded, "big"), numbers.d, numbers.public_numbers.n).to_bytes(
        size, "big"
    )
    return base64.b64encode(signed).decode().rstrip("=")


def decrypt_session_key(value: str) -> bytes:
    return _airport_key().decrypt(
        decode_b64(value),
        padding.OAEP(mgf=padding.MGF1(hashes.SHA1()), algorithm=hashes.SHA1(), label=None),
    )


def encrypt_session_key(key: bytes) -> str:
    """Sender side of rsaaeskey: RSA-OAEP encrypt the session AES key with the
    AirPort public key (the inverse of :func:`decrypt_session_key`)."""
    encrypted = (
        _airport_key()
        .public_key()
        .encrypt(
            key, padding.OAEP(mgf=padding.MGF1(hashes.SHA1()), algorithm=hashes.SHA1(), label=None)
        )
    )
    return base64.b64encode(encrypted).decode().rstrip("=")


# fmtp announced by MiCast when SENDING audio to another AirPlay device.
# Identical to the classic iTunes layout except the frame length: PyAV's ALAC
# encoder cannot be configured below its 4096-sample frame size, so packets
# carry 4096 frames and receivers must honor the announced value (open
# implementations like shairport do; Apple 1st-party devices are out of scope).
SENDER_FMTP = [4096, 0, 16, 40, 10, 14, 2, 255, 0, 0, 44100]


def alac_cookie(fmtp: list[int]) -> bytes:
    if len(fmtp) >= 12:
        fmtp = fmtp[1:12]
    if len(fmtp) != 11:
        fmtp = [352, 0, 16, 40, 10, 14, 2, 255, 0, 0, 44100]
    config = struct.pack(">IBBBBBBHIII", *fmtp)
    # FFmpeg/PyAV expects the 24-byte ALACSpecificConfig wrapped in the
    # standard 36-byte `alac` atom used by CAF/MP4 streams.
    return struct.pack(">I4sI", 36, b"alac", 0) + config
