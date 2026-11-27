"""Call history in a persona pack.

- ``calls.txt`` (optional): the phone's call history, one call per line under a date:

      == 1905-03-17
      09:15 out mileva 4m12s
      09:40 in besso missed
      10:02 in +447700900555 missed
      18:20 out mileva 22m facetime-video

  Direction (``in``/``out``), a contact id or an E.164 number, then how long the call
  lasted or ``missed`` (nobody answered), then optionally ``facetime-audio`` or
  ``facetime-video`` (the default is ``phone``). Each voicemail (voicemail.py) also leaves
  a missed call, as on a real phone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from backupgen.persona.core import AreaData, Contact, Pack, PersonaError, _check_who, _dated_lines, _seconds

CALL_SERVICES = ("phone", "facetime-audio", "facetime-video")
_CALL = re.compile(r"^(\d{1,2}):(\d{2})\s+(in|out)\s+(\+\d+|[A-Za-z0-9_-]+)\s+(missed|[\dhms]+)(?:\s+(\S+))?\s*$")


@dataclass
class Call:
    real_time: datetime
    outgoing: bool
    who: str  # contact id, or an E.164 number for callers who aren't contacts
    duration: int  # seconds; 0 when nobody answered
    service: str  # one of CALL_SERVICES
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


def load(pack: Pack) -> AreaData:
    """Persona.calls: the calls, or None when the pack has no calls.txt."""
    path = pack.folder / "calls.txt"
    calls = _parse_calls(path, pack.contacts) if path.exists() else None
    return AreaData({"calls": calls}, records=calls or [])
