"""Voicemail in a persona pack.

- ``voicemails.txt`` (optional): one voicemail per line under a date,
  ``09:40 besso 38s: what they said``: a contact id or an E.164 number, how long it is,
  then its transcript. Each voicemail also leaves a missed call in call history, as on a
  real phone (writers/calls.py).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from backupgen.persona.core import AreaData, Contact, Pack, PersonaError, _check_who, _dated_lines, _seconds

_VOICEMAIL = re.compile(r"^(\d{1,2}):(\d{2})\s+(\+\d+|[A-Za-z0-9_-]+)\s+([\dhms]+):\s(.+)$")


@dataclass
class Voicemail:
    real_time: datetime
    who: str
    duration: int
    transcript: str
    where: str


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


def load(pack: Pack) -> AreaData:
    """Persona.voicemails: the voicemails, or None when the pack has no voicemails.txt."""
    path = pack.folder / "voicemails.txt"
    voicemails = _parse_voicemails(path, pack.contacts) if path.exists() else None
    return AreaData({"voicemails": voicemails}, records=voicemails or [])
