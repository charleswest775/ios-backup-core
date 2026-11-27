"""Voice Memos in a persona pack.

- ``voice_memos.txt`` (optional): Voice Memos recordings under ``== date`` headings, one per
  line: start time, length, title, then any of ``(favorite)``, ``(deleted)`` (in Recently
  Deleted), ``(icloud)`` (kept in iCloud only, so not in the backup) and ``(folder: Name)``.
  Indented ``>`` lines after a recording are its transcript, joined with single spaces (not
  for an ``(icloud)`` recording: the transcript lives in the audio file):

      == 2025-11-17
      21:10 19m40s DD presentation run-through (favorite) (folder: Work)
        > Okay, from the top. The river comes first.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from backupgen.persona.core import AreaData, Pack, PersonaError, _seconds

_MEMO = re.compile(r"^(\d{1,2}):(\d{2})\s+([\dhms]+)\s+(.+)$")
_MEMO_FLAG = re.compile(r"\s*\((favorite|deleted|icloud|folder:[^()]*)\)\s*$")


@dataclass
class VoiceMemoDef:
    real_time: datetime  # when recording started, local wall clock
    duration: int  # seconds
    title: str
    where: str
    favorite: bool = False
    deleted: bool = False
    icloud: bool = False  # kept in iCloud only: the row is there, the audio isn't
    folder: str = ""
    transcript: str = ""


def _parse_voice_memos(path: Path) -> list[VoiceMemoDef]:
    memos: list[VoiceMemoDef] = []
    day: Optional[date] = None
    last: Optional[VoiceMemoDef] = None
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line, where = raw.rstrip(), f"{path.name}:{number}"
        if not line or line.lstrip().startswith("#"):
            continue
        if raw[:1].isspace() and line.lstrip().startswith(">"):
            if last is None:
                raise PersonaError(f"{where}: a > transcript line belongs under a recording")
            if last.icloud:
                raise PersonaError(f"{where}: an (icloud) recording has no audio in the backup, so nowhere to keep a "
                                   "transcript (it lives in the audio file)")
            # Single spaces, as the audio file's transcript holds the words (writers/voice_memos.py).
            last.transcript = " ".join(f"{last.transcript} {line.lstrip()[1:]}".split())
            continue
        if line.startswith("=="):
            try:
                day = date.fromisoformat(line[2:].strip())
            except ValueError:
                raise PersonaError(f"{where}: can't read the date in {line!r}") from None
            last = None
            continue
        match = _MEMO.match(line)
        if day is None or not match:
            raise PersonaError(f"{where}: can't parse {line!r} (write HH:MM 4m12s Title (favorite))"
                               if day is not None else f"{where}: needs a == date heading first")
        hour, minute, length, rest = match.groups()
        flags: dict[str, str] = {}
        while (flag := _MEMO_FLAG.search(rest)) is not None:
            name, _, value = flag.group(1).partition(":")
            if name in flags:
                raise PersonaError(f"{where}: ({name}) twice")
            if name == "folder" and not value.strip():
                raise PersonaError(f"{where}: (folder: Name) needs the folder's name")
            flags[name] = value.strip()
            rest = rest[:flag.start()]
        title = rest.strip()
        if not title:
            raise PersonaError(f"{where}: the recording needs a title")
        duration = _seconds(length, where)
        if not duration:
            raise PersonaError(f"{where}: a recording lasts at least a second")
        when = datetime(day.year, day.month, day.day, int(hour), int(minute))
        if memos and when < memos[-1].real_time:
            raise PersonaError(f"{where}: recordings must be in time order")
        last = VoiceMemoDef(when, duration, title, where, favorite="favorite" in flags, deleted="deleted" in flags,
                            icloud="icloud" in flags, folder=flags.get("folder", ""))
        memos.append(last)
    return memos


def load(pack: Pack) -> AreaData:
    """Persona.voice_memos: the recordings, or None when the pack has no voice_memos.txt."""
    path = pack.folder / "voice_memos.txt"
    memos = _parse_voice_memos(path) if path.exists() else None
    return AreaData({"voice_memos": memos}, records=memos or [])
