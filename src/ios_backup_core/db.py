"""
Read-only access to the SQLite databases in a backup.

A backup must never change, and it may sit on a read-only disk or in a folder
this process can't write to. A plain sqlite3.connect() breaks both rules on a
WAL-mode database, which most iOS databases are (Manifest.db included): SQLite
needs a -shm file beside the database, so the first query fails in a
read-only folder ("unable to open database file"), and in a writable one it
leaves new files in the backup.
"""

import os
import sqlite3
from typing import Optional
from urllib.parse import quote


def get_database(backup, relative_path: str, domain: str = "HomeDomain") -> Optional[str]:
    """Return a local path to a database in the backup, with its -wal beside it if any.

    Uses the accessor's get_database() when it has one (LocalBackupAccessor
    does). Otherwise falls back to get_file(), extracting the -wal too.
    """
    get_db = getattr(backup, "get_database", None)
    if callable(get_db):
        return get_db(relative_path, domain=domain)
    db_path = backup.get_file(relative_path, domain=domain)
    if db_path:
        backup.get_file(relative_path + "-wal", domain=domain)
    return db_path


def connect_readonly(db_path: str, backup=None) -> sqlite3.Connection:
    """Open a SQLite database for reading, without writing to it or beside it.

    A file inside the backup folder (``backup.backup_dir``) is opened
    "immutable": SQLite then takes no locks and never creates -wal, -shm or
    -journal files. Elsewhere (a file decrypted or copied into the temp dir),
    a non-empty -wal beside the database is merged by opening it "mode=ro",
    which creates its -shm there.

    Raises sqlite3.Error if the file can't be read as a database.
    """
    uri = _file_uri(db_path)
    uris = [f"{uri}?mode=ro&immutable=1"]
    wal_path = db_path + "-wal"
    in_backup = _is_within(db_path, getattr(backup, "backup_dir", None))
    if not in_backup and os.path.isfile(wal_path) and os.path.getsize(wal_path) > 0:
        uris.insert(0, f"{uri}?mode=ro")
    error: Optional[sqlite3.Error] = None
    for candidate in uris:
        conn = None
        try:
            conn = sqlite3.connect(candidate, uri=True)
            # SQLite opens lazily, so a bad file only fails on the first query.
            conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
            return conn
        except sqlite3.Error as e:
            if conn is not None:
                conn.close()
            error = e
    raise error


def _file_uri(path: str) -> str:
    """Build a SQLite file: URI for *path* (Windows drive and UNC paths included)."""
    p = os.path.abspath(path).replace(os.sep, "/")
    if not p.startswith("/"):
        p = "/" + p  # C:/x -> /C:/x
    # A UNC path //server/share stays as is: "file://" + "//server/share"
    # leaves the URI authority empty, which is what SQLite requires.
    return "file://" + quote(p, safe="/:")


def _is_within(path: str, directory: Optional[str]) -> bool:
    if not directory:
        return False
    try:
        root = os.path.realpath(directory)
        return os.path.commonpath([os.path.realpath(path), root]) == root
    except ValueError:  # different drives on Windows
        return False
