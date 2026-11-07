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
from html.parser import HTMLParser

from ios_backup_core.db import connect_readonly, get_database
from ios_backup_core.protobuf import extract_protobuf_strings
from ios_backup_core.timestamps import apple_to_iso

# Core Data numbers attribute columns per entity, and the numbering moves between
# releases: iOS 15 keeps a note's creation date in ZCREATIONDATE3, while older
# releases use ZCREATIONDATE1 or ZCREATIONDATE. A note row only has the note
# entity's columns set, so COALESCE over whichever columns exist finds it.
_NOTE_CREATED_COLUMNS = ("ZCREATIONDATE3", "ZCREATIONDATE2", "ZCREATIONDATE1", "ZCREATIONDATE")
_NOTE_MODIFIED_COLUMNS = ("ZMODIFICATIONDATE1", "ZMODIFICATIONDATE")


class _HtmlText(HTMLParser):
    """Plain text from the HTML a notes.sqlite note body is (iOS 4 to 8)."""

    _BREAKS = {"br", "div", "p", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre"}
    _SKIP = {"style", "script", "head", "title"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list = []
        self._skipping = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skipping += 1
        elif tag in self._BREAKS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP:
            self._skipping = max(0, self._skipping - 1)

    def handle_data(self, data):
        if not self._skipping:
            self.parts.append(data)


def html_to_text(markup: str) -> str:
    """The text of a note body stored as HTML, one line per block (div, p, br...)."""
    parser = _HtmlText()
    parser.feed(markup)
    parser.close()
    lines = [" ".join(line.split()) for line in "".join(parser.parts).replace("\xa0", " ").split("\n")]
    text = "\n".join(lines)
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")
    return text.strip()


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
        # Try legacy notes.sqlite first (iOS 8 and earlier)
        db_path = get_database(backup, self.NOTES_DB_PATH, domain="HomeDomain")

        notes = []

        if db_path:
            notes = self._parse_legacy_notes(db_path, backup)

        if not notes:
            # Try the group container path for newer iOS via manifest scan.
            files = backup.list_files(path_like="%NoteStore.sqlite")
            for f in files:
                extracted = get_database(backup, f["path"], domain=f["domain"])
                if extracted:
                    notes = self._parse_notestore(extracted, backup)
                    if notes:
                        break

        if not notes:
            # Encrypted backups: Manifest.db is encrypted so list_files() returns
            # nothing. Try the well-known iOS 11+ group container path directly.
            notestore_path = get_database(
                backup,
                "NoteStore.sqlite",
                domain="AppDomainGroup-group.com.apple.notes",
            )
            if notestore_path:
                notes = self._parse_notestore(notestore_path, backup)

        return {"notes": notes}

    def _parse_legacy_notes(self, db_path: str, backup=None) -> list:
        """Parse Library/Notes/notes.sqlite, the notes of iOS 8 and earlier."""
        notes = self._parse_coredata_notes(db_path, backup)
        if notes:
            return notes
        notes = []
        try:
            conn = connect_readonly(db_path, backup)
            conn.row_factory = sqlite3.Row
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

    def _parse_coredata_notes(self, db_path: str, backup=None) -> list:
        """notes.sqlite from iOS 4 to 8: ZNOTE rows, each with an HTML body in ZNOTEBODY.

        iOS 9 moved notes to NoteStore.sqlite, but keeps notes.sqlite for
        notes in accounts that weren't upgraded.
        """
        notes = []
        conn = None
        try:
            conn = connect_readonly(db_path, backup)
            conn.row_factory = sqlite3.Row
            columns = {r[1] for r in conn.execute("PRAGMA table_info(ZNOTE)")}
            if not columns:
                return notes
            deleted = "COALESCE(n.ZDELETEDFLAG, 0) = 0" if "ZDELETEDFLAG" in columns else "1"
            rows = conn.execute(f"""
                SELECT n.Z_PK, n.ZTITLE AS title, n.ZCREATIONDATE AS created,
                       n.ZMODIFICATIONDATE AS modified, b.ZCONTENT AS content
                FROM ZNOTE n
                LEFT JOIN ZNOTEBODY b ON b.ZOWNER = n.Z_PK
                WHERE {deleted}
                ORDER BY n.ZMODIFICATIONDATE DESC
            """).fetchall()
            for row in rows:
                content = row["content"]
                if isinstance(content, bytes):
                    content = content.decode("utf-8", errors="replace")
                notes.append({
                    "note_id": row["Z_PK"],
                    "title": row["title"] or "Untitled",
                    "body": html_to_text(content or "")[:20000],
                    "created": apple_to_iso(row["created"]),
                    "modified": apple_to_iso(row["modified"]),
                })
        except sqlite3.Error:
            pass
        finally:
            if conn is not None:
                conn.close()
        return notes

    def _parse_notestore(self, db_path: str, backup=None) -> list:
        """Parse the newer NoteStore.sqlite format (iOS 11+)."""
        notes = []
        try:
            conn = connect_readonly(db_path, backup)
            conn.row_factory = sqlite3.Row
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
