"""Voicemail: Library/Voicemail/voicemail.db plus <ROWID>.amr and <ROWID>.transcript per message.

voicemail.db is a plain (rollback-journal) SQLite database. Dates are Unix seconds,
``sender`` and ``callback_num`` hold the caller's number, ``receiver`` the owner's,
and ``flags`` bit 0 marks a message as listened to.

The audio is silent AMR-NB of the right length. Spoken audio needs a text-to-speech
voice whose output may be redistributed, which macOS's built-in voices may not.

The transcript is an NSKeyedArchiver archive of a VMVoicemailTranscript, encoded as
VisualVoicemail.framework's ``encodeWithCoder:`` does it (iOS 18.2): the whole text,
an overall confidence and rating, and one VMVoicemailTranscriptSegment per word with
its own confidence, time and place in the text.
"""

from __future__ import annotations

import plistlib
import random
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from backupgen.persona.core import Persona, Snapshot
from backupgen.sqlitegen import SchemaDatabase
from backupgen.writers.calls import address_of, contact_name
from backupgen.writers.context import StepContext

_HEARD = 3  # what iOS stores once a voicemail has been played
_AMR_FRAME = bytes([0x3C]) + bytes(31)  # one 20 ms frame at 12.2 kbit/s, all zero: silence


@dataclass
class WrittenVoicemail:
    """One voicemail as written, for the answer key."""

    rowid: int
    date: datetime
    sender: str
    duration: int
    transcript: str


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


def populate(db: SchemaDatabase, persona: Persona, snapshot: Snapshot, rng: random.Random) -> list[WrittenVoicemail]:
    table = db.table("voicemail")
    owner = persona.owner
    receiver = "+" + "".join(ch for ch in owner.phones[0][1] if ch.isdigit()) if owner.phones else None
    written = []
    for n, v in enumerate(sorted(persona.voicemails or [], key=lambda v: v.real_time), start=1):
        # The caller hangs up on the missed call (written in its first 30 seconds), then leaves the message.
        when = persona.device_time(v.real_time) + timedelta(seconds=40)
        if when > snapshot.backup_date:
            continue
        sender = address_of(persona, v.who)
        rowid = table.insert({
            "remote_uid": 100 + n,
            "date": int(when.timestamp()),
            "token": "Complete",
            "sender": sender,
            "callback_num": sender,
            "duration": v.duration,
            "expiration": 0,
            "trashed_date": 0,
            "flags": _HEARD,
            "receiver": receiver,
            "uuid": str(uuid.UUID(bytes=rng.randbytes(16))).upper(),
        })
        written.append(WrittenVoicemail(rowid, when.replace(microsecond=0), sender, v.duration, v.transcript))
    return written


def step(ctx: StepContext) -> Optional[list[WrittenVoicemail]]:
    """voicemail.db, with each voicemail's .amr and its .transcript if it has one. Returns them."""
    if ctx.persona.voicemails is None:
        return None
    voicemails: list[WrittenVoicemail] = []
    database = ctx.build("voicemail.db",
                         lambda db: voicemails.extend(populate(db, ctx.persona, ctx.snapshot, ctx.rng)))
    ctx.writer.add_file("HomeDomain", "Library/Voicemail/voicemail.db", database)
    for v in voicemails:
        ctx.writer.add_file("HomeDomain", f"Library/Voicemail/{v.rowid}.amr", silent_amr(v.duration))
        if v.transcript:
            ctx.writer.add_file("HomeDomain", f"Library/Voicemail/{v.rowid}.transcript",
                                transcript_archive(v.transcript, v.duration))
    return voicemails


def answer_key(written: Optional[list[WrittenVoicemail]], cards: list) -> list[dict]:
    """The voicemails written, each named from ``cards`` (the contacts step's result)."""
    return [
        {
            "rowid": v.rowid, "date": v.date.isoformat(), "sender": v.sender,
            "contact": contact_name(cards, v.sender), "duration": v.duration, "transcript": v.transcript,
        }
        for v in written or []
    ]
