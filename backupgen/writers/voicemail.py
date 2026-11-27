"""Voicemail: Library/Voicemail/voicemail.db plus <ROWID>.amr and <ROWID>.transcript per message.

voicemail.db is a plain (rollback-journal) SQLite database. Dates are Unix seconds,
``sender`` and ``callback_num`` hold the caller's number (both NULL for a caller with no
number), ``receiver`` the owner's. ``flags`` is built from the bits the reader knows
(ios_backup_core.extractors.voicemail): 0x1 played, 0x8 in Deleted, 0x80 in Spam. 0x2 is
set on every row here; what it means isn't known. Other bits aren't written, since
nothing here knows what they mean. ``trashed_date``, set when a voicemail is deleted, is
in Cocoa seconds (since 2001), unlike ``date``; a ``(deleted-old)`` voicemail has 0x8 and
no trashed_date.

Two more places the pack's flags (persona/voicemail.py) put voicemails:

- voicemail.db's ``deleted`` table (iOS 15 and later), with the voicemail table's columns:
  ``(removed)`` voicemails, which readers don't list. Each takes the ROWID it
  would have had in the voicemail table, so neither its row nor its .amr meets a listed
  voicemail's; an ``(audio-only)`` one takes a ROWID too but leaves only its .amr.
- The per-line store, ``Library/Voicemail/A-<UUID>/voicemail.db`` with its own .amr
  files, written when a voicemail in the snapshot says ``(line...)``. It has a smaller
  layout: the iOS 17.3 schema without the ``uuid`` column and without the ``deleted`` and
  ``map`` tables (line_store_schema). Its rows repeat main rows under other ROWIDs
  (AUTOINCREMENT ids needn't start at 1) and other remote_uids. The main store then has a
  ``map`` row and a ``label`` on each row the line holds; what iOS writes in those isn't
  documented, so the values (the line's UUID and a made-up label) only link them as the
  column names suggest, and no reader uses them.

The audio is silent AMR-NB of the right length. Spoken audio needs a text-to-speech
voice whose output may be redistributed, which macOS's built-in voices may not.

The transcript is an NSKeyedArchiver archive of a VMVoicemailTranscript, encoded as
VisualVoicemail.framework's ``encodeWithCoder:`` does it (iOS 18.2): the whole text,
an overall confidence and rating, and one VMVoicemailTranscriptSegment per word with
its own confidence, time and place in the text. It goes beside the copy a reader
picks (the first, main store first, whose audio is in the backup).
"""

from __future__ import annotations

import copy
import plistlib
import random
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Mapping, Optional, Union

from backupgen.persona.core import Persona, Snapshot
from backupgen.persona.voicemail import LINE_FLAGS, Voicemail
from backupgen.sqlitegen import SchemaDatabase, load_schema
from backupgen.writers import cocoa_seconds
from backupgen.writers.calls import address_of, contact_name
from backupgen.writers.context import Step, StepContext

_HEARD = 0x1
_ALWAYS = 0x2  # set on every row here; meaning unknown
_DELETED = 0x8
_SPAM = 0x80
_LINE_START = (12, 40)  # the range the per-line store's first ROWID comes from: AUTOINCREMENT ids needn't start at 1
_AMR_FRAME = bytes([0x3C]) + bytes(31)  # one 20 ms frame at 12.2 kbit/s, all zero: silence
_FOLDER = "Library/Voicemail"
_UNKNOWN_NAME = "Unknown"  # what a reader shows for a caller with no number, as Calls does


def row_flags(heard: bool, deleted: bool, spam: bool) -> int:
    """voicemail.db's flags from what's known: 3 for a played voicemail, as before."""
    return _ALWAYS | (_HEARD if heard else 0) | (_DELETED if deleted else 0) | (_SPAM if spam else 0)


@dataclass
class WrittenVoicemail:
    """One voicemail as a reader should list it, for the answer key."""

    rowid: Optional[int]  # its main-store ROWID; None when it's only in the per-line store
    id: Union[int, str]  # the id a reader gives it: the ROWID, or "A-<UUID>.<ROWID>" for the line's copy
    date: datetime  # of the copy a reader picks
    sender: Optional[str]  # likewise; None for a caller with no number
    duration: int
    transcript: str
    folder: str = "voicemail"  # "voicemail", "spam" or "deleted": the list it is in
    is_spam: bool = False
    is_deleted: bool = False
    is_read: bool = True
    audio_available: bool = True


