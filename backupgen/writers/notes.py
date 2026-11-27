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
- A deleted note moves to the account's Recently Deleted folder, a folder row with
  ZFOLDERTYPE 1 (0 for the others), and has ZMARKEDFORDELETION 1, as in an iOS 27.3
  backup. The iPhone removes it 30 days later.
- A note shared over iCloud keeps its CKShare in ZSERVERSHAREDATA, archived with
  NSKeyedArchiver: the share's fields sit at the top of the archive, and
  "Participants" holds a CKShareParticipant per person, the owner first. Each one's
  UserIdentity has LookupInfo (the EmailAddress or PhoneNumber the share went to),
  NameComponents and a UserRecordID. The owner's record name is the one on the
  iCloud account row (ZUSERRECORDNAME).
"""

from __future__ import annotations

import gzip
import hashlib
import plistlib
import random
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from backupgen.persona.core import Contact, Persona, Snapshot
from backupgen.persona.notes import Note
from backupgen.sqlitegen import SchemaDatabase
from backupgen.writers import CoreData, cocoa_seconds
from backupgen.writers.context import StepContext

_TITLE, _DASHED, _CHECKLIST = 0, 101, 103
RECENTLY_DELETED = "Recently Deleted"
_KEPT_DELETED = timedelta(days=30)


@dataclass
class WrittenNote:
    """One note as written, for the answer key."""

    title: str
    text: str  # what the note holds: list markers become formatting, not characters
    folder: str  # "Recently Deleted" for a deleted note
    created: datetime
    modified: datetime
    pinned: bool
    shared_with: list[dict]  # {"name", "email", "phone"} for each person besides the owner
    deleted: bool  # in Recently Deleted


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


def share_address(contact: Contact) -> dict:
    """Who a share went to, as CloudKit looks them up: their first email address, else first phone number."""
    if contact.emails:
        return {"name": contact.display_name, "email": contact.emails[0][1].lower(), "phone": None}
    return {"name": contact.display_name, "email": None, "phone": "+" + re.sub(r"\D", "", contact.phones[0][1])}


def user_record(persona: Persona, contact: Contact) -> str:
    """A CloudKit user record name, the same for a person on every share. Not drawn from
    the backup's random numbers, so sharing a note changes nothing else in the backup."""
    return "_" + hashlib.sha256(f"{persona.id}/{contact.id}".encode()).hexdigest()[:32]


def share_data(persona: Persona, people: list[Contact]) -> bytes:
    """A CKShare as NSKeyedArchiver writes it, with the owner and each person as participants."""
    objects: list = ["$null"]
    classes: dict[str, plistlib.UID] = {}

    def add(obj) -> plistlib.UID:
        objects.append(obj)
        return plistlib.UID(len(objects) - 1)

    def of(name: str, *parents: str) -> plistlib.UID:
        if name not in classes:
            classes[name] = add({"$classname": name, "$classes": [name, *parents, "NSObject"]})
        return classes[name]

    def string(value) -> plistlib.UID:
        return add(value) if value else plistlib.UID(0)

    def participant(contact: Contact, record: str, role: int) -> plistlib.UID:
        address = share_address(contact)
        names = add({"NS.givenName": string(contact.first), "NS.familyName": string(contact.last),
                     "$class": of("_NSPersonNameComponentsData")})
        lookup = add({"EmailAddress": string(address["email"]), "PhoneNumber": string(address["phone"]),
                      "$class": of("CKUserIdentityLookupInfo")})
        record_id = add({"RecordName": string(record), "$class": of("CKRecordID")})
        identity = add({"LookupInfo": lookup, "NameComponents": add({"NS.nameComponentsPrivate": names,
                                                                     "$class": of("NSPersonNameComponents")}),
                        "UserRecordID": record_id, "$class": of("CKUserIdentity")})
        return add({"UserIdentity": identity, "Role": role, "$class": of("CKShareParticipant")})

    # Role: 1 is the owner, 3 someone the share was sent to
    members = [participant(persona.owner, user_record(persona, persona.owner), 1)]
    members += [participant(c, user_record(persona, c), 3) for c in people]
    top = {
        "RecordType": string("cloudkit.share"),
        "Participants": add({"NS.objects": members, "$class": of("NSArray")}),
    }
    return plistlib.dumps({"$version": 100000, "$archiver": "NSKeyedArchiver", "$top": top, "$objects": objects},
                          fmt=plistlib.FMT_BINARY)


