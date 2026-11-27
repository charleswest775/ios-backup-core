"""Safari history in a persona pack.

- ``safari.txt`` (optional): ``09:15 https://example.org/page | Page title`` under
  ``== date`` headings, one line per visit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from backupgen.persona.core import PersonaError, _dated_lines

_VISIT = re.compile(r"^(\d{1,2}):(\d{2})\s+(\S+)\s+\|\s+(.+)$")


@dataclass
class Visit:
    real_time: datetime
    url: str
    title: str
    where: str


def _parse_visits(path: Path) -> list[Visit]:
    visits = []
    for day, line, where in _dated_lines(path):
        match = _VISIT.match(line)
        if not match or not match.group(3).startswith(("http://", "https://")):
            raise PersonaError(f"{where}: can't parse {line!r} (write HH:MM <url> | <title>)")
        hour, minute, url, title = match.groups()
        visits.append(Visit(datetime(day.year, day.month, day.day, int(hour), int(minute)), url, title.strip(), where))
    return visits


def load_visits(folder: Path) -> Optional[list[Visit]]:
    """The visits, or None when the pack has no safari.txt."""
    return _parse_visits(folder / "safari.txt") if (folder / "safari.txt").exists() else None
