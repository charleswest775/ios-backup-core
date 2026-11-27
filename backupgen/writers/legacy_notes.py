"""Notes in the old notes.sqlite (HomeDomain Library/Notes), the store of iOS 4 to 8.

iOS 9 moved notes to NoteStore.sqlite but left each account's notes in notes.sqlite until
the account was upgraded, so a backup can hold an "On My iPhone" account there beside
iCloud notes in NoteStore. A pack's ``store: legacy`` notes (persona/notes.py) go here,
in every backup made after they were written.

No notes.sqlite has been harvested (the rest of backupgen fills harvested schemas), so
the tables are hand-written from the published layouts of the Core Data store those
releases kept, with the columns readers touch, not every column iOS wrote:

- ZACCOUNT: one local account (ZTYPE 0) named "On My iPhone" with its default store, and,
  in a backup of iOS 9 or later, a ZDIDCHOOSETOMIGRATE column left NULL (iOS 9 added it
  for the upgrade to NoteStore; readers don't use it).
- ZSTORE: the account's one store, "Notes".
- ZNOTE: ZTITLE, ZSUMMARY (the first line after the title with text, NULL when there's
  none), ZGUID, ZINTEGERID, Core Data dates (seconds since 2001-01-01) and ZDELETEDFLAG,
  1 for a note deleted but not yet removed: it stays in the file, and readers leave it out.
- ZNOTEBODY: the note as HTML, its title the first line and each later line a <div>, a
  blank line <div><br></div>; ZOWNER is the note, ZNOTE.ZBODY the body.
- ZNOTEATTACHMENT: empty (no note here has an attachment).
- ZNOTECHANGE: change records, one per note edited after it was written and one per deleted
  note (ZCHANGETYPE 1 and 2), so readers meet rows there that aren't notes; the column set
  is approximate.
- ZNEXTID: the next ZINTEGERID to give out.
- Z_PRIMARYKEY and Z_METADATA, as every Core Data store has them.

legacy.py's iOS 6 to 10 backups write their notes.sqlite with ``notes_sqlite`` too.
"""

from __future__ import annotations

import html
import plistlib
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from backupgen.persona.core import Persona
from backupgen.writers import cocoa_seconds
from backupgen.writers.context import Step, StepContext

DOMAIN, PATH = "HomeDomain", "Library/Notes/notes.sqlite"
ACCOUNT_NAME, STORE_NAME = "On My iPhone", "Notes"
LEGACY_ID_PREFIX = "legacy_"  # the reader's note_id for these when NoteStore lists notes too

# Core Data numbers entities in name order.
_ENTITIES = ("Account", "NextId", "Note", "NoteAttachment", "NoteBody", "NoteChange", "Store")
_ENT = {name: number for number, name in enumerate(_ENTITIES, 1)}
_CHANGE_EDITED, _CHANGE_DELETED = 1, 2

_DDL = """
    CREATE TABLE ZACCOUNT (Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER, {migrate}ZTYPE INTEGER,
        ZDEFAULTSTORE INTEGER, ZACCOUNTIDENTIFIER VARCHAR, ZNAME VARCHAR);
    CREATE TABLE ZNEXTID (Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER, ZCOUNTER INTEGER);
    CREATE TABLE ZNOTE (Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER, ZCONTAINSCJK INTEGER,
        ZCONTENTTYPE INTEGER, ZDELETEDFLAG INTEGER, ZEXTERNALFLAGS INTEGER, ZEXTERNALSERVERINTID INTEGER,
        ZINTEGERID INTEGER, ZISBOOKKEEPINGENTRY INTEGER, ZBODY INTEGER, ZSTORE INTEGER,
        ZCREATIONDATE TIMESTAMP, ZMODIFICATIONDATE TIMESTAMP, ZAUTHOR VARCHAR, ZGUID VARCHAR,
        ZSERVERID VARCHAR, ZSUMMARY VARCHAR, ZTITLE VARCHAR, ZEXTERNALCONTENTREF BLOB,
        ZEXTERNALREPRESENTATION BLOB);
    CREATE TABLE ZNOTEATTACHMENT (Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER, ZNOTE INTEGER,
        ZCONTENTID VARCHAR, ZFILENAME VARCHAR, ZMIMETYPE VARCHAR);
    CREATE TABLE ZNOTEBODY (Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER, ZOWNER INTEGER,
        ZCONTENT VARCHAR, ZEXTERNALCONTENTREF VARCHAR, ZEXTERNALREPRESENTATION BLOB);
    CREATE TABLE ZNOTECHANGE (Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER, ZCHANGETYPE INTEGER,
        ZNOTEINTEGERID INTEGER, ZSTORE INTEGER, ZNOTESERVERID VARCHAR);
    CREATE TABLE ZSTORE (Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER, ZTYPE INTEGER, ZACCOUNT INTEGER,
        ZEXTERNALIDENTIFIER VARCHAR, ZNAME VARCHAR, ZSYNCANCHOR VARCHAR);
    CREATE TABLE Z_PRIMARYKEY (Z_ENT INTEGER PRIMARY KEY, Z_NAME VARCHAR, Z_SUPER INTEGER, Z_MAX INTEGER);
    CREATE TABLE Z_METADATA (Z_VERSION INTEGER PRIMARY KEY, Z_UUID VARCHAR(255), Z_PLIST BLOB);
    CREATE INDEX ZNOTE_ZSTORE_INDEX ON ZNOTE (ZSTORE);
    CREATE INDEX ZNOTE_ZBODY_INDEX ON ZNOTE (ZBODY);
    CREATE INDEX ZNOTEBODY_ZOWNER_INDEX ON ZNOTEBODY (ZOWNER);
    CREATE INDEX ZSTORE_ZACCOUNT_INDEX ON ZSTORE (ZACCOUNT);
"""