@dataclass
class RemovedVoicemail:
    """A voicemail readers don't list: a row of the ``deleted`` table, or only its .amr."""

    table: Optional[str]  # "deleted", or None when only the .amr is left
    rowid: int
    date: datetime
    sender: Optional[str]
    duration: int
    audio: Optional[str]  # the .amr's path in the backup, or None


@dataclass
class Written:
    """What the voicemail step wrote. False when nothing is listed, so the answer key calls it absent."""

    voicemails: list[WrittenVoicemail] = field(default_factory=list)
    removed: list[RemovedVoicemail] = field(default_factory=list)
    line: Optional[str] = None  # the per-line store's folder name ("A-<UUID>"), if there is one

    def __bool__(self) -> bool:
        return bool(self.voicemails)


@dataclass
class _Plan:
    """What to write besides voicemail.db: files in the order they're added, and the per-line store's rows."""

    files: list[tuple[str, bytes]] = field(default_factory=list)
    line_rows: list[dict] = field(default_factory=list)
    line_files: list[tuple[str, bytes]] = field(default_factory=list)
    written: Written = field(default_factory=Written)


def silent_amr(seconds: int) -> bytes:
    return b"#!AMR\n" + _AMR_FRAME * (50 * seconds)


def transcript_archive(text: str, seconds: int) -> bytes:
    """A ``<ROWID>.transcript`` file holding ``text``, its words spread evenly over ``seconds``.

    Confidence is fixed rather than drawn, so adding a transcript doesn't move the
    random draws that the other voicemails come from.
    """
    objects: list = ["$null"]

    def add(value) -> plistlib.UID:
        objects.append(value)
        return plistlib.UID(len(objects) - 1)

    def classes(*names: str) -> plistlib.UID:
        return add({"$classname": names[0], "$classes": [*names, "NSObject"]})

    words = list(re.finditer(r"\S+", text))
    step = seconds / max(len(words), 1)
    root = add({})  # filled in below, so the root keeps UID 1 as NSKeyedArchiver writes it
    segments = []
    for n, word in enumerate(words):
        segments.append({
            "confidence": 0.9, "confidenceRating": 3, "duration": round(step * 0.8, 3),
            "substring": word.group(), "substringRange": (word.start(), len(word.group())),
            "timestamp": round(step * n, 3),
        })
    segment_class = classes("VMVoicemailTranscriptSegment") if segments else None
    value_class = classes("NSValue") if segments else None
    segment_uids = []
    for segment in segments:
        location, length = segment.pop("substringRange")
        substring = add(segment.pop("substring"))
        value = add({"NS.rangeval.location": location, "NS.rangeval.length": length, "NS.special": 4,
                     "$class": value_class})
        segment_uids.append(add({**segment, "substring": substring, "substringRange": value,
                                 "$class": segment_class}))
    array = add({"NS.objects": segment_uids, "$class": classes("NSArray")})
    objects[root.data] = {
        "confidence": 0.9 if words else 0.0,
        "confidenceRating": 3 if words else 0,
        "segments": array,
        "transcriptionString": add(text),
        "$class": classes("VMVoicemailTranscript"),
    }
    archive = {"$version": 100000, "$archiver": "NSKeyedArchiver", "$top": {"root": root}, "$objects": objects}
    return plistlib.dumps(archive, fmt=plistlib.FMT_BINARY, sort_keys=False)


def line_store_schema() -> dict:
    """The per-line store's layout: iOS 17.3's voicemail.db without the uuid column and the deleted and map tables."""
    schema = copy.deepcopy(load_schema("17.3", "voicemail.db"))
    dropped = {"deleted", "map"}
    schema["objects"] = [o for o in schema["objects"] if o["table"] not in dropped]
    for obj in schema["objects"]:
        if obj["type"] == "table" and obj["name"] == "voicemail":
            if obj["sql"].count(", uuid TEXT)") != 1:
                raise RuntimeError("iOS 17.3's voicemail table changed: can't take out its uuid column")
            obj["sql"] = obj["sql"].replace(", uuid TEXT)", ")")
    schema["columns"] = {table: [c for c in columns if (table, c["name"]) != ("voicemail", "uuid")]
                         for table, columns in schema["columns"].items() if table not in dropped}
    schema.pop("shape", None)
    return schema


