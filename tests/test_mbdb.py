"""Tests for ios_backup_core.mbdb, the reader for Manifest.mbdb (backups made by iOS 9 and earlier)."""

import hashlib
import os
import sqlite3
import struct
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, ROOT)

from backupgen.legacy import MBDB_HEADER, mbdb_record  # noqa: E402
from ios_backup_core.mbdb import (  # noqa: E402
    FLAG_DIRECTORY,
    FLAG_FILE,
    FLAG_SYMLINK,
    MbdbError,
    build_files_db,
    file_id,
    iter_mbdb,
    read_mbdb,
)


def _record(domain="HomeDomain", path="Library/SMS/sms.db", **kw):
    kw.setdefault("mode", 0o100644)
    kw.setdefault("inode", 7)
    kw.setdefault("size", 1234)
    kw.setdefault("stamp", 1_400_000_000)
    return mbdb_record(domain, path, **kw)


def test_reads_every_field_of_a_record():
    key = struct.pack("<l", 3) + b"k" * 40
    data = MBDB_HEADER + _record(data_hash=b"h" * 20, encryption_key=key, protection_class=3,
                                 properties={"com.apple.assetsd.UUID": b"ABC", "flag": b"\x01"})
    (record,) = iter_mbdb(data)
    assert record.domain == "HomeDomain"
    assert record.relative_path == "Library/SMS/sms.db"
    assert record.link_target == ""
    assert record.data_hash == b"h" * 20
    assert record.encryption_key == key
    assert (record.mode, record.inode, record.uid, record.gid) == (0o100644, 7, 501, 501)
    assert (record.mtime, record.atime, record.ctime) == (1_400_000_000,) * 3
    assert record.size == 1234
    assert record.protection_class == 3
    assert record.properties == {"com.apple.assetsd.UUID": b"ABC", "flag": b"\x01"}
    assert record.flags == FLAG_FILE


def test_file_id_is_sha1_of_domain_and_path():
    # The well-known name of sms.db in every backup.
    assert file_id("HomeDomain", "Library/SMS/sms.db") == "3d0d7e5fb2ce288813306e4d4636395e047a3d28"
    (record,) = iter_mbdb(MBDB_HEADER + _record())
    assert record.file_id == "3d0d7e5fb2ce288813306e4d4636395e047a3d28"


def test_file_id_uses_the_raw_bytes_of_a_path_that_isnt_utf8():
    raw_path = b"Media/caf\xe9.jpg"  # Latin-1, not UTF-8
    data = MBDB_HEADER + struct.pack(">H", 10) + b"HomeDomain" + struct.pack(">H", len(raw_path)) + raw_path \
        + b"\xff\xff" * 3 + struct.pack(">HQIIIIIQBB", 0o100644, 1, 501, 501, 0, 0, 0, 5, 0, 0)
    (record,) = iter_mbdb(data)
    assert record.file_id == hashlib.sha1(b"HomeDomain-" + raw_path).hexdigest()
    assert record.relative_path == "Media/caf�.jpg"


def test_flags_follow_the_mode():
    data = MBDB_HEADER + _record(path="Library", mode=0o40755, size=0) \
        + _record(path="Library/link", mode=0o120755, size=0, link_target="/var/mobile") + _record()
    records = list(iter_mbdb(data))
    assert [r.flags for r in records] == [FLAG_DIRECTORY, FLAG_SYMLINK, FLAG_FILE]
    assert records[1].link_target == "/var/mobile"


def test_domain_root_has_an_empty_path():
    (record,) = iter_mbdb(MBDB_HEADER + _record(domain="CameraRollDomain", path="", mode=0o40755, size=0))
    assert record.relative_path == ""
    assert record.file_id == file_id("CameraRollDomain", "")


def test_an_empty_file_has_no_records():
    assert list(iter_mbdb(MBDB_HEADER)) == []


@pytest.mark.parametrize("data, message", [
    (b"SQLite format 3\x00", "not a Manifest.mbdb"),
    (b"mbdb\x04\x00", "unsupported Manifest.mbdb version 4.0"),
    (b"", "not a Manifest.mbdb"),
])
def test_rejects_other_files(data, message):
    with pytest.raises(MbdbError, match=message):
        list(iter_mbdb(data))


@pytest.mark.parametrize("cut", [1, 2, 8, 30, 45])
def test_a_record_cut_short_is_an_error(cut):
    data = MBDB_HEADER + _record() + _record(path="Library/AddressBook/AddressBook.sqlitedb")
    with pytest.raises(MbdbError, match="cut short"):
        list(iter_mbdb(data[:-cut]))


def test_build_files_db_has_manifest_db_shape(tmp_path):
    mbdb = tmp_path / "Manifest.mbdb"
    mbdb.write_bytes(MBDB_HEADER + _record(domain="HomeDomain", path="", mode=0o40755, size=0)
                     + _record(protection_class=4, encryption_key=b"\x04\x00\x00\x00" + b"k" * 40))
    db = tmp_path / "Manifest.db"
    assert build_files_db(str(mbdb), str(db)) == 2
    conn = sqlite3.connect(db)
    columns = [r[1] for r in conn.execute("PRAGMA table_info(Files)")]
    assert columns[:5] == ["fileID", "domain", "relativePath", "flags", "file"]
    rows = conn.execute("SELECT fileID, domain, relativePath, flags, file, size, protectionClass, encryptionKey "
                        "FROM Files ORDER BY flags DESC").fetchall()
    assert rows[0] == (file_id("HomeDomain", ""), "HomeDomain", "", FLAG_DIRECTORY, None, 0, 0, None)
    assert rows[1][:6] == ("3d0d7e5fb2ce288813306e4d4636395e047a3d28", "HomeDomain", "Library/SMS/sms.db",
                           FLAG_FILE, None, 1234)
    assert rows[1][6:] == (4, b"\x04\x00\x00\x00" + b"k" * 40)
    conn.close()


def test_build_files_db_leaves_nothing_behind_on_a_damaged_file(tmp_path):
    mbdb = tmp_path / "Manifest.mbdb"
    mbdb.write_bytes(MBDB_HEADER + _record()[:-3])
    db = tmp_path / "Manifest.db"
    with pytest.raises(MbdbError):
        build_files_db(str(mbdb), str(db))
    assert not db.exists()


def test_read_mbdb_reads_a_file(tmp_path):
    path = tmp_path / "Manifest.mbdb"
    path.write_bytes(MBDB_HEADER + _record() + _record(path="Library/Notes/notes.sqlite"))
    assert [r.relative_path for r in read_mbdb(str(path))] == ["Library/SMS/sms.db", "Library/Notes/notes.sqlite"]
