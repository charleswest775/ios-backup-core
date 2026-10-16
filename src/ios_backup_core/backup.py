"""
BackupAccessor protocol and LocalBackupAccessor implementation.

Adapted from openextract/python/backup.py (OpenBackup class, lines 66-220).
Electron/sidecar-specific pieces removed. Logging stripped — library is silent.

The BackupReader high-level API wires all extractors together for convenience.
"""

import os
import plistlib
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional, Protocol, runtime_checkable

from ios_backup_core.db import connect_readonly

# iphone_backup_decrypt is a soft dependency — only needed for encrypted backups.
try:
    from iphone_backup_decrypt import EncryptedBackup  # type: ignore[import]
    _HAS_DECRYPT = True
except ImportError:
    _HAS_DECRYPT = False


# ---------------------------------------------------------------------------
# BackupAccessor protocol — the interface all extractors depend on
# ---------------------------------------------------------------------------

@runtime_checkable
class BackupAccessor(Protocol):
    """Minimal interface for reading files out of an iPhone backup."""

    @property
    def backup_dir(self) -> str: ...

    @property
    def udid(self) -> str: ...

    @property
    def info(self) -> dict: ...

    @property
    def encrypted(self) -> bool: ...

    def get_file(
        self, relative_path: str, domain: str = "HomeDomain"
    ) -> Optional[str]:
        """Extract a file and return its local path, or None if not present."""
        ...

    def list_files(
        self,
        domain: Optional[str] = None,
        path_like: Optional[str] = None,
    ) -> list[dict]:
        """Return a list of file manifest entries matching the filters.

        Each entry is a dict with keys: 'hash', 'domain', 'path'.
        """
        ...


# ---------------------------------------------------------------------------
# LocalBackupAccessor — reads backups directly from disk
# ---------------------------------------------------------------------------