def _deletion(when: datetime, snapshot: Snapshot, rng: random.Random) -> tuple[bool, int]:
    """Deleted a little while after it came in: (whether that was by the backup, its trashed_date)."""
    moment = when + timedelta(seconds=rng.randrange(10 * 60, 2 * 86400))
    return (True, int(cocoa_seconds(moment))) if moment <= snapshot.backup_date else (False, 0)


def _new_uuid(rng: random.Random) -> str:
    return str(uuid.UUID(bytes=rng.randbytes(16))).upper()


def populate(db: SchemaDatabase, persona: Persona, snapshot: Snapshot, rng: random.Random) -> _Plan:
    """Fill the main voicemail.db; returns its files and the per-line store's rows and files to write after it."""
    table, deleted_table, map_table = db.table("voicemail"), db.table("deleted"), db.table("map")
    owner = persona.owner
    receiver = "+" + "".join(ch for ch in owner.phones[0][1] if ch.isdigit()) if owner.phones else None
    plan = _Plan()
    dated: list[tuple[int, Voicemail, datetime]] = []
    for n, v in enumerate(sorted(persona.voicemails or [], key=lambda v: v.real_time), start=1):
        # The caller hangs up on the missed call (written in its first 30 seconds), then leaves the message.
        when = persona.device_time(v.real_time) + timedelta(seconds=40)
        if when <= snapshot.backup_date:
            dated.append((n, v, when))
    line = label = None
    line_rowid = 0
    if any(v.flags & LINE_FLAGS for _, v, _ in dated):
        line, label = "A-" + _new_uuid(rng), _new_uuid(rng)
        line_rowid = rng.randrange(*_LINE_START)

    rowid = 0
    for n, v, when in dated:
        flags = v.flags
        sender = address_of(persona, v.who)  # None: no caller ID
        heard, spam = "unheard" not in flags, "spam" in flags
        deleted, trashed = "deleted-old" in flags, 0
        if "deleted" in flags:
            deleted, trashed = _deletion(when, snapshot, rng)
        row = {
            "remote_uid": 100 + n,
            "date": int(when.timestamp()),
            "token": "Complete",
            "sender": sender,
            "callback_num": sender,
            "duration": v.duration,
            "expiration": 0,
            "trashed_date": trashed,
            "flags": row_flags(heard, deleted, spam),
            "receiver": receiver,
        }
        if "line-only" not in flags:
            rowid += 1  # every voicemail but the line's own takes a ROWID here, removed ones too

        if "audio-only" in flags:
            audio = f"{_FOLDER}/{rowid}.amr"
            plan.files.append((audio, silent_amr(v.duration)))
            plan.written.removed.append(RemovedVoicemail(None, rowid, when, sender, v.duration, audio))
            continue
        if "removed" in flags:
            if deleted_table is not None:
                deleted_table.insert({"ROWID": rowid, **row, "flags": row_flags(heard, True, spam),
                                      "uuid": _new_uuid(rng)})
            audio = None if "no-audio" in flags else f"{_FOLDER}/{rowid}.amr"
            if audio:
                plan.files.append((audio, silent_amr(v.duration)))
            plan.written.removed.append(RemovedVoicemail("deleted" if deleted_table is not None else None, rowid,
                                                         when, sender, v.duration, audio))
            continue

        # (store folder, ROWID, date, sender, whether its .amr is in the backup) of each copy, main store first
        copies = []
        if "line-only" not in flags:
            main_row = {"ROWID": rowid, **row, "uuid": _new_uuid(rng)}
            if flags & LINE_FLAGS:
                main_row["label"] = label
            table.insert(main_row)
            copies.append(("", rowid, when, sender, not flags & {"no-audio", "line-audio"}))
        line_deleted = False
        if flags & LINE_FLAGS:
            if line_rowid == rowid and "line-only" not in flags:
                line_rowid += 1  # another ROWID than its main row's, so no reader gets by mixing them up
            line_trashed = trashed
            if "line-deleted" in flags:
                line_deleted, line_trashed = _deletion(when, snapshot, rng)
            line_when = when + timedelta(seconds=1) if "line-late" in flags else when
            line_sender = sender[1:] if "line-no-plus" in flags and sender and sender.startswith("+") else sender
            plan.line_rows.append({"ROWID": line_rowid, **row, "remote_uid": 4000 + n,
                                   "date": int(line_when.timestamp()), "sender": line_sender,
                                   "callback_num": line_sender, "trashed_date": line_trashed,
                                   "flags": row_flags(heard, deleted or line_deleted, spam)})
            copies.append((line, line_rowid, line_when, line_sender, "no-audio" not in flags))
            line_rowid += rng.randrange(1, 3)

        for store, copy_rowid, _, _, has_audio in copies:
            if has_audio:
                files = plan.line_files if store else plan.files
                files.append((f"{_FOLDER}/{store}/{copy_rowid}.amr" if store else f"{_FOLDER}/{copy_rowid}.amr",
                              silent_amr(v.duration)))
        store, chosen_rowid, chosen_when, chosen_sender, has_audio = next((c for c in copies if c[4]), copies[0])
        if v.transcript:
            files = plan.line_files if store else plan.files
            files.append((f"{_FOLDER}/{store}/{chosen_rowid}.transcript" if store
                          else f"{_FOLDER}/{chosen_rowid}.transcript", transcript_archive(v.transcript, v.duration)))
        is_deleted = deleted or line_deleted
        plan.written.voicemails.append(WrittenVoicemail(
            rowid=None if "line-only" in flags else rowid,
            id=f"{store}.{chosen_rowid}" if store else chosen_rowid,
            date=chosen_when.replace(microsecond=0), sender=chosen_sender, duration=v.duration,
            transcript=v.transcript, folder="deleted" if is_deleted else "spam" if spam else "voicemail",
            is_spam=spam, is_deleted=is_deleted, is_read=heard, audio_available=has_audio,
        ))

    if line and map_table is not None:
        map_table.insert({"account": line[2:], "label": label})
    plan.written.line = line
    return plan


