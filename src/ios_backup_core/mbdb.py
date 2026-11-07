"""
Manifest.mbdb: the file list of backups made by iOS 9 and earlier.

From iOS 10 a backup lists its files in Manifest.db, a SQLite database, and
keeps each one at ``<fileID[:2]>/<fileID>``. Before that (iTunes backups from
about 2011 to 2016) it listed them in Manifest.mbdb, a binary file, and kept
them flat in the backup folder at ``<fileID>``. In both, fileID is
SHA-1("<domain>-<relativePath>").

This module reads Manifest.mbdb and writes its records into a SQLite database
shaped like Manifest.db's ``Files`` table, so the rest of the library reads
old and new backups alike.

Format (every integer big-endian):

    "mbdb" 0x05 0x00                      header
    then one record after another:
      domain, path, link target           strings
      data hash, encryption key           strings (raw bytes)
      mode u16, inode u64, uid u32, gid u32,
      mtime u32, atime u32, ctime u32, size u64,
      protection class u8, property count u8
      property count x (name, value)      strings

A string is a u16 length followed by that many bytes; length 0xFFFF means
empty. Paths are UTF-8.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
import struct
from dataclasses import dataclass, field
from typing import Iterator

MBDB_HEADER = b"mbdb\x05\x00"

# Files.flags in Manifest.db
FLAG_FILE = 1
FLAG_DIRECTORY = 2
FLAG_SYMLINK = 4

_FIXED = struct.Struct(">HQIIIIIQBB")  # mode .. property count: 40 bytes


class MbdbError(ValueError):
    """Manifest.mbdb is damaged, cut short, or not in a format this module reads."""


@dataclass
class MbdbRecord:
    domain: str
    relative_path: str
    link_target: str
    data_hash: bytes
    encryption_key: bytes
    mode: int
    inode: int
    uid: int
    gid: int
    mtime: int
    atime: int
    ctime: int
    size: int
    protection_class: int
    properties: dict = field(default_factory=dict)
    file_id: str = ""

    @property
    def flags(self) -> int:
        """Manifest.db's flags for this record: 1 file, 2 directory, 4 symbolic link."""
        kind = stat.S_IFMT(self.mode)
        if kind == stat.S_IFDIR:
            return FLAG_DIRECTORY
        if kind == stat.S_IFLNK:
            return FLAG_SYMLINK
        return FLAG_FILE


def file_id(domain: str, relative_path: str) -> str:
    """The name a backup stores a file under: SHA-1 of "<domain>-<relativePath>"."""
    return hashlib.sha1(f"{domain}-{relative_path}".encode("utf-8")).hexdigest()


def iter_mbdb(data: bytes) -> Iterator[MbdbRecord]:
    """Yield the records of a Manifest.mbdb file's contents.

    Raises MbdbError when the header is wrong or a record is cut short.
    """
    if not data.startswith(b"mbdb"):
        raise MbdbError("not a Manifest.mbdb file")
    if data[:6] != MBDB_HEADER:
        version = ".".join(str(b) for b in data[4:6])
        raise MbdbError(f"unsupported Manifest.mbdb version {version}")

    pos = len(MBDB_HEADER)
    end = len(data)

    def raw_string() -> bytes:
        nonlocal pos
        if pos + 2 > end:
            raise MbdbError(f"Manifest.mbdb is cut short at byte {pos}")
        (length,) = struct.unpack_from(">H", data, pos)
        pos += 2
        if length == 0xFFFF:
            return b""
        if pos + length > end:
            raise MbdbError(f"Manifest.mbdb is cut short at byte {pos}")
        value = data[pos:pos + length]
        pos += length
        return value

    while pos < end:
        domain_raw = raw_string()
        path_raw = raw_string()
        link_raw = raw_string()
        data_hash = raw_string()
        encryption_key = raw_string()
        if pos + _FIXED.size > end:
            raise MbdbError(f"Manifest.mbdb is cut short at byte {pos}")
        (mode, inode, uid, gid, mtime, atime, ctime, size,
         protection_class, property_count) = _FIXED.unpack_from(data, pos)
        pos += _FIXED.size
        properties = {}
        for _ in range(property_count):
            name = raw_string().decode("utf-8", errors="replace")
            properties[name] = raw_string()
        yield MbdbRecord(
            domain=domain_raw.decode("utf-8", errors="replace"),
            relative_path=path_raw.decode("utf-8", errors="replace"),
            link_target=link_raw.decode("utf-8", errors="replace"),
            data_hash=data_hash,
            encryption_key=encryption_key,
            mode=mode, inode=inode, uid=uid, gid=gid,
            mtime=mtime, atime=atime, ctime=ctime, size=size,
            protection_class=protection_class,
            properties=properties,
            # From the raw bytes, so a name that isn't valid UTF-8 still finds its file.
            file_id=hashlib.sha1(domain_raw + b"-" + path_raw).hexdigest(),
        )


def read_mbdb(path: str) -> list[MbdbRecord]:
    """Read every record of the Manifest.mbdb at *path*."""
    with open(path, "rb") as f:
        return list(iter_mbdb(f.read()))


def build_files_db(mbdb_path: str, db_path: str) -> int:
    """Write the records of *mbdb_path* to a new SQLite database at *db_path*.

    The database has Manifest.db's ``Files`` table (fileID, domain,
    relativePath, flags, file), with ``file`` left NULL, plus the record
    fields Manifest.db keeps inside ``file``: size, mode, mtime, protection
    class and the encryption key. Returns the number of records written.

    Raises MbdbError if Manifest.mbdb can't be read; *db_path* is then not
    left behind.
    """
    records = read_mbdb(mbdb_path)
    if os.path.exists(db_path):
        os.remove(db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE Files (fileID TEXT PRIMARY KEY, domain TEXT, relativePath TEXT, "
            "flags INTEGER, file BLOB, size INTEGER, mode INTEGER, mtime INTEGER, "
            "protectionClass INTEGER, encryptionKey BLOB)"
        )
        conn.executemany(
            "INSERT OR REPLACE INTO Files VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)",
            (
                (r.file_id, r.domain, r.relative_path, r.flags, r.size, r.mode,
                 r.mtime, r.protection_class, r.encryption_key or None)
                for r in records
            ),
        )
        # The same lookups Manifest.db is indexed for.
        conn.execute("CREATE INDEX FilesDomainIdx ON Files(domain)")
        conn.execute("CREATE INDEX FilesRelativePathIdx ON Files(relativePath)")
        conn.commit()
    except BaseException:
        conn.close()
        os.remove(db_path)
        raise
    conn.close()
    return len(records)
