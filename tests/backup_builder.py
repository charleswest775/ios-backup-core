"""Test helper: builds a small unencrypted iPhone backup on disk.

A Manifest.db plus files stored under their SHA-1 names, so extractors run
through LocalBackupAccessor the same way they do against a real backup.
"""

import hashlib
import os
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ios_backup_core.backup import LocalBackupAccessor  # noqa: E402


class BackupBuilder:
    """Writes files into a fake unencrypted backup directory."""

    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="ibc_test_")
        self._scratch = tempfile.mkdtemp(prefix="ibc_scratch_")
        self._manifest = sqlite3.connect(os.path.join(self.dir, "Manifest.db"))
        self._manifest.execute(
            "CREATE TABLE Files (fileID TEXT PRIMARY KEY, domain TEXT, "
            "relativePath TEXT, flags INTEGER, file BLOB)"
        )

    def add_bytes(self, domain: str, relative_path: str, data: bytes) -> None:
        file_id = hashlib.sha1(f"{domain}-{relative_path}".encode()).hexdigest()
        os.makedirs(os.path.join(self.dir, file_id[:2]), exist_ok=True)
        with open(os.path.join(self.dir, file_id[:2], file_id), "wb") as f:
            f.write(data)
        self._manifest.execute(
            "INSERT INTO Files VALUES (?, ?, ?, 1, NULL)", (file_id, domain, relative_path)
        )
        self._manifest.commit()

    def add_db(self, domain: str, relative_path: str, build, wal_build=None) -> None:
        """Create a SQLite DB with build(conn).

        If wal_build is given, it runs in WAL mode after build and its writes
        are left un-checkpointed, so they exist only in the -wal file — the
        state iOS often leaves a database in when a backup is taken.
        """
        key = hashlib.sha1(f"{domain}-{relative_path}".encode()).hexdigest()
        path = os.path.join(self._scratch, key)
        conn = sqlite3.connect(path)
        build(conn)
        conn.commit()
        if wal_build is None:
            conn.close()
            with open(path, "rb") as f:
                self.add_bytes(domain, relative_path, f.read())
            return
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_autocheckpoint=0")
        wal_build(conn)
        conn.commit()
        # Snapshot while the connection is open, before any checkpoint.
        for suffix in ("", "-wal"):
            with open(path + suffix, "rb") as f:
                self.add_bytes(domain, relative_path + suffix, f.read())
        conn.close()

    def accessor(self, encrypted: bool = False) -> LocalBackupAccessor:
        self._manifest.close()
        return LocalBackupAccessor(self.dir, "test-udid", {}, encrypted=encrypted)

    def cleanup(self):
        shutil.rmtree(self.dir, ignore_errors=True)
        shutil.rmtree(self._scratch, ignore_errors=True)
