"""
Voice Memos extraction.

The Voice Memos app keeps a Core Data index next to the audio files in a
``Recordings/`` folder:

  - CloudRecordings.db, table ZCLOUDRECORDING (iOS 12+). Current iOS keeps it
    in the AppDomainGroup-group.com.apple.VoiceMemos.shared domain; older
    versions used MediaDomain ``Media/Recordings/``.
  - Recordings.db, table ZRECORDING (iOS 11 and earlier).

Audio is .m4a, or .qta (QuickTime audio) on newer iOS. ZPATH holds the file
name — or a full device path on old versions — so audio is matched to rows by
base name against the manifest.

Memos in "Recently Deleted" are returned with ``deleted: True``: Voice Memos
sets ZEVICTIONDATE when a memo is deleted and leaves it NULL otherwise.
"""

import os
import sqlite3
import sys
from typing import Optional

from ios_backup_core.backup import open_database
from ios_backup_core.schema import column_names, table_names
from ios_backup_core.timestamps import apple_to_iso

# Index database names, most preferred first.
_INDEX_TABLES = {
    "CloudRecordings.db": "ZCLOUDRECORDING",
    "Recordings.db": "ZRECORDING",
}

# Title columns, most preferred first. ZENCRYPTEDTITLE holds the current title
# in plain text despite its name; older versions only have ZCUSTOMLABEL.
_TITLE_COLUMNS = ("ZENCRYPTEDTITLE", "ZCUSTOMLABELFORSORTING", "ZCUSTOMLABEL")

# Folder name columns in ZFOLDER, most preferred first.
_FOLDER_NAME_COLUMNS = ("ZENCRYPTEDNAME", "ZNAME", "ZTITLE")

_MIME_TYPES = {
    ".m4a": "audio/mp4",
    ".qta": "audio/mp4",  # QuickTime audio — same ISO BMFF family as .m4a
    ".mp4": "audio/mp4",
    ".aac": "audio/aac",
    ".caf": "audio/x-caf",
    ".wav": "audio/wav",
}


def _basename(path: Optional[str]) -> Optional[str]:
    if not path or not isinstance(path, str):
        return None
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    return name or None


def _first_text(*values) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def mime_type_for(file_name: Optional[str]) -> str:
    ext = os.path.splitext(file_name or "")[1].lower()
    return _MIME_TYPES.get(ext, "application/octet-stream")


