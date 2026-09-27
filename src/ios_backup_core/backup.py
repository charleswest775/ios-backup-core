"""
BackupAccessor protocol and LocalBackupAccessor implementation.

Adapted from openextract/python/backup.py (OpenBackup class, lines 66-220).
Electron/sidecar-specific pieces removed. Logging stripped — library is silent.

The BackupReader high-level API wires all extractors together for convenience.
"""

import hashlib
import os
import plistlib
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional, Protocol, runtime_checkable

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


_SQLITE_SIDECARS = ("-wal", "-shm", "-journal")


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

        # Prefix with a short domain hash: different domains can hold files at
        # the same relative path (e.g. both Firefox app groups ship
        # profile.profile/browser.db), and they must not overwrite each other.
        domain_tag = hashlib.sha1(domain.encode("utf-8")).hexdigest()[:8]
        output_path = os.path.join(
            self._temp_dir,
            domain_tag + "--" + relative_path.replace("/", "--").replace("\\", "--"),
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
        """Return a path to a SQLite database with its -wal/-shm/-journal files beside it.

        iOS often leaves recent writes in the write-ahead log. In an
        unencrypted backup every file is stored under its own hash, so SQLite
        never finds the WAL next to the main file and silently shows stale
        data. When sidecars exist, copy the set into the temp dir under
        matching names so SQLite merges them on open.
        """
        main = self.get_file(relative_path, domain)
        if not main:
            return None
        sidecars = {}
        for suffix in _SQLITE_SIDECARS:
            path = self.get_file(relative_path + suffix, domain)
            if path:
                sidecars[suffix] = path
        if all(path == main + suffix for suffix, path in sidecars.items()):
            return main  # no sidecars, or already co-located (decrypted copies)

        cache_key = f"db:{domain}:{relative_path}"
        cached = self._file_cache.get(cache_key)
        if cached:
            return cached
        dest_dir = tempfile.mkdtemp(prefix="db_", dir=self._temp_dir)
        dest = os.path.join(dest_dir, os.path.basename(relative_path) or "database")
        try:
            shutil.copyfile(main, dest)
            for suffix, path in sidecars.items():
                shutil.copyfile(path, dest + suffix)
        except OSError as e:
            print(
                f"[backup.get_database] copy failed for {domain}:{relative_path}: {e}",
                file=sys.stderr, flush=True,
            )
            return main
        self._file_cache[cache_key] = dest
        return dest

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
            conn = sqlite3.connect(manifest)
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
        if os.path.exists(manifest_path):
            self._manifest_db_path = manifest_path
            return manifest_path
        return None

    def _get_manifest_conn(self) -> Optional[sqlite3.Connection]:
        if self._manifest_conn is not None:
            return self._manifest_conn
        manifest = self._get_manifest_db()
        if not manifest:
            return None
        try:
            conn = sqlite3.connect(manifest)
            conn.execute("PRAGMA query_only = TRUE")
            conn.execute("PRAGMA synchronous = OFF")
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


def open_database(backup, relative_path: str, domain: str = "HomeDomain") -> Optional[str]:
    """Return a readable local path for a SQLite database in the backup.

    Uses the accessor's WAL-aware ``get_database`` when it has one (see
    LocalBackupAccessor.get_database) and falls back to ``get_file`` for
    other BackupAccessor implementations.
    """
    get_database = getattr(backup, "get_database", None)
    if callable(get_database):
        return get_database(relative_path, domain=domain)
    return backup.get_file(relative_path, domain=domain)


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
        """List browser history (Safari, Firefox, Chrome, Edge, Brave)."""
        from ios_backup_core.extractors.browser_history import BrowserHistoryExtractor
        extractor = BrowserHistoryExtractor()
        return extractor.list_browser_history(self._accessor, **kwargs)

    def voicemail(self, **kwargs):
        """List voicemails."""
        from ios_backup_core.extractors.voicemail import VoicemailExtractor
        extractor = VoicemailExtractor()
        contacts = self.contacts()
        return extractor.list_voicemails(self._accessor, contacts)

    def voice_memos(self):
        """List Voice Memos recordings."""
        from ios_backup_core.extractors.voice_memos import VoiceMemoExtractor
        return VoiceMemoExtractor().list_voice_memos(self._accessor)

    def calendar_events(self, calendar_id: Optional[int] = None):
        """List calendar events and calendars."""
        from ios_backup_core.extractors.calendar_events import CalendarExtractor
        return CalendarExtractor().list_events(self._accessor, calendar_id=calendar_id)