class LocalBackupAccessor:
    """Default implementation for reading backups from disk.

    Supports both encrypted (via iphone-backup-decrypt) and unencrypted
    iTunes/Finder backups. Adapted from OpenExtract's OpenBackup class;
    Electron-specific pieces removed.
    """

    def __init__(
        self,
        backup_dir: str,
        udid: str,
        info: dict,
        encrypted: bool,
        decrypted_backup=None,
    ):
        self._backup_dir = backup_dir
        self._udid = udid
        self._info = info
        self._encrypted = encrypted
        self._decrypted_backup = decrypted_backup
        self._manifest_db_path: Optional[str] = None
        self._manifest_conn: Optional[sqlite3.Connection] = None
        self._temp_dir = tempfile.mkdtemp(prefix="iosbackup_")
        self._file_cache: dict[str, Optional[str]] = {}

    # -- Protocol properties -------------------------------------------------

    @property
    def backup_dir(self) -> str:
        return self._backup_dir

    @property
    def udid(self) -> str:
        return self._udid

    @property
    def info(self) -> dict:
        return self._info

    @property
    def encrypted(self) -> bool:
        return self._encrypted

    # -- Core file access ----------------------------------------------------

    def get_file(self, relative_path: str, domain: str = "HomeDomain") -> Optional[str]:
        """Extract a file from the backup and return its path on disk.

        For unencrypted backups, looks up the SHA-1 hash in Manifest.db and
        returns the direct path (no copy needed).
        For encrypted backups, delegates to iphone_backup_decrypt.
        Results are cached to avoid redundant extractions.
        """
        cache_key = f"{domain}:{relative_path}"
        if cache_key in self._file_cache:
            cached = self._file_cache[cache_key]
            return cached if cached else None

        output_path = os.path.join(
            self._temp_dir,
            relative_path.replace("/", "--").replace("\\", "--"),
        )

        if self._encrypted and self._decrypted_backup:
            try:
                self._decrypted_backup.extract_file(
                    relative_path=relative_path,
                    domain_like=domain,
                    output_filename=output_path,
                )
                if os.path.exists(output_path):
                    self._file_cache[cache_key] = output_path
                    return output_path
            except Exception as e:
                print(
                    f"[backup.get_file] extract_file failed for {domain}:{relative_path}: {e}",
                    file=sys.stderr, flush=True,
                )
                self._file_cache[cache_key] = None
                return None
        else:
            # Unencrypted: look up file hash in Manifest.db
            file_hash = self._lookup_file_hash(relative_path, domain)
            if file_hash:
                source = os.path.join(self._backup_dir, file_hash[:2], file_hash)
                if os.path.exists(source):
                    self._file_cache[cache_key] = source
                    return source

        self._file_cache[cache_key] = None
        return None

    def get_database(self, relative_path: str, domain: str = "HomeDomain") -> Optional[str]:
        """Return a path to a SQLite database, with its -wal beside it if it has one.

        In an unencrypted backup every file is stored under its own hash, so a
        database's -wal never sits next to it and SQLite would ignore it. When
        the backup has a non-empty -wal, both are copied into the temp dir
        under matching names; the backup itself is never written to. Decrypted
        files already land side by side in the temp dir.
        """
        db_path = self.get_file(relative_path, domain)
        if not db_path:
            return None
        wal_path = self.get_file(relative_path + "-wal", domain)
        if not wal_path or wal_path == db_path + "-wal" or os.path.getsize(wal_path) == 0:
            return db_path
        cache_key = f"db:{domain}:{relative_path}"
        if cache_key not in self._file_cache:
            name = os.path.basename(relative_path) or "database"
            self._file_cache[cache_key] = self._copy_with_wal(db_path, wal_path, name)
        return self._file_cache[cache_key] or db_path

    def list_files(
        self,
        domain: Optional[str] = None,
        path_like: Optional[str] = None,
    ) -> list[dict]:
        """List files in the backup matching optional filters."""
        if self._encrypted and self._decrypted_backup:
            try:
                query = "SELECT fileID, domain, relativePath FROM Files WHERE flags=1"
                params: list = []
                if domain:
                    query += " AND domain = ?"
                    params.append(domain)
                if path_like:
                    query += " AND relativePath LIKE ?"
                    params.append(path_like)
                with self._decrypted_backup.manifest_db_cursor() as cur:
                    cur.execute(query, params)
                    return [
                        {"hash": row[0], "domain": row[1], "path": row[2]}
                        for row in cur.fetchall()
                    ]
            except Exception as e:
                print(
                    f"[backup.list_files] encrypted manifest query failed: {e}",
                    file=sys.stderr, flush=True,
                )
                return []

        manifest = self._get_manifest_db()
        if not manifest:
            return []

        try:
            conn = connect_readonly(manifest, self)
            query = "SELECT fileID, domain, relativePath FROM Files WHERE 1=1"
            params = []
            if domain:
                query += " AND domain = ?"
                params.append(domain)
            if path_like:
                query += " AND relativePath LIKE ?"
                params.append(path_like)

            cursor = conn.execute(query, params)
            results = [
                {"hash": row[0], "domain": row[1], "path": row[2]}
                for row in cursor.fetchall()
            ]
            conn.close()
            return results
        except Exception:
            return []

    def cleanup(self) -> None:
        """Remove the temporary directory used for extracted files."""
        if os.path.exists(self._temp_dir):
            shutil.rmtree(self._temp_dir, ignore_errors=True)

    def __del__(self):
        try:
            self.cleanup()
        except Exception:
            pass

    # -- Internal helpers ----------------------------------------------------

    def _get_manifest_db(self) -> Optional[str]:
        if self._manifest_db_path and os.path.exists(self._manifest_db_path):
            return self._manifest_db_path
        manifest_path = os.path.join(self._backup_dir, "Manifest.db")
        if not os.path.exists(manifest_path):
            return None
        wal_path = manifest_path + "-wal"
        if os.path.isfile(wal_path) and os.path.getsize(wal_path) > 0:
            # Finder left changes in the WAL (an interrupted backup, say).
            copy = self._copy_with_wal(manifest_path, wal_path, "Manifest.db")
            manifest_path = copy or manifest_path
        self._manifest_db_path = manifest_path
        return manifest_path

    def _copy_with_wal(self, db_path: str, wal_path: str, name: str) -> Optional[str]:
        """Copy a database and its WAL into the temp dir as <name> and <name>-wal."""
        dest_dir = tempfile.mkdtemp(prefix="db_", dir=self._temp_dir)
        dest = os.path.join(dest_dir, name)
        try:
            shutil.copyfile(db_path, dest)
            shutil.copyfile(wal_path, dest + "-wal")
        except OSError as e:
            print(
                f"[backup] could not copy {name} to read its WAL, reading it without: {e}",
                file=sys.stderr, flush=True,
            )
            shutil.rmtree(dest_dir, ignore_errors=True)
            return None
        return dest

    def _get_manifest_conn(self) -> Optional[sqlite3.Connection]:
        if self._manifest_conn is not None:
            return self._manifest_conn
        manifest = self._get_manifest_db()
        if not manifest:
            return None
        try:
            conn = connect_readonly(manifest, self)
            conn.execute("PRAGMA cache_size = -10000")
            conn.execute("PRAGMA temp_store = MEMORY")
            self._manifest_conn = conn
            return conn
        except Exception:
            return None

    def _lookup_file_hash(self, relative_path: str, domain: str) -> Optional[str]:
        conn = self._get_manifest_conn()
        if not conn:
            return None
        try:
            cursor = conn.execute(
                "SELECT fileID FROM Files WHERE relativePath = ? AND domain = ?",
                (relative_path, domain),
            )
            row = cursor.fetchone()
            return row[0] if row else None
        except Exception:
            return None


