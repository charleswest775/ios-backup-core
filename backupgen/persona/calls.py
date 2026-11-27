"""Call history and voicemail in a persona pack.

- ``calls.txt`` (optional): the phone's call history, one call per line under a date:

      == 1905-03-17
      09:15 out mileva 4m12s
      09:40 in besso missed
      10:02 in +447700900555 missed
      18:20 out mileva 22m facetime-video

  Direction (``in``/``out``), a contact id or an E.164 number, then how long the call
  lasted or ``missed`` (nobody answered), then optionally ``facetime-audio`` or
  ``facetime-video`` (the default is ``phone``).
- ``voicemails.txt`` (optional): ``09:40 besso 38s: what they said``. Each voicemail
  also leaves a missed call, as on a real phone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from backupgen.persona.core import Contact, PersonaError, _check_who, _dated_lines, _seconds

CALL_SERVICES = ("phone", "facetime-audio", "facetime-video")
_CALL = re.compile(r"^(\d{1,2}):(\d{2})\s+(in|out)\s+(\+\d+|[A-Za-z0-9_-]+)\s+(missed|[\dhms]+)(?:\s+(\S+))?\s*$")
_VOICEMAIL = re.compile(r"^(\d{1,2}):(\d{2})\s+(\+\d+|[A-Za-z0-9_-]+)\s+([\dhms]+):\s(.+)$")


@dataclass
class Call:
    real_time: datetime
    outgoing: bool
    who: str  # contact id, or an E.164 number for callers who aren't contacts
    duration: int  # seconds; 0 when nobody answered
    service: str  # one of CALL_SERVICES
    where: str


@dataclass
class Voicemail:
    real_time: datetime
    who: str
    duration: int
    transcript: str
    where: str


def _parse_calls(path: Path, contacts: dict[str, Contact]) -> list[Call]:
    calls = []
    for day, line, where in _dated_lines(path):
        match = _CALL.match(line)
        if not match:
            raise PersonaError(f"{where}: can't parse {line!r}")
        hour, minute, direction, who, outcome, service = match.groups()
        service = service or "phone"
        if service not in CALL_SERVICES:
            raise PersonaError(f"{where}: unknown service {service!r}")
        _check_who(who, contacts, where)
        calls.append(Call(datetime(day.year, day.month, day.day, int(hour), int(minute)), direction == "out", who,
                          0 if outcome == "missed" else _seconds(outcome, where), service, where))
    return calls


def _parse_voicemails(path: Path, contacts: dict[str, Contact]) -> list[Voicemail]:
    voicemails = []
    for day, line, where in _dated_lines(path):
        match = _VOICEMAIL.match(line)
        if not match:
            raise PersonaError(f"{where}: can't parse {line!r}")
        hour, minute, who, duration, transcript = match.groups()
        _check_who(who, contacts, where)
        voicemails.append(Voicemail(datetime(day.year, day.month, day.day, int(hour), int(minute)), who,
                                    _seconds(duration, where), transcript, where))
    return voicemails


def load_calls(folder: Path, contacts: dict[str, Contact]) -> tuple[Optional[list[Call]], Optional[list[Voicemail]]]:
    """The calls and the voicemails, each None when the pack has no file for them."""
    calls = _parse_calls(folder / "calls.txt", contacts) if (folder / "calls.txt").exists() else None
    voicemails = _parse_voicemails(folder / "voicemails.txt", contacts) if (folder / "voicemails.txt").exists() else None
    return calls, voicemails
