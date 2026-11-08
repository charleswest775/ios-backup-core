"""
Decrypting encrypted backups made by iOS 9 and earlier (Manifest.mbdb backups).

iphone-backup-decrypt reads the encryption of iOS 10.2 and later. Older
backups differ in two ways:

- The keybag in Manifest.plist wraps its class keys with a key derived by
  PBKDF2-SHA1 over SALT/ITER alone. iOS 10.2 added a PBKDF2-SHA256 round
  (DPSL/DPIC) before it.
- Manifest.mbdb itself isn't encrypted. Each record carries its file's key:
  the protection class (little-endian int32), then the key wrapped (RFC 3394)
  with that class's key.

File contents are AES-256-CBC with a zero IV and PKCS#7 padding, as later.

LegacyEncryptedBackup offers the two methods of
``iphone_backup_decrypt.EncryptedBackup`` this library and OpenExtract use,
``extract_file`` and ``manifest_db_cursor``, so it slots in as a
LocalBackupAccessor's ``decrypted_backup``.

iOS 10.0 and 10.1 sit in between: Manifest.db, encrypted as later, but the
older keybag. For those, passphrase_key_without_dpsl derives the key and
iphone-backup-decrypt does the rest.
"""

from __future__ import annotations

import hashlib
import os
import plistlib
import shutil
import sqlite3
import struct
import tempfile
from contextlib import contextmanager
from typing import Optional

from ios_backup_core.mbdb import build_files_db

# pycryptodome comes with iphone-backup-decrypt. Like that import, it raises
# OSError, not ImportError, when Windows refuses to load its native library.
try:
    from Crypto.Cipher import AES  # type: ignore[import]
    HAS_LEGACY_CRYPTO = True
except (ImportError, OSError):
    HAS_LEGACY_CRYPTO = False

_WRAP_PASSCODE = 2
_CLASS_KEY_TAGS = {b"CLAS", b"WRAP", b"WPKY", b"KTYP", b"PBKY"}
# iOS used 10,000 rounds; refuse absurd counts from a damaged or hostile plist.
_MAX_ITERATIONS = 10_000_000
_CHUNK = 1024 * 1024  # a multiple of the AES block size


class IncorrectPassphraseError(ValueError):
    """The password doesn't unlock the backup's keybag."""


def aes_unwrap(kek: bytes, wrapped: bytes) -> Optional[bytes]:
    """RFC 3394 AES key unwrap. Returns None when the integrity check fails (wrong key)."""
    if len(wrapped) % 8 or len(wrapped) < 24:
        return None
    n = len(wrapped) // 8 - 1
    a = struct.unpack(">Q", wrapped[:8])[0]
    r = [wrapped[8 * (i + 1):8 * (i + 2)] for i in range(n)]
    cipher = AES.new(kek, AES.MODE_ECB)
    for j in range(5, -1, -1):
        for i in range(n, 0, -1):
            b = cipher.decrypt(struct.pack(">Q", a ^ (n * j + i)) + r[i - 1])
            a = struct.unpack(">Q", b[:8])[0]
            r[i - 1] = b[8:]
    if a != 0xA6A6A6A6A6A6A6A6:
        return None
    return b"".join(r)


def _tlv_blocks(blob: bytes):
    pos = 0
    while pos + 8 <= len(blob):
        tag = blob[pos:pos + 4]
        (length,) = struct.unpack(">L", blob[pos + 4:pos + 8])
        yield tag, blob[pos + 8:pos + 8 + length]
        pos += 8 + length


