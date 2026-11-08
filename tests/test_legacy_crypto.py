"""Tests for ios_backup_core.legacy_crypto: the encryption of backups made by iOS 9 and earlier."""

import hashlib
import os
import plistlib
import random
import struct
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, ROOT)

from backupgen.crypto import Keybag, aes_wrap, encrypt_cbc  # noqa: E402
from backupgen.legacy import _LegacyKeybag  # noqa: E402
from ios_backup_core.legacy_crypto import (  # noqa: E402
    IncorrectPassphraseError,
    LegacyEncryptedBackup,
    LegacyKeybag,
    aes_unwrap,
    passphrase_key_without_dpsl,
)

KEK = bytes.fromhex("000102030405060708090A0B0C0D0E0F101112131415161718191A1B1C1D1E1F")
KEY = bytes.fromhex("00112233445566778899AABBCCDDEEFF000102030405060708090A0B0C0D0E0F")
WRAPPED = bytes.fromhex("28C9F404C4B810F4CBCCB35CFB87F8263F5786E2D80ED326CBC7F0E71A99F43BFB988B9B7A02DD21")


def test_aes_unwrap_matches_rfc3394_vector():
    assert aes_unwrap(KEK, WRAPPED) == KEY


def test_aes_unwrap_with_the_wrong_key_is_none():
    assert aes_unwrap(bytes(32), WRAPPED) is None
    assert aes_unwrap(KEK, WRAPPED[:-3]) is None


def test_keybag_unlocks_with_the_password_only():
    keybag_out = _LegacyKeybag(random.Random(1), "secret")
    keybag = LegacyKeybag(keybag_out.blob)
    assert keybag.attrs[b"ITER"] == 10_000
    assert b"DPSL" not in keybag.attrs  # iOS 10.2 added that round
    with pytest.raises(IncorrectPassphraseError):
        keybag.unlock("wrong")
    keybag.unlock("secret")
    file_key = os.urandom(32)
    assert keybag.unwrap_file_key(keybag_out.wrap_key(3, file_key)) == file_key
    assert keybag.unwrap_file_key(struct.pack("<l", 99) + aes_wrap(KEK, file_key)) is None


def test_only_a_keybag_without_dpsl_gets_a_key_derived(tmp_path):
    # iOS 10.0 and 10.1: Manifest.db, but this older keybag.
    assert passphrase_key_without_dpsl(str(tmp_path), "secret") is None  # no Manifest.plist
    old = _LegacyKeybag(random.Random(3), "secret")
    (tmp_path / "Manifest.plist").write_bytes(plistlib.dumps({"BackupKeyBag": old.blob}))
    assert passphrase_key_without_dpsl(str(tmp_path), "secret") == \
        hashlib.pbkdf2_hmac("sha1", b"secret", old.salt, old.iter, 32)
    modern = Keybag(random.Random(3), {"classes": [{"CLAS": 3, "WRAP": 3}]}, "secret")
    (tmp_path / "Manifest.plist").write_bytes(plistlib.dumps({"BackupKeyBag": modern.blob}))
    assert passphrase_key_without_dpsl(str(tmp_path), "secret") is None


def test_a_keybag_with_absurd_iterations_is_refused():
    blob = _LegacyKeybag(random.Random(2), "secret").blob
    at = blob.index(b"ITER") + 8
    blob = blob[:at] + struct.pack(">L", 4_000_000_000) + blob[at + 4:]
    with pytest.raises(ValueError, match="damaged"):
        LegacyKeybag(blob).unlock("secret")


@pytest.mark.parametrize("size_known", [True, False])
@pytest.mark.parametrize("length", [0, 1, 15, 16, 17, 1024 * 1024 + 5])
def test_decrypt_restores_the_plaintext(tmp_path, length, size_known):
    plain = os.urandom(length)
    key = os.urandom(32)
    src, dst = tmp_path / "enc", tmp_path / "out"
    src.write_bytes(encrypt_cbc(key, plain))
    LegacyEncryptedBackup._decrypt(str(src), str(dst), key, length if size_known else 0)
    assert dst.read_bytes() == plain


def test_decrypt_rejects_a_file_cut_mid_block(tmp_path):
    src = tmp_path / "enc"
    src.write_bytes(encrypt_cbc(KEY, b"x" * 40)[:-5])
    with pytest.raises(ValueError, match="multiple of 16"):
        LegacyEncryptedBackup._decrypt(str(src), str(tmp_path / "out"), KEY, 40)
