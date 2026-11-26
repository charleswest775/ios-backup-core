"""
Notes extraction from NoteStore.sqlite.

Extracted from openextract/python/notes.py:NoteExtractor.
Changes:
  - export_notes() removed — UI concern
  - apple_date_to_iso import replaced with ios_backup_core.timestamps.apple_to_iso
  - _extract_pb_strings() replaced with import from ios_backup_core.protobuf
"""

import gzip
import plistlib
import sqlite3
from html.parser import HTMLParser

from ios_backup_core.db import connect_readonly, get_database
from ios_backup_core.protobuf import (
    ProtobufError,
    extract_protobuf_strings,
    field_text,
    parse_fields,
    sub_message,
)
from ios_backup_core.timestamps import apple_to_iso

# Core Data numbers attribute columns per entity, and the numbering moves between
# releases: iOS 15 keeps a note's creation date in ZCREATIONDATE3, while older
# releases use ZCREATIONDATE1 or ZCREATIONDATE. A note row only has the note
# entity's columns set, so COALESCE over whichever columns exist finds it.
_NOTE_CREATED_COLUMNS = ("ZCREATIONDATE3", "ZCREATIONDATE2", "ZCREATIONDATE1", "ZCREATIONDATE")
_NOTE_MODIFIED_COLUMNS = ("ZMODIFICATIONDATE1", "ZMODIFICATIONDATE")

# A shared note or folder keeps its iCloud share in ZSERVERSHAREDATA (ZSERVERSHARE
# on iOS 11): a CKShare written with NSKeyedArchiver, its system fields at the top
# of the archive. "Participants" lists everyone on the share, each a
# CKShareParticipant whose UserIdentity has the email address or phone number the
# share went to (LookupInfo), a name (NameComponents) and a CloudKit user record
# name (UserRecordID). The phone's owner is one of them, under CloudKit's
# placeholder for the current user or the record name on their Notes account
# (ZUSERRECORDNAME).
_SHARE_COLUMNS = ("ZSERVERSHAREDATA", "ZSERVERSHARE")
_CURRENT_USER_RECORD = "__defaultOwner__"  # CKCurrentUserDefaultName

# A deleted note sits in the account's Recently Deleted folder for 30 days before
# the iPhone removes it. That folder is a folder row with ZFOLDERTYPE 1 (0 is an
# ordinary folder, 3 a smart folder), and a note in it can also have
# ZMARKEDFORDELETION set, as both were in an iOS 27.3 backup. Either one makes
# the note deleted.
_TRASH_FOLDER_TYPE = 1


def share_participants(blob, own_record_names=frozenset()) -> list:
    """Who a CKShare archive says it's shared with, besides the phone's owner.

    Each is {"name", "email", "phone"}, any of them None. An archive that can't
    be read gives []: a note is still a note when its sharing can't be told.
    """
    if not isinstance(blob, (bytes, bytearray)) or not blob:
        return []
    try:
        archive = plistlib.loads(bytes(blob))
    except Exception:
        return []
    if not isinstance(archive, dict) or not isinstance(archive.get("$objects"), list):
        return []
    objects = archive["$objects"]
    top = archive.get("$top") if isinstance(archive.get("$top"), dict) else {}

    def deref(value):
        """One step through the archive: a UID is the object it points at; "$null" is None."""
        if isinstance(value, plistlib.UID):
            value = objects[value.data] if 0 <= value.data < len(objects) else None
        return None if value == "$null" else value

    def field(obj, key):
        return deref(obj.get(key)) if isinstance(obj, dict) else None

    def text(obj, key):
        value = field(obj, key)
        return value.strip() or None if isinstance(value, str) else None

    participants = deref(top.get("Participants"))
    if participants is None:
        participants = field(deref(top.get("root")), "Participants")
    found = []
    for item in field(participants, "NS.objects") or []:
        identity = field(deref(item), "UserIdentity")
        if not isinstance(identity, dict):
            continue
        record = text(field(identity, "UserRecordID"), "RecordName")
        if record and (record == _CURRENT_USER_RECORD or record in own_record_names):
            continue
        lookup = field(identity, "LookupInfo")
        names = field(identity, "NameComponents")
        # NSPersonNameComponents keeps its parts one level down, in NS.nameComponentsPrivate
        names = field(names, "NS.nameComponentsPrivate") or names
        name = " ".join(p for p in (text(names, "NS.givenName"), text(names, "NS.familyName")) if p) \
            or text(names, "NS.nickname")
        person = {"name": name or None, "email": text(lookup, "EmailAddress"), "phone": text(lookup, "PhoneNumber")}
        if any(person.values()) and person not in found:
            found.append(person)
    return found


