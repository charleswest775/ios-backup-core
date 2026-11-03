"""Notes: NoteStore.sqlite in AppDomainGroup-group.com.apple.notes, a Core Data store.

Rows follow what iOS 13–17 write:
- Accounts, folders and notes all live in ZICCLOUDSYNCINGOBJECT, told apart by Z_ENT.
  A note's title is ZTITLE1, a folder's name ZTITLE2, an account's name ZNAME.
- A note's creation date is ZCREATIONDATE1 on iOS 13–14 and ZCREATIONDATE3 from iOS 15;
  its modification date is ZMODIFICATIONDATE1. Dates are seconds since 2001-01-01.
- The text is in ZICNOTEDATA.ZDATA: a gzipped NoteStoreProto protobuf. Its note message
  holds the plain text (field 2), the "topotext" edit records Notes syncs with (3), a
  version vector keyed by a 16-byte replica UUID (4) and "attribute runs" that style the
  text (5). Runs count UTF-16 units, include each line's newline and carry an edit
  timestamp (13). Style 0 is the title, 101 a dashed list item, 103 a checklist item.
- ZSNIPPET is the first line after the title, as the notes list shows it.
"""

from __future__ import annotations

import gzip
import random
import uuid
from dataclasses import dataclass
from datetime import datetime

from backupgen.persona import Note, Persona, Snapshot
from backupgen.sqlitegen import SchemaDatabase
from backupgen.writers import CoreData, cocoa_seconds

_TITLE, _DASHED, _CHECKLIST = 0, 101, 103


@dataclass
class WrittenNote:
    """One note as written, for the answer key."""

    title: str
    text: str  # what the note holds: list markers become formatting, not characters
    folder: str
    created: datetime
    modified: datetime
    pinned: bool


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        byte, n = n & 0x7F, n >> 7
        out.append(byte | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _int_field(field: int, value: int) -> bytes:
    return _varint(field << 3) + _varint(value)


def _bytes_field(field: int, payload: bytes) -> bytes:
    return _varint((field << 3) | 2) + _varint(len(payload)) + payload


def parse_lines(note: Note) -> list[tuple[str, int | None, bool | None]]:
    """(text, paragraph style, checked) per line, with list markers taken off."""
    lines = []
    for i, line in enumerate(note.text.split("\n")):
        if i == 0:
            lines.append((line, _TITLE, None))
        elif line.startswith(("[ ] ", "[x] ", "[X] ")):
            lines.append((line[4:], _CHECKLIST, line[1] != " "))
        elif line.startswith("- "):
            lines.append((line[2:], _DASHED, None))
        else:
            lines.append((line, None, None))
    return lines


def _position(replica: int, clock: int) -> bytes:
    return _int_field(1, replica) + _int_field(2, clock)


def note_data(lines: list[tuple[str, int | None, bool | None]], edited: datetime, rng: random.Random) -> bytes:
    text = "\n".join(t for t, _, _ in lines)
    length = len(text.encode("utf-16-le")) // 2
    # One replica typed the whole text: a start marker, the inserted run, an end marker.
    edits = (
        _bytes_field(3, _bytes_field(1, _position(0, 0)) + _int_field(2, 0) + _bytes_field(3, _position(0, 0)) + _int_field(5, 1))
        + _bytes_field(3, _bytes_field(1, _position(1, 0)) + _int_field(2, length) + _bytes_field(3, _position(1, 0))
                       + _int_field(5, 2))
        + _bytes_field(3, _bytes_field(1, _position(0, 0xFFFFFFFF)) + _bytes_field(3, _position(0, 0xFFFFFFFF)))
    )
    version = _bytes_field(4, _bytes_field(1, _bytes_field(1, rng.randbytes(16)) + _bytes_field(2, _int_field(1, length))
                                           + _bytes_field(2, _int_field(1, 1))))
    stamp = int(edited.timestamp())
    runs = b""
    for i, (line, style, checked) in enumerate(lines):
        run_length = len((line + ("\n" if i < len(lines) - 1 else "")).encode("utf-16-le")) // 2
        if not run_length:
            continue
        paragraph = b""
        if style is not None:
            paragraph = _int_field(1, style)
            if style == _CHECKLIST:
                paragraph += _bytes_field(5, _bytes_field(1, rng.randbytes(16)) + _int_field(2, int(bool(checked))))
        runs += _bytes_field(5, _int_field(1, run_length) + (_bytes_field(2, paragraph) if paragraph else b"")
                             + _int_field(13, stamp))
    note = _bytes_field(2, text.encode("utf-8")) + edits + version + runs
    document = _int_field(1, 0) + _int_field(2, 0) + _bytes_field(3, note)
    return gzip.compress(_int_field(1, 0) + _bytes_field(2, document), mtime=0)


def _uuid(rng: random.Random) -> str:
    return str(uuid.UUID(bytes=rng.randbytes(16))).upper()


def populate(db: SchemaDatabase, persona: Persona, snapshot: Snapshot, rng: random.Random) -> list[WrittenNote]:
    core = CoreData(db)
    objects, data = db.table("ZICCLOUDSYNCINGOBJECT"), db.table("ZICNOTEDATA")
    created_column = "ZCREATIONDATE3" if objects.has("ZCREATIONDATE3") else "ZCREATIONDATE1"
    account = objects.insert({"Z_ENT": core.ent("ICAccount"), "Z_OPT": 1, "ZNAME": "iCloud", "ZACCOUNTTYPE": 1,
                              "ZIDENTIFIER": _uuid(rng), "ZMARKEDFORDELETION": 0})
    core.saw("ICCloudSyncingObject", account)
    folders: dict[str, int] = {}
    written = []
    for note in sorted(persona.notes or [], key=lambda n: n.created):
        created = persona.device_time(note.created)
        if created > snapshot.backup_date:
            continue
        modified = persona.device_time(note.modified)
        if modified > snapshot.backup_date:
            modified = created  # the edit came after this backup
        if note.folder not in folders:
            folders[note.folder] = objects.insert({
                "Z_ENT": core.ent("ICFolder"), "Z_OPT": 1, "ZTITLE2": note.folder, "ZFOLDERTYPE": 0,
                "ZIDENTIFIER": _uuid(rng), "ZMARKEDFORDELETION": 0, "ZOWNER": account,
            })
            core.saw("ICCloudSyncingObject", folders[note.folder])
        lines = parse_lines(note)
        title = lines[0][0]
        snippet = next((t for t, _, _ in lines[1:] if t.strip()), "")
        pk = objects.insert({
            "Z_ENT": core.ent("ICNote"), "Z_OPT": 1,
            "ZTITLE1": title, "ZSNIPPET": snippet,
            created_column: cocoa_seconds(created), "ZMODIFICATIONDATE1": cocoa_seconds(modified),
            "ZFOLDER": folders[note.folder], "ZISPINNED": int(note.pinned), "ZMARKEDFORDELETION": 0,
            "ZIDENTIFIER": _uuid(rng),
        })
        core.saw("ICCloudSyncingObject", pk)
        data_pk = data.insert({"Z_ENT": core.ent("ICNoteData"), "Z_OPT": 1, "ZNOTE": pk, "ZDATA": note_data(lines, modified, rng)})
        core.saw("ICNoteData", data_pk)
        db.conn.execute("UPDATE ZICCLOUDSYNCINGOBJECT SET ZNOTEDATA = ? WHERE Z_PK = ?", (data_pk, pk))
        written.append(WrittenNote(title, "\n".join(t for t, _, _ in lines), note.folder, created, modified, note.pinned))
    core.finish()
    return written