def _uuid(rng: random.Random) -> str:
    return str(uuid.UUID(bytes=rng.randbytes(16))).upper()


def populate(db: SchemaDatabase, persona: Persona, snapshot: Snapshot, rng: random.Random) -> list[WrittenNote]:
    core = CoreData(db)
    objects, data = db.table("ZICCLOUDSYNCINGOBJECT"), db.table("ZICNOTEDATA")
    created_column = "ZCREATIONDATE3" if objects.has("ZCREATIONDATE3") else "ZCREATIONDATE1"
    account = objects.insert({"Z_ENT": core.ent("ICAccount"), "Z_OPT": 1, "ZNAME": "iCloud", "ZACCOUNTTYPE": 1,
                              "ZIDENTIFIER": _uuid(rng), "ZMARKEDFORDELETION": 0,
                              "ZUSERRECORDNAME": user_record(persona, persona.owner)})
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
        deleted = note.deleted is not None and persona.device_time(note.deleted) <= snapshot.backup_date
        if deleted and snapshot.backup_date - persona.device_time(note.deleted) > _KEPT_DELETED:
            continue  # gone from Recently Deleted by this backup
        # A note that gets deleted draws from its own random numbers, so deleting
        # one changes nothing else in the backup.
        note_rng = random.Random(f"{persona.id}/notes/{note.id}") if note.deleted else rng
        folder = RECENTLY_DELETED if deleted else note.folder
        if folder not in folders:
            folders[folder] = objects.insert({
                "Z_ENT": core.ent("ICFolder"), "Z_OPT": 1, "ZTITLE2": folder, "ZFOLDERTYPE": int(deleted),
                "ZIDENTIFIER": _uuid(note_rng), "ZMARKEDFORDELETION": 0, "ZOWNER": account,
            })
            core.saw("ICCloudSyncingObject", folders[folder])
        lines = parse_lines(note)
        title = lines[0][0]
        snippet = next((t for t, _, _ in lines[1:] if t.strip()), "")
        people = [persona.contacts[c] for c in note.shared_with]
        row = {
            "Z_ENT": core.ent("ICNote"), "Z_OPT": 1,
            "ZTITLE1": title, "ZSNIPPET": snippet,
            created_column: cocoa_seconds(created), "ZMODIFICATIONDATE1": cocoa_seconds(modified),
            "ZFOLDER": folders[folder], "ZISPINNED": int(note.pinned), "ZMARKEDFORDELETION": int(deleted),
            "ZIDENTIFIER": _uuid(note_rng),
        }
        if people:
            row["ZSERVERSHAREDATA"] = share_data(persona, people)
        pk = objects.insert(row)
        core.saw("ICCloudSyncingObject", pk)
        data_pk = data.insert({"Z_ENT": core.ent("ICNoteData"), "Z_OPT": 1, "ZNOTE": pk, "ZDATA": note_data(lines, modified, note_rng)})
        core.saw("ICNoteData", data_pk)
        db.conn.execute("UPDATE ZICCLOUDSYNCINGOBJECT SET ZNOTEDATA = ? WHERE Z_PK = ?", (data_pk, pk))
        written.append(WrittenNote(title, "\n".join(t for t, _, _ in lines), folder, created, modified, note.pinned,
                                   [share_address(c) for c in people], deleted))
    core.finish()
    return written


def step(ctx: StepContext) -> Optional[list[WrittenNote]]:
    """NoteStore.sqlite in the Notes app group. Returns the notes written."""
    if ctx.persona.notes is None:
        return None
    notes: list[WrittenNote] = []
    store = ctx.build("NoteStore.sqlite", lambda db: notes.extend(populate(db, ctx.persona, ctx.snapshot, ctx.rng)))
    ctx.writer.add_file("AppDomainGroup-group.com.apple.notes", "NoteStore.sqlite", store)
    return notes


def answer_key(written: Optional[list[WrittenNote]]) -> list[dict]:
    return [
        {
            "title": n.title, "text": n.text, "folder": n.folder, "created": n.created.isoformat(),
            "modified": n.modified.isoformat(), "pinned": n.pinned, "shared_with": n.shared_with,
            "deleted": n.deleted,
        }
        for n in written or []
    ]