def note_text(proto: bytes):
    """A note's text from its unzipped ZICNOTEDATA.ZDATA, or None when it can't be read.

    The text is field 2 of the Note (field 3) in the Document (field 2), as
    apple_cloud_notes_parser reads it. Searching every string in the data for
    the longest one finds the same text, except in a note whose text has fewer
    than three letters, such as one that's only a number: that search skips
    those as noise, and the note came back blank.
    """
    try:
        document = sub_message(parse_fields(proto), 2)
    except ProtobufError:
        return None
    note = sub_message(document, 3) if document else None
    return field_text(note, 2) if note else None


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
        """List all notes with content, newest change first.

        Notes can be in two files, and a backup can have both: NoteStore.sqlite
        (iOS 9 and later) and notes.sqlite (iOS 8 and earlier). iOS 9 leaves an
        account's notes in notes.sqlite until that account is upgraded, so a
        phone restored from backups since then can still carry, say, an old
        "On My iPhone" account there next to its iCloud notes in NoteStore. Both
        are listed. A note keeps its row number as its note_id; when both files
        have notes, those from notes.sqlite get "legacy_" in front so no two
        notes share an id.

        Each note has ``is_deleted``: True for one in the iPhone's Recently
        Deleted folder, which the iPhone's Notes app keeps apart from the others.
        """
        notes = self._notestore_notes(backup)
        legacy = []
        db_path = get_database(backup, self.NOTES_DB_PATH, domain="HomeDomain")
        if db_path:
            legacy = self._parse_legacy_notes(db_path, backup)
        if notes and legacy:
            for note in legacy:
                note["note_id"] = f"legacy_{note['note_id']}"
        notes = notes + legacy
        notes.sort(key=lambda n: n["modified"] or "", reverse=True)
        return {"notes": notes}

    def _notestore_notes(self, backup) -> list:
        """The notes in NoteStore.sqlite (iOS 9 and later), or [] when it has none."""
        for f in backup.list_files(path_like="%NoteStore.sqlite"):
            extracted = get_database(backup, f["path"], domain=f["domain"])
            if extracted:
                notes = self._parse_notestore(extracted, backup)
                if notes:
                    return notes
        # When the file list can't be searched, try where iOS 11 and later keep it
        notestore_path = get_database(backup, "NoteStore.sqlite", domain="AppDomainGroup-group.com.apple.notes")
        if notestore_path:
            return self._parse_notestore(notestore_path, backup)
        return []

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
                    "shared_with": [],
                    "is_deleted": False,
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
                    "shared_with": [],
                    "is_deleted": False,
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
            # A note's share is on its own row, or on its folder's when the folder is shared
            share_column = next((c for c in _SHARE_COLUMNS if c in columns), None)
            note_share = f"n.{share_column}" if share_column else "NULL"
            folder_share = f"f.{share_column}" if share_column and "ZFOLDER" in columns else "NULL"
            folder_join = "LEFT JOIN ZICCLOUDSYNCINGOBJECT f ON f.Z_PK = n.ZFOLDER" if "ZFOLDER" in columns else ""
            deleted_if = []
            if folder_join and "ZFOLDERTYPE" in columns:
                deleted_if.append(f"f.ZFOLDERTYPE = {_TRASH_FOLDER_TYPE}")
            if "ZMARKEDFORDELETION" in columns:
                deleted_if.append("n.ZMARKEDFORDELETION = 1")
            deleted = f"COALESCE({' OR '.join(deleted_if)}, 0)" if deleted_if else "0"
            own_records = frozenset()
            if share_column and "ZUSERRECORDNAME" in columns:
                own_records = frozenset(
                    r[0] for r in conn.execute(
                        "SELECT ZUSERRECORDNAME FROM ZICCLOUDSYNCINGOBJECT WHERE ZUSERRECORDNAME IS NOT NULL"
                    )
                )
            rows = conn.execute(f"""
                SELECT
                    n.Z_PK,
                    n.ZTITLE1 AS title,
                    {created} AS created,
                    {modified} AS modified,
                    n.ZSNIPPET AS snippet,
                    nd.ZDATA AS body_data,
                    {note_share} AS note_share,
                    {folder_share} AS folder_share,
                    {deleted} AS deleted
                FROM ZICCLOUDSYNCINGOBJECT n
                LEFT JOIN ZICNOTEDATA nd ON nd.ZNOTE = n.Z_PK
                {folder_join}
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
                            text = note_text(decompressed)
                            if text is None:
                                pb_strings = extract_protobuf_strings(decompressed)
                                text = max(pb_strings, key=len) if pb_strings else ""
                            if len(text) > len(body):
                                body = text.strip()
                        except Exception:
                            pass

                shared_with = share_participants(row["note_share"], own_records)
                for person in share_participants(row["folder_share"], own_records):
                    if person not in shared_with:
                        shared_with.append(person)

                notes.append({
                    "note_id": row["Z_PK"],
                    "title": row["title"] or "Untitled",
                    "body": body[:20000],
                    "created": apple_to_iso(row["created"]),
                    "modified": apple_to_iso(row["modified"]),
                    "shared_with": shared_with,
                    "is_deleted": bool(row["deleted"]),
                })

            conn.close()
        except Exception:
            pass

        return notes
