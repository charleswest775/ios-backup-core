"""
Notes extraction from NoteStore.sqlite.

Extracted from openextract/python/notes.py:NoteExtractor.
Changes:
  - export_notes() removed — UI concern
  - apple_date_to_iso import replaced with ios_backup_core.timestamps.apple_to_iso
  - _extract_pb_strings() replaced with import from ios_backup_core.protobuf
"""

import gzip
import sqlite3

from ios_backup_core.protobuf import extract_protobuf_strings
from ios_backup_core.timestamps import apple_to_iso

# Core Data numbers attribute columns per entity, and the numbering moves between
# releases: iOS 15 keeps a note's creation date in ZCREATIONDATE3, while older
# releases use ZCREATIONDATE1 or ZCREATIONDATE. A note row only has the note
# entity's columns set, so COALESCE over whichever columns exist finds it.
_NOTE_CREATED_COLUMNS = ("ZCREATIONDATE3", "ZCREATIONDATE2", "ZCREATIONDATE1", "ZCREATIONDATE")
_NOTE_MODIFIED_COLUMNS = ("ZMODIFICATIONDATE1", "ZMODIFICATIONDATE")


def _coalesce_present(columns: set, candidates: tuple) -> str:
    present = [f"n.{c}" for c in candidates if c in columns]
    if not present:
        return "NULL"
    return present[0] if len(present) == 1 else f"COALESCE({', '.join(present)})"


class NoteExtractor:
    """Extracts notes from iOS backups."""

    NOTES_DB_PATH = "Library/Notes/notes.sqlite"

    def list_notes(self, backup) -> dict:
        """List all notes with content."""
        # Try legacy notes.sqlite first (iOS < 9 / older path)
        db_path = backup.get_file(self.NOTES_DB_PATH, domain="HomeDomain")

        notes = []

        if db_path:
            notes = self._parse_legacy_notes(db_path)

        if not notes:
            # Try the group container path for newer iOS via manifest scan.
            files = backup.list_files(path_like="%NoteStore.sqlite")
            for f in files:
                extracted = backup.get_file(f["path"], domain=f["domain"])
                if extracted:
                    notes = self._parse_notestore(extracted)
                    if notes:
                        break

        if not notes:
            # Encrypted backups: Manifest.db is encrypted so list_files() returns
            # nothing. Try the well-known iOS 11+ group container path directly.
            notestore_path = backup.get_file(
                "NoteStore.sqlite",
                domain="AppDomainGroup-group.com.apple.notes",
            )
            if notestore_path:
                notes = self._parse_notestore(notestore_path)

        return {"notes": notes}

    def _parse_legacy_notes(self, db_path: str) -> list:
        """Parse the older notes.sqlite format."""
        notes = []
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only = TRUE")
            conn.execute("PRAGMA synchronous = OFF")
            conn.execute("PRAGMA cache_size = -10000")
            conn.execute("PRAGMA temp_store = MEMORY")

            rows = conn.execute("""
                SELECT
                    n.ROWID,
                    n.creation_date,
                    n.modification_date,
                    n.title,
                    nb.data AS body_data
                FROM note n
                LEFT JOIN note_bodies nb ON nb.note_id = n.ROWID
                ORDER BY n.modification_date DESC
            """).fetchall()

            for row in rows:
                body = ""
                if row["body_data"]:
                    if isinstance(row["body_data"], bytes):
                        body = row["body_data"].decode("utf-8", errors="replace")
                    else:
                        body = str(row["body_data"])

                notes.append({
                    "note_id": row["ROWID"],
                    "title": row["title"] or "Untitled",
                    "body": body[:5000],
                    "created": apple_to_iso(row["creation_date"]),
                    "modified": apple_to_iso(row["modification_date"]),
                })

            conn.close()
        except Exception:
            pass

        return notes

    def _parse_notestore(self, db_path: str) -> list:
        """Parse the newer NoteStore.sqlite format (iOS 11+)."""
        notes = []
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only = TRUE")
            conn.execute("PRAGMA synchronous = OFF")
            conn.execute("PRAGMA cache_size = -10000")
            conn.execute("PRAGMA temp_store = MEMORY")

            columns = {r[1] for r in conn.execute("PRAGMA table_info(ZICCLOUDSYNCINGOBJECT)")}
            created = _coalesce_present(columns, _NOTE_CREATED_COLUMNS)
            modified = _coalesce_present(columns, _NOTE_MODIFIED_COLUMNS)
            rows = conn.execute(f"""
                SELECT
                    n.Z_PK,
                    n.ZTITLE1 AS title,
                    {created} AS created,
                    {modified} AS modified,
                    n.ZSNIPPET AS snippet,
                    nd.ZDATA AS body_data
                FROM ZICCLOUDSYNCINGOBJECT n
                LEFT JOIN ZICNOTEDATA nd ON nd.ZNOTE = n.Z_PK
                WHERE n.ZTITLE1 IS NOT NULL
                ORDER BY modified DESC
            """).fetchall()

            for row in rows:
                body = row["snippet"] or ""
                if row["body_data"]:
                    data = row["body_data"]
                    idx = data.find(b'\x1f\x8b\x08')
                    if idx != -1:
                        try:
                            decompressed = gzip.decompress(data[idx:])
                            pb_strings = extract_protobuf_strings(decompressed)
                            if pb_strings:
                                text = max(pb_strings, key=len)
                                if len(text) > len(body):
                                    body = text.strip()
                        except Exception:
                            pass

                notes.append({
                    "note_id": row["Z_PK"],
                    "title": row["title"] or "Untitled",
                    "body": body[:20000],
                    "created": apple_to_iso(row["created"]),
                    "modified": apple_to_iso(row["modified"]),
                })

            conn.close()
        except Exception:
            pass

        return notes