class LegacyKeybag:
    """A backup keybag: its attributes and its class keys, unlocked with the password."""

    def __init__(self, blob: bytes):
        self.attrs: dict = {}
        self.class_keys: dict = {}
        uuid = wrap = None
        current: Optional[dict] = None
        for tag, value in _tlv_blocks(blob):
            number = struct.unpack(">L", value)[0] if len(value) == 4 else None
            if tag == b"UUID" and uuid is None:
                uuid = value
            elif tag == b"WRAP" and wrap is None:
                wrap = number
            elif tag == b"UUID":
                if current and b"CLAS" in current:
                    self.class_keys[current[b"CLAS"]] = current
                current = {b"UUID": value}
            elif tag in _CLASS_KEY_TAGS and current is not None:
                current[tag] = number if number is not None and tag != b"WPKY" else value
            else:
                self.attrs[tag] = number if number is not None else value
        if current and b"CLAS" in current:
            self.class_keys[current[b"CLAS"]] = current
        self._keys: dict = {}

    def passphrase_key(self, passphrase: str) -> bytes:
        """The key that wraps the class keys. Raises ValueError if the keybag is damaged."""
        salt, iterations = self.attrs.get(b"SALT"), self.attrs.get(b"ITER")
        if not isinstance(salt, bytes) or not isinstance(iterations, int) or not 0 < iterations <= _MAX_ITERATIONS:
            raise ValueError("the backup's keybag is damaged")
        secret = passphrase.encode("utf-8")
        dpsl, dpic = self.attrs.get(b"DPSL"), self.attrs.get(b"DPIC")
        if isinstance(dpsl, bytes) and isinstance(dpic, int) and 0 < dpic <= 20_000_000:
            secret = hashlib.pbkdf2_hmac("sha256", secret, dpsl, dpic, 32)
        return hashlib.pbkdf2_hmac("sha1", secret, salt, iterations, 32)

    def unlock(self, passphrase: str) -> None:
        """Unwrap the class keys. Raises IncorrectPassphraseError if *passphrase* is wrong."""
        passcode_key = self.passphrase_key(passphrase)
        keys = {}
        for clas, entry in self.class_keys.items():
            wrapped = entry.get(b"WPKY")
            if not isinstance(wrapped, bytes) or not (entry.get(b"WRAP", 0) & _WRAP_PASSCODE):
                continue
            key = aes_unwrap(passcode_key, wrapped)
            if key is None:
                # Same words as iphone-backup-decrypt, so both kinds of backup read alike.
                raise IncorrectPassphraseError("Failed to decrypt keys: incorrect passphrase?")
            keys[clas] = key
        if not keys:
            raise ValueError("the backup's keybag has no keys for a password")
        self._keys = keys

    def unwrap_file_key(self, encryption_key: bytes) -> Optional[bytes]:
        """A file's own key, from the 44-byte value in its Manifest.mbdb record."""
        if len(encryption_key) < 4 + 24:
            return None
        (clas,) = struct.unpack("<l", encryption_key[:4])
        class_key = self._keys.get(clas)
        return aes_unwrap(class_key, encryption_key[4:]) if class_key else None


def passphrase_key_without_dpsl(backup_directory: str, passphrase: str) -> Optional[bytes]:
    """For a backup made by iOS 10.0 or 10.1, the key for iphone-backup-decrypt's ``passphrase_key``.

    Those backups list their files in an encrypted Manifest.db, as later ones
    do, but their keybag has no DPSL/DPIC yet. iphone-backup-decrypt always
    runs that round and fails with KeyError: b'DPSL' (b'DPIC' from 0.11), so
    the key is derived here from SALT/ITER alone and handed over in place of
    the password.

    Returns None when the keybag has DPSL (iOS 10.2 and later) or Manifest.plist
    can't be read, so iphone-backup-decrypt takes the password as usual. Raises
    ValueError if a keybag without DPSL is damaged.
    """
    try:
        with open(os.path.join(backup_directory, "Manifest.plist"), "rb") as f:
            blob = plistlib.load(f).get("BackupKeyBag")
    except Exception:
        return None
    if not isinstance(blob, bytes):
        return None
    keybag = LegacyKeybag(blob)
    if b"DPSL" in keybag.attrs:
        return None
    return keybag.passphrase_key(passphrase)


