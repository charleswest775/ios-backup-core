"""Backup encryption, the inverse of what iphone_backup_decrypt reads.

- The keybag is a TLV blob. Each protection-class key is wrapped (RFC 3394) with a
  key derived from the password: PBKDF2-SHA256 over DPSL/DPIC, then PBKDF2-SHA1
  over SALT/ITER.
- Each file has its own random key, wrapped with its protection class's key and
  stored in the file's Manifest.db record. ``Manifest.db`` uses ``ManifestKey``.
- Contents are AES-256-CBC with a zero IV and PKCS#7 padding.

Keys come from a seeded RNG so output is reproducible. That is right for test
data and wrong for anything else.
"""

from __future__ import annotations

import hashlib
import random
import struct
from typing import Optional

from Crypto.Cipher import AES

# (DPIC, ITER). "real" matches iOS 10.2+ backups; "fast" keeps tests quick.
ITERATIONS = {"real": (10_000_000, 10_000), "fast": (1_000, 10)}


def aes_wrap(kek: bytes, key: bytes) -> bytes:
    """RFC 3394 AES key wrap."""
    n = len(key) // 8
    a = 0xA6A6A6A6A6A6A6A6
    r = [key[i * 8:(i + 1) * 8] for i in range(n)]
    cipher = AES.new(kek, AES.MODE_ECB)
    for j in range(6):
        for i in range(n):
            b = cipher.encrypt(struct.pack(">Q", a) + r[i])
            a = struct.unpack(">Q", b[:8])[0] ^ (n * j + i + 1)
            r[i] = b[8:]
    return struct.pack(">Q", a) + b"".join(r)


def encrypt_cbc(key: bytes, data: bytes) -> bytes:
    pad = 16 - len(data) % 16
    return AES.new(key, AES.MODE_CBC, iv=b"\x00" * 16).encrypt(data + bytes([pad]) * pad)


def derive_password_key(password: str, dpsl: bytes, dpic: int, salt: bytes, iterations: int) -> bytes:
    round1 = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), dpsl, dpic, 32)
    return hashlib.pbkdf2_hmac("sha1", round1, salt, iterations, 32)


def _tlv(tag: bytes, value) -> bytes:
    if isinstance(value, int):
        value = struct.pack(">L", value)
    return tag + struct.pack(">L", len(value)) + value


class Keybag:
    """A backup keybag with the same tag layout and protection classes as the target iOS version."""

    def __init__(self, rng: random.Random, layout: dict, password: Optional[str], iterations: str = "fast"):
        self.dpic, self.iter = ITERATIONS[iterations]
        self.salt, self.dpsl = rng.randbytes(20), rng.randbytes(20)
        classes = [(c["CLAS"], c["WRAP"]) for c in layout["classes"]]
        self.class_keys = {clas: rng.randbytes(32) for clas, _ in classes}
        # Unencrypted backups still carry a keybag; its keys are wrapped with a
        # device key we don't have, so a random key models that.
        wrapping_key = (
            derive_password_key(password, self.dpsl, self.dpic, self.salt, self.iter) if password else rng.randbytes(32)
        )
        parts = [
            _tlv(b"VERS", layout.get("VERS", 5)),
            _tlv(b"TYPE", 1),
            _tlv(b"UUID", rng.randbytes(16)),
            _tlv(b"HMCK", rng.randbytes(40)),
            _tlv(b"WRAP", 0),
            _tlv(b"SALT", self.salt),
            _tlv(b"ITER", self.iter),
            _tlv(b"DPWT", 1),
            _tlv(b"DPIC", self.dpic),
            _tlv(b"DPSL", self.dpsl),
        ]
        for clas, wrap in classes:
            parts += [
                _tlv(b"UUID", rng.randbytes(16)),
                _tlv(b"CLAS", clas),
                _tlv(b"WRAP", wrap),
                _tlv(b"KTYP", 0),
                _tlv(b"WPKY", aes_wrap(wrapping_key, self.class_keys[clas])),
            ]
        self.blob = b"".join(parts)

    def wrap_key(self, protection_class: int, key: bytes) -> bytes:
        """The 44-byte form stored in Manifest records: class (little-endian int32) + wrapped key."""
        return struct.pack("<l", protection_class) + aes_wrap(self.class_keys[protection_class], key)
