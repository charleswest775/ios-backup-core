"""Messages in a persona pack.

- ``chats/*.txt``: one conversation per file, written like a transcript:

      chat: mileva
      with: mileva
      service: iMessage

      == 1905-03-17
      19:42 me: Posted the light-quanta paper to the Annalen today.
      19:44 mileva: At last! Did you keep a copy?
      19:45 me +love

  ``+love`` is a tapback on the latest message from someone else. The reactions are
  love, like, dislike, laugh, emphasize and question. ``19:46 mileva [photo:bern-1]``
  sends a photo from ``photos.json``. Lines starting with ``#`` are notes for reviewers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from backupgen.persona.core import AreaData, Contact, Pack, PersonaError

REACTIONS = {"love": 2000, "like": 2001, "dislike": 2002, "laugh": 2003, "emphasize": 2004, "question": 2005}
_LINE = re.compile(r"^(\d{1,2}):(\d{2})\s+([A-Za-z0-9_-]+)(?::\s(.*)|\s+\+(\w+)|\s+\[photo:([A-Za-z0-9_-]+)\])\s*$")


@dataclass
class Message:
    real_time: datetime  # naive wall-clock time in the persona's time zone, real (unshifted) year
    sender: str  # "me" or a contact id
    text: Optional[str]
    reaction: Optional[str]
    where: str  # file:line, for error messages
    photo: Optional[str] = None  # id of a photo in photos.json sent as an attachment


@dataclass
class Chat:
    id: str
    participants: list[str]
    service: str
    name: str
    messages: list[Message]

    @property
    def is_group(self) -> bool:
        return len(self.participants) > 1


def _parse_chat(path: Path, contacts: dict[str, Contact]) -> Chat:
    headers: dict[str, str] = {}
    messages: list[Message] = []
    day: Optional[date] = None
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.rstrip()
        where = f"{path.name}:{number}"
        if not line or line.startswith("#"):
            continue
        if line.startswith("=="):
            day = date.fromisoformat(line[2:].strip())
            continue
        header = re.match(r"^(chat|with|service|name):\s*(.*)$", line)
        if header and day is None:
            headers[header.group(1)] = header.group(2).strip()
            continue
        match = _LINE.match(line)
        if not match or day is None:
            raise PersonaError(f"{where}: can't parse {line!r}")
        hour, minute, sender, text, reaction, photo = match.groups()
        if reaction and reaction not in REACTIONS:
            raise PersonaError(f"{where}: unknown reaction +{reaction}")
        messages.append(Message(datetime(day.year, day.month, day.day, int(hour), int(minute)), sender, text, reaction,
                                where, photo))
    participants = [p.strip() for p in headers.get("with", "").split(",") if p.strip()]
    chat = Chat(headers.get("chat", path.stem), participants, headers.get("service", "iMessage"), headers.get("name", ""), messages)
    for p in participants:
        if p not in contacts:
            raise PersonaError(f"{path.name}: unknown participant {p!r}")
    previous = None
    for m in messages:
        if m.sender != "me" and m.sender not in participants:
            raise PersonaError(f"{m.where}: {m.sender!r} isn't in this chat")
        if previous and m.real_time < previous:
            raise PersonaError(f"{m.where}: messages must be in time order")
        previous = m.real_time
    return chat


def load(pack: Pack) -> AreaData:
    """Every conversation in chats/, in file name order (Persona.chats)."""
    chats = [_parse_chat(p, pack.contacts) for p in sorted((pack.folder / "chats").glob("*.txt"))]
    ids = [c.id for c in chats]
    if len(ids) != len(set(ids)):
        raise PersonaError(f"{pack.id}: duplicate chat ids")
    messages = [m for chat in chats for m in chat.messages]
    return AreaData({"chats": chats}, records=messages, sent_photos=[m for m in messages if m.photo])