class LegacyEncryptedBackup:
    """An encrypted backup made by iOS 9 or earlier, opened with its password.

    Raises IncorrectPassphraseError (a ValueError) for a wrong password,
    ValueError for a damaged keybag, ios_backup_core.mbdb.MbdbError for a
    damaged Manifest.mbdb, and RuntimeError when pycryptodome can't be loaded.
    """

    def __init__(self, *, backup_directory: str, passphrase: str):
        if not HAS_LEGACY_CRYPTO:
            raise RuntimeError("pycryptodome, which decrypts backups, couldn't be loaded")
        self._backup_directory = backup_directory
        self._conn: Optional[sqlite3.Connection] = None
        self._temporary_folder: Optional[str] = None
        with open(os.path.join(backup_directory, "Manifest.plist"), "rb") as f:
            manifest = plistlib.load(f)
        blob = manifest.get("BackupKeyBag")
        if not isinstance(blob, bytes):
            raise ValueError("the backup has no keybag")
        self.keybag = LegacyKeybag(blob)
        self.keybag.unlock(passphrase)
        self._temporary_folder = tempfile.mkdtemp(prefix="iosbackup_mbdb_")
        db_path = os.path.join(self._temporary_folder, "Manifest.db")
        build_files_db(os.path.join(backup_directory, "Manifest.mbdb"), db_path)
        self._conn = sqlite3.connect(db_path)

    def __del__(self):
        self._cleanup()

    def _cleanup(self) -> None:
        conn, self._conn = getattr(self, "_conn", None), None
        if conn is not None:
            conn.close()
        folder, self._temporary_folder = getattr(self, "_temporary_folder", None), None
        if folder:
            shutil.rmtree(folder, ignore_errors=True)

    @contextmanager
    def manifest_db_cursor(self):
        """A cursor on the backup's Files table (Manifest.db's shape, built from Manifest.mbdb)."""
        cur = self._conn.cursor()
        try:
            yield cur
        finally:
            cur.close()

    def extract_file(self, *, relative_path: str, domain_like: Optional[str] = None,
                     output_filename: str) -> None:
        """Decrypt one file into *output_filename*. Raises FileNotFoundError if the backup hasn't got it."""
        row = self._conn.execute(
            "SELECT fileID, encryptionKey, size FROM Files "
            "WHERE relativePath = ? AND domain LIKE ? AND flags = 1 ORDER BY domain LIMIT 1",
            (relative_path, domain_like or "%"),
        ).fetchone()
        if not row:
            raise FileNotFoundError(relative_path)
        file_id, encryption_key, size = row
        source = None
        for candidate in (os.path.join(self._backup_directory, file_id),
                          os.path.join(self._backup_directory, file_id[:2], file_id)):
            if os.path.isfile(candidate):
                source = candidate
                break
        if source is None:
            raise FileNotFoundError(relative_path)
        if not encryption_key:
            # Some records (empty files) carry no key; their contents aren't encrypted.
            shutil.copyfile(source, output_filename)
            return
        key = self.keybag.unwrap_file_key(encryption_key)
        if key is None:
            raise ValueError(f"the key for {relative_path} couldn't be unwrapped")
        self._decrypt(source, output_filename, key, size)

    @staticmethod
    def _decrypt(source: str, output_filename: str, key: bytes, size: int) -> None:
        cipher = AES.new(key, AES.MODE_CBC, iv=b"\x00" * 16)
        total = last_byte = 0
        with open(source, "rb") as src, open(output_filename, "wb") as dst:
            while True:
                chunk = src.read(_CHUNK)
                if not chunk:
                    break
                if len(chunk) % 16:
                    raise ValueError("an encrypted file's size isn't a multiple of 16 bytes")
                plain = cipher.decrypt(chunk)
                dst.write(plain)
                total += len(plain)
                last_byte = plain[-1]
            # The record's size is the plaintext's; fall back to the padding when it's missing.
            if 0 < size <= total:
                dst.truncate(size)
            elif 0 < last_byte <= 16:
                dst.truncate(total - last_byte)
