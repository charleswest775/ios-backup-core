"""Voicemail in a persona pack.

- ``voicemails.txt`` (optional): one voicemail per line under a date,
  ``09:40 besso 38s: what they said``: who left it, how long it is, then its
  transcript. Each voicemail also leaves a missed call in call history, as on a real
  phone (writers/calls.py), unless calls.txt has it already; a new one, being one more
  call in Calls' random numbers, re-draws the seconds and ids of the calls after it.

  Who: a contact id, an E.164 number, or ``unknown`` for a caller with no number (no
  caller ID: the voicemail's sender and callback number are empty, and so is its
  missed call's number). A line with no ``: text`` is a voicemail the phone never
  transcribed, so it has no .transcript file.

  Words in brackets after the length say how the backup holds it, several at once if
  need be (``10:05 +447700900123 14s (spam) (unheard): ...``). Without any it's a
  voicemail in the main list, played, with its audio, as before:

  - ``(unheard)``: never played.
  - ``(spam)``: in the Spam list (iOS 26 and later).
  - ``(deleted)``: in Deleted, deleted a little while after it came in, with a
    trashed_date. A snapshot taken before that moment has it in its list still.
  - ``(deleted-old)``: in Deleted, with 0x8 and no trashed_date.
  - ``(no-audio)``: its .amr isn't in the backup.
  - ``(removed)``: not a row of the voicemail table at all but of voicemail.db's
    ``deleted`` table, which readers don't list. Its .amr stays beside the
    database unless ``(no-audio)`` says otherwise.
  - ``(audio-only)``: only its .amr is left, under a ROWID no row has.
  - The per-line store (``Library/Voicemail/A-<UUID>/voicemail.db``, a store for
    one line, with audio of its own):
    ``(line)`` it's in that store too, under another ROWID and remote_uid;
    ``(line-only)`` it's only there; ``(line-audio)`` its main row has no .amr and the
    line's copy has one; ``(line-deleted)`` only the line's copy records its deletion;
    ``(line-no-plus)`` the line's copy has the number without its "+";
    ``(line-late)`` the line's copy is dated a second later. Each of these but
    ``(line-only)`` keeps its main row.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from backupgen.persona.core import AreaData, Contact, Pack, PersonaError, _check_who, _dated_lines, _seconds

_VOICEMAIL = re.compile(
    r"^(\d{1,2}):(\d{2})\s+(\+\d+|[A-Za-z0-9_-]+)\s+([\dhms]+)((?:\s+\([a-z-]+\))*)(?::\s(.+))?$"
)

UNKNOWN_CALLER = "unknown"  # who, for a caller with no number

# What each bracketed word may say (see the module docs).
VOICEMAIL_FLAGS = (
    "unheard", "spam", "deleted", "deleted-old", "no-audio", "removed", "audio-only",
    "line", "line-only", "line-audio", "line-deleted", "line-no-plus", "line-late",
)
LINE_FLAGS = frozenset({"line", "line-only", "line-audio", "line-deleted", "line-no-plus", "line-late"})
# Words that can't go together, as (these, those): a word of the first set with one of the second
# would contradict it or say nothing (an (audio-only) voicemail has no row to mark spam, a line-only
# one no main row for its copy to differ from).
_CLASHES = (
    ({"deleted"}, {"deleted-old"}),
    ({"removed"}, {"audio-only"}),
    ({"removed", "audio-only"}, LINE_FLAGS),
    ({"audio-only"}, {"no-audio", "unheard", "spam", "deleted", "deleted-old"}),
    ({"line-only"}, {"line-audio", "line-no-plus", "line-late"}),
    ({"line-audio"}, {"no-audio"}),
)


@dataclass
class Voicemail:
    real_time: datetime
    who: str  # contact id, E.164 number or UNKNOWN_CALLER
    duration: int
    transcript: str  # "" when the phone never transcribed it
    where: str
    flags: frozenset = field(default_factory=frozenset)  # VOICEMAIL_FLAGS


def _flags(text: str, where: str) -> frozenset:
    words = re.findall(r"\(([a-z-]+)\)", text)
    for word in words:
        if word not in VOICEMAIL_FLAGS:
            raise PersonaError(f"{where}: unknown voicemail flag ({word}) (one of: {', '.join(VOICEMAIL_FLAGS)})")
    if len(words) != len(set(words)):
        raise PersonaError(f"{where}: a voicemail flag is written twice")
    flags = frozenset(words)
    for these, those in _CLASHES:
        clash = sorted(flags & these) + sorted(flags & those - these)
        if flags & these and flags & those - these:
            raise PersonaError(f"{where}: voicemail flags {', '.join(f'({w})' for w in clash)} can't go together")
    return flags


def _parse_voicemails(path: Path, contacts: dict[str, Contact]) -> list[Voicemail]:
    voicemails = []
    for day, line, where in _dated_lines(path):
        match = _VOICEMAIL.match(line)
        if not match:
            raise PersonaError(f"{where}: can't parse {line!r}")
        hour, minute, who, duration, flags, transcript = match.groups()
        if who != UNKNOWN_CALLER:
            _check_who(who, contacts, where)
        voicemails.append(Voicemail(datetime(day.year, day.month, day.day, int(hour), int(minute)), who,
                                    _seconds(duration, where), transcript or "", where, _flags(flags, where)))
    return voicemails


def load(pack: Pack) -> AreaData:
    """Persona.voicemails: the voicemails, or None when the pack has no voicemails.txt."""
    # Call history (writers/calls.py) takes it for no number too, so it's refused with or without voicemail
    if UNKNOWN_CALLER in pack.contacts:
        raise PersonaError(f"persona.json: a contact can't have the id {UNKNOWN_CALLER!r}: voicemail and call "
                           f"history use it for a caller with no number")
    path = pack.folder / "voicemails.txt"
    voicemails = _parse_voicemails(path, pack.contacts) if path.exists() else None
    return AreaData({"voicemails": voicemails}, records=voicemails or [])