class VoiceMemoExtractor:
    """Extracts Voice Memos recordings and their metadata."""

    # ── Discovery ────────────────────────────────────────────────────────────

    def _find_index(self, backup) -> Optional[tuple]:
        """Return (domain, relative_path, table) for the recordings index, or None."""
        try:
            files = backup.list_files(path_like="%Recordings/%Recordings.db")
        except Exception:
            return None
        candidates = []
        for f in files:
            name = _basename(f.get("path"))
            if name not in _INDEX_TABLES:
                continue
            domain = f.get("domain", "")
            name_rank = list(_INDEX_TABLES).index(name)
            # The app-group copy is the live one on current iOS; a MediaDomain
            # copy can linger from before the move.
            domain_rank = 0 if "voicememos" in domain.lower() else 1
            candidates.append((name_rank, domain_rank, domain, f["path"], _INDEX_TABLES[name]))
        if not candidates:
            return None
        _, _, domain, path, table = min(candidates)
        return domain, path, table

    def _audio_files(self, backup, preferred_domain: str) -> dict:
        """Map audio file base name → (domain, relative_path)."""
        try:
            files = backup.list_files(path_like="%Recordings/%")
        except Exception:
            return {}
        found: dict = {}
        for f in files:
            name = _basename(f.get("path"))
            # Skip the index itself and edit bundles (".composition" folders).
            if not name or name.endswith((".db", "-wal", "-shm", ".plist", ".composition")):
                continue
            domain = f.get("domain", "")
            # Keep the copy that lives next to the index when names collide.
            if name not in found or domain == preferred_domain:
                found[name] = (domain, f["path"])
        return found

    # ── Listing ──────────────────────────────────────────────────────────────

    def list_voice_memos(self, backup) -> dict:
        """List recordings, newest first.

        Returns ``{"voice_memos": [...], "errors": [...]}``. Each memo has
        ``id``, ``title``, ``date`` (ISO UTC), ``duration`` (seconds),
        ``folder``, ``deleted``, ``deleted_date``, ``file_name`` and
        ``has_audio``.
        """
        index = self._find_index(backup)
        if not index:
            return {"voice_memos": [], "errors": []}
        domain, path, table = index

        db_path = open_database(backup, path, domain)
        if not db_path:
            return {"voice_memos": [], "errors": []}

        try:
            rows, folders = self._read_index(db_path, table)
        except Exception as e:
            print(f"[voice_memos] parse error: {e}", file=sys.stderr, flush=True)
            return {
                "voice_memos": [],
                "errors": [{"message": f"Couldn't read the Voice Memos database ({e})."}],
            }

        audio = self._audio_files(backup, domain)
        memos = []
        for row in rows:
            file_name = _basename(row["ZPATH"])
            eviction = row["ZEVICTIONDATE"]
            memos.append({
                "id": row["Z_PK"],
                "title": _first_text(*(row[c] for c in _TITLE_COLUMNS)),
                "date": apple_to_iso(row["ZDATE"]),
                "duration": float(row["ZDURATION"] or 0),
                "folder": folders.get(row["ZFOLDER"]),
                "deleted": bool(eviction),
                "deleted_date": apple_to_iso(eviction) if eviction else None,
                "file_name": file_name,
                "has_audio": bool(file_name and file_name in audio),
            })
        memos.sort(key=lambda m: m["date"] or "", reverse=True)
        return {"voice_memos": memos, "errors": []}

    @staticmethod
    def _read_index(db_path: str, table: str) -> tuple:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA query_only = TRUE")
            cols = column_names(conn, table)
            wanted = ("Z_PK", "ZDATE", "ZDURATION", "ZPATH", "ZEVICTIONDATE", "ZFOLDER") + _TITLE_COLUMNS
            select = ", ".join(c if c in cols else f"NULL AS {c}" for c in wanted)
            rows = conn.execute(f"SELECT {select} FROM {table}").fetchall()

            folders: dict = {}
            if "ZFOLDER" in table_names(conn):
                folder_cols = column_names(conn, "ZFOLDER")
                name_col = next((c for c in _FOLDER_NAME_COLUMNS if c in folder_cols), None)
                if name_col and "Z_PK" in folder_cols:
                    for pk, name in conn.execute(f"SELECT Z_PK, {name_col} FROM ZFOLDER"):
                        if isinstance(name, str) and name.strip():
                            folders[pk] = name.strip()
            return rows, folders
        finally:
            conn.close()

    # ── Audio ────────────────────────────────────────────────────────────────

    def get_audio_file(self, backup, memo_id: int) -> Optional[dict]:
        """Return ``{"path", "file_name", "mime_type"}`` for a memo's audio, or None.

        ``path`` is a local file. For unencrypted backups it is the file inside
        the backup folder, stored under its hash with no extension; use
        ``file_name`` when the extension matters.
        """
        index = self._find_index(backup)
        if not index:
            return None
        domain, path, table = index
        db_path = open_database(backup, path, domain)
        if not db_path:
            return None
        try:
            conn = sqlite3.connect(db_path)
            try:
                row = conn.execute(f"SELECT ZPATH FROM {table} WHERE Z_PK = ?", (memo_id,)).fetchone()
            finally:
                conn.close()
        except Exception as e:
            print(f"[voice_memos] lookup error: {e}", file=sys.stderr, flush=True)
            return None
        file_name = _basename(row[0]) if row else None
        if not file_name:
            return None
        location = self._audio_files(backup, domain).get(file_name)
        if not location:
            return None
        local_path = backup.get_file(location[1], domain=location[0])
        if not local_path or not os.path.exists(local_path):
            return None
        return {"path": local_path, "file_name": file_name, "mime_type": mime_type_for(file_name)}