# ---------------------------------------------------------------------------
# Factory helpers
# ---------------------------------------------------------------------------

def _read_backup_info(backup_dir: str) -> Optional[dict]:
    """Read metadata from a backup directory. Returns None if not a valid backup."""
    manifest_db = os.path.join(backup_dir, "Manifest.db")
    if not os.path.exists(manifest_db):
        return None

    info_plist = os.path.join(backup_dir, "Info.plist")
    manifest_plist = os.path.join(backup_dir, "Manifest.plist")

    info: dict = {}
    if os.path.exists(info_plist):
        try:
            with open(info_plist, "rb") as f:
                plist = plistlib.load(f)
            info = {
                "udid": plist.get("Unique Identifier", os.path.basename(backup_dir)),
                "device_name": plist.get("Device Name", "Unknown"),
                "product_type": plist.get("Product Type", "Unknown"),
                "product_version": plist.get("Product Version", "Unknown"),
                "serial_number": plist.get("Serial Number", ""),
                "phone_number": plist.get("Phone Number", ""),
                "last_backup": plist.get("Last Backup Date", ""),
            }
            if isinstance(info["last_backup"], datetime):
                info["last_backup"] = info["last_backup"].isoformat()
        except Exception:
            info = {
                "udid": os.path.basename(backup_dir),
                "device_name": "Unknown",
                "product_type": "Unknown",
                "product_version": "Unknown",
            }
    else:
        info = {
            "udid": os.path.basename(backup_dir),
            "device_name": "Unknown",
            "product_type": "Unknown",
            "product_version": "Unknown",
            "serial_number": "",
            "phone_number": "",
            "last_backup": "",
        }

    encrypted = False
    if os.path.exists(manifest_plist):
        try:
            with open(manifest_plist, "rb") as f:
                manifest = plistlib.load(f)
            encrypted = manifest.get("IsEncrypted", False)
        except Exception:
            pass

    info["encrypted"] = encrypted
    info["backup_dir"] = backup_dir
    return info