@dataclass
class LegacyNote:
    """One ZNOTE row and its body, in the order they're written (Z_PK 1, 2, ...)."""

    title: str
    body: str  # ZNOTEBODY.ZCONTENT: HTML, the title its first line
    summary: Optional[str]  # ZSUMMARY
    created: datetime  # aware
    modified: datetime
    deleted: bool  # ZDELETEDFLAG
    guid: str


@dataclass
class WrittenLegacyNote:
    """A listed legacy note as written, for the answer key."""

    pk: int  # its Z_PK, which the reader's note_id is made from
    title: str
    text: str
    created: datetime
    modified: datetime


def html_body(lines: Sequence[str]) -> str:
    """A note's lines as notes.sqlite keeps them: the title, then a <div> per line (<div><br></div> when blank)."""
    first, *rest = (html.escape(line, quote=False) for line in lines)
    return first + "".join(f"<div>{line}</div>" if line else "<div><br></div>" for line in rest)


def summary(lines: Sequence[str]) -> Optional[str]:
    """ZSUMMARY: the first line after the title with text in it, or NULL."""
    return next((line for line in lines[1:] if line.strip()), None)


def notes_sqlite(path: Path, notes: Sequence[LegacyNote], *, account_identifier: str, store_uuid: str,
                 offered_upgrade: bool = True) -> bytes:
    """Write notes.sqlite holding ``notes`` at ``path``; returns its bytes.

    ``offered_upgrade``: iOS 9 and later, whose ZACCOUNT has a ZDIDCHOOSETOMIGRATE column (left NULL).
    """
    conn = sqlite3.connect(path)
    try:
        # As sqlitegen.SchemaDatabase: no flush and journal file per CREATE (slow on Windows); same file.
        conn.execute("PRAGMA journal_mode = MEMORY")
        conn.execute("PRAGMA synchronous = OFF")
        conn.executescript(_DDL.format(migrate="ZDIDCHOOSETOMIGRATE INTEGER, " if offered_upgrade else ""))
        account = {"Z_PK": 1, "Z_ENT": _ENT["Account"], "Z_OPT": 1, "ZTYPE": 0, "ZDEFAULTSTORE": 1,
                   "ZACCOUNTIDENTIFIER": account_identifier, "ZNAME": ACCOUNT_NAME}
        _insert(conn, "ZACCOUNT", account)
        _insert(conn, "ZSTORE", {"Z_PK": 1, "Z_ENT": _ENT["Store"], "Z_OPT": 1, "ZTYPE": 0, "ZACCOUNT": 1,
                                 "ZNAME": STORE_NAME})
        changes = 0
        for pk, note in enumerate(notes, 1):
            _insert(conn, "ZNOTE", {
                "Z_PK": pk, "Z_ENT": _ENT["Note"], "Z_OPT": 1, "ZCONTAINSCJK": 0, "ZCONTENTTYPE": 0,
                "ZDELETEDFLAG": int(note.deleted), "ZINTEGERID": pk, "ZISBOOKKEEPINGENTRY": 0, "ZBODY": pk,
                "ZSTORE": 1, "ZCREATIONDATE": cocoa_seconds(note.created),
                "ZMODIFICATIONDATE": cocoa_seconds(note.modified), "ZGUID": note.guid, "ZSUMMARY": note.summary,
                "ZTITLE": note.title,
            })
            _insert(conn, "ZNOTEBODY", {"Z_PK": pk, "Z_ENT": _ENT["NoteBody"], "Z_OPT": 1, "ZOWNER": pk,
                                        "ZCONTENT": note.body})
            for change, waiting in ((_CHANGE_EDITED, note.modified > note.created), (_CHANGE_DELETED, note.deleted)):
                if waiting:
                    changes += 1
                    _insert(conn, "ZNOTECHANGE", {"Z_PK": changes, "Z_ENT": _ENT["NoteChange"], "Z_OPT": 1,
                                                  "ZCHANGETYPE": change, "ZNOTEINTEGERID": pk, "ZSTORE": 1})
        _insert(conn, "ZNEXTID", {"Z_PK": 1, "Z_ENT": _ENT["NextId"], "Z_OPT": 1, "ZCOUNTER": len(notes) + 1})
        maxima = {"Account": 1, "NextId": 1, "Note": len(notes), "NoteAttachment": 0, "NoteBody": len(notes),
                  "NoteChange": changes, "Store": 1}
        for name, number in _ENT.items():
            _insert(conn, "Z_PRIMARYKEY", {"Z_ENT": number, "Z_NAME": name, "Z_SUPER": 0, "Z_MAX": maxima[name]})
        metadata = plistlib.dumps({"NSStoreType": "SQLite", "NSStoreUUID": store_uuid}, fmt=plistlib.FMT_BINARY)
        _insert(conn, "Z_METADATA", {"Z_VERSION": 1, "Z_UUID": store_uuid, "Z_PLIST": metadata})
        conn.commit()
    finally:
        conn.close()
    return Path(path).read_bytes()


