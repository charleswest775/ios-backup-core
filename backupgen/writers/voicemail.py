"""Voicemail: Library/Voicemail/voicemail.db plus one <ROWID>.amr per message.

voicemail.db is a plain (rollback-journal) SQLite database. Dates are Unix seconds,
``sender`` and ``callback_num`` hold the caller's number, ``receiver`` the owner's,
and ``flags`` bit 0 marks a message as listened to.

The audio is silent AMR-NB of the right length. Spoken audio needs a text-to-speech
voice whose output may be redistributed, which macOS's built-in voices may not.
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from backupgen.persona import Persona, Snapshot
from backupgen.sqlitegen import SchemaDatabase
from backupgen.writers.calls import address_of

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