def step(ctx: StepContext) -> Optional[Written]:
    """voicemail.db with each voicemail's .amr and .transcript, then the per-line store if any. Returns them."""
    if ctx.persona.voicemails is None:
        return None
    plans: list[_Plan] = []
    database = ctx.build("voicemail.db", lambda db: plans.append(populate(db, ctx.persona, ctx.snapshot, ctx.rng)))
    plan = plans[0]
    ctx.writer.add_file("HomeDomain", f"{_FOLDER}/voicemail.db", database)
    for path, data in plan.files:
        ctx.writer.add_file("HomeDomain", path, data)
    if plan.written.line:
        line_db = SchemaDatabase(ctx.tmp / "line-voicemail.db", line_store_schema())
        table = line_db.table("voicemail")
        for row in plan.line_rows:
            table.insert(row)
        ctx.writer.add_file("HomeDomain", f"{_FOLDER}/{plan.written.line}/voicemail.db", line_db.finish().read_bytes())
        for path, data in plan.line_files:
            ctx.writer.add_file("HomeDomain", path, data)
    return plan.written


def answer_key(written: Optional[Written], cards: list) -> list[dict]:
    """Every voicemail a reader should list with include_deleted, each named from ``cards`` (the contacts step's result).

    ``id`` is the id a reader gives it, ``display_name`` the name it shows (the contact's, else the
    number, else "Unknown"), and ``audio_available`` whether a reader finds its audio.
    """
    out = []
    for v in written.voicemails if written else []:
        contact = contact_name(cards, v.sender)
        out.append({
            "rowid": v.rowid, "id": v.id, "date": v.date.isoformat(), "sender": v.sender, "contact": contact,
            "display_name": contact or v.sender or _UNKNOWN_NAME, "duration": v.duration, "transcript": v.transcript,
            "folder": v.folder, "is_spam": v.is_spam, "is_deleted": v.is_deleted, "is_read": v.is_read,
            "audio_available": v.audio_available,
        })
    return out


def removed_key(written: Optional[Written], cards: list) -> list[dict]:
    """The voicemails a reader shouldn't list: ``table`` "deleted" (voicemail.db's deleted table) or None (only
    the .amr is left), and ``audio_file``, the .amr's path in the backup or None."""
    return [
        {
            "table": r.table, "rowid": r.rowid, "date": r.date.replace(microsecond=0).isoformat(), "sender": r.sender,
            "contact": contact_name(cards, r.sender), "duration": r.duration, "audio_file": r.audio,
        }
        for r in (written.removed if written else [])
    ]


def sections(persona: Persona, written: Mapping[str, Any]) -> dict:
    cards = written.get("contacts", [])
    return {"voicemails": answer_key(written.get("voicemail"), cards),
            "removed_voicemails": removed_key(written.get("voicemail"), cards)}


STEP = Step("voicemail", step, sections)