def _insert(conn: sqlite3.Connection, table: str, row: dict) -> None:
    conn.execute(f"INSERT INTO {table} ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})", tuple(row.values()))


def step(ctx: StepContext) -> Optional[list[WrittenLegacyNote]]:
    """notes.sqlite with the pack's store: legacy notes written by this backup. Returns those listed (not deleted)."""
    persona, snapshot = ctx.persona, ctx.snapshot
    rows: list[LegacyNote] = []
    written: list[WrittenLegacyNote] = []
    for note in sorted((n for n in persona.notes or [] if n.store == "legacy"), key=lambda n: n.created):
        created = persona.device_time(note.created)
        if created > snapshot.backup_date:
            continue
        modified = persona.device_time(note.modified)
        if modified > snapshot.backup_date:
            modified = created  # the edit came after this backup
        deleted = note.deleted is not None and persona.device_time(note.deleted) <= snapshot.backup_date
        lines = note.text.split("\n")
        rows.append(LegacyNote(lines[0], html_body(lines), summary(lines), created, modified, deleted,
                               str(uuid.UUID(bytes=ctx.rng.randbytes(16))).upper()))
        if not deleted:
            written.append(WrittenLegacyNote(len(rows), lines[0], note.text, created, modified))
    if not rows:
        return None
    store = notes_sqlite(ctx.tmp / "notes.sqlite", rows, account_identifier=str(uuid.UUID(bytes=ctx.rng.randbytes(16))).upper(),
                         store_uuid=str(uuid.UUID(bytes=ctx.rng.randbytes(16))).upper())
    ctx.writer.add_file(DOMAIN, PATH, store)
    return written


def note_id(pk: int, beside_note_store: bool):
    """The note_id a reader gives a notes.sqlite note: its Z_PK, with "legacy_" in front when NoteStore lists
    notes too (titled ones: a placeholder doesn't count), so no two notes share an id
    (ios_backup_core.extractors.notes.NoteExtractor.list_notes)."""
    return f"{LEGACY_ID_PREFIX}{pk}" if beside_note_store else pk


def answer_key(written: Optional[list[WrittenLegacyNote]], beside_note_store: bool) -> list[dict]:
    """The listed legacy notes as the answer key's "notes" entries (writers/notes.py puts them after NoteStore's)."""
    return [
        {
            "title": n.title, "text": n.text, "folder": None, "created": n.created.isoformat(),
            "modified": n.modified.isoformat(), "pinned": False, "shared_with": [], "deleted": False,
            "store": "legacy", "note_id": note_id(n.pk, beside_note_store),
        }
        for n in written or []
    ]


def sections(persona: Persona, written: Mapping[str, Any]) -> dict:
    return {}  # its notes are in the notes step's "notes" section


STEP = Step("legacy_notes", step, sections, part_of="notes")