def open_local_backup(
    backup_dir: str,
    password: Optional[str] = None,
) -> "LocalBackupAccessor":
    """Open an on-disk backup and return a LocalBackupAccessor.

    Raises:
        ValueError — backup_dir does not contain a valid backup, or
                     decryption fails.
        RuntimeError — encrypted backup requested but iphone-backup-decrypt
                       is not installed.
    """
    info = _read_backup_info(backup_dir)
    if not info:
        raise ValueError(f"No valid backup found at: {backup_dir}")

    encrypted = info["encrypted"]
    decrypted_backup = None

    if encrypted:
        if not password:
            raise ValueError(
                "Backup is encrypted. Provide a password or use "
                "LocalBackupAccessor directly with an EncryptedBackup instance."
            )
        if not _HAS_DECRYPT:
            raise RuntimeError(
                "iphone-backup-decrypt is not installed. "
                "Run: pip install iphone-backup-decrypt"
            )
        try:
            decrypted_backup = EncryptedBackup(
                backup_directory=backup_dir,
                passphrase=password,
            )
            # Validate passphrase early
            with decrypted_backup.manifest_db_cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM Files")
        except Exception as e:
            raise ValueError(f"Failed to decrypt backup: {e}") from e

    return LocalBackupAccessor(
        backup_dir=backup_dir,
        udid=info.get("udid", os.path.basename(backup_dir)),
        info=info,
        encrypted=encrypted,
        decrypted_backup=decrypted_backup,
    )


# ---------------------------------------------------------------------------
# BackupReader — high-level convenience API
# ---------------------------------------------------------------------------

class BackupReader:
    """High-level API that wires a BackupAccessor to all extractors.

    Usage:
        reader = BackupReader.from_path("/path/to/backup", password="secret")
        contacts = reader.contacts()
        msgs = reader.messages(chat_id=1)
        calls = reader.calls()
    """

    def __init__(self, accessor: BackupAccessor):
        self._accessor = accessor
        self._contacts_cache: Optional[dict] = None

    @classmethod
    def from_path(
        cls,
        backup_dir: str,
        password: Optional[str] = None,
    ) -> "BackupReader":
        """Create a BackupReader from a backup directory path."""
        accessor = open_local_backup(backup_dir, password=password)
        return cls(accessor)

    @property
    def ios_version(self) -> Optional[str]:
        return self._accessor.info.get("product_version")

    def contacts(self) -> dict:
        """Load and cache contacts from the backup. Returns a lookup dict."""
        if self._contacts_cache is None:
            from ios_backup_core.contacts import ContactResolver
            resolver = ContactResolver()
            self._contacts_cache = resolver.load_contacts(self._accessor)
        return self._contacts_cache

    def messages(self, chat_id: Optional[int] = None, **kwargs):
        """List conversations or get messages from a specific chat."""
        from ios_backup_core.extractors.messages import MessageExtractor
        extractor = MessageExtractor()
        contacts = self.contacts()
        if chat_id is not None:
            return extractor.get_messages(self._accessor, chat_id, contacts, **kwargs)
        return extractor.list_conversations(self._accessor, contacts)

    def calls(self, **kwargs):
        """List call history records."""
        from ios_backup_core.extractors.calls import CallExtractor
        extractor = CallExtractor()
        contacts = self.contacts()
        return extractor.list_calls(self._accessor, contacts, **kwargs)

    def notes(self, **kwargs):
        """List notes."""
        from ios_backup_core.extractors.notes import NoteExtractor
        extractor = NoteExtractor()
        return extractor.list_notes(self._accessor)

    def browser_history(self, **kwargs):
        """List browser history (Safari + Firefox)."""
        from ios_backup_core.extractors.browser_history import BrowserHistoryExtractor
        extractor = BrowserHistoryExtractor()
        return extractor.list_browser_history(self._accessor, **kwargs)

    def voicemail(self, **kwargs):
        """List voicemails."""
        from ios_backup_core.extractors.voicemail import VoicemailExtractor
        extractor = VoicemailExtractor()
        contacts = self.contacts()
        return extractor.list_voicemails(self._accessor, contacts)
