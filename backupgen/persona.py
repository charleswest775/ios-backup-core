"""Persona packs: who the phone belongs to, their contacts, their conversations, their timeline.

A pack is a folder under ``personas/``:

- ``persona.json``: owner, contacts, time shift, devices and backup snapshots.
- ``timeline.json``: the documented events the conversations are built on, with sources.
- ``chats/*.txt``: one conversation per file, written like a transcript:

      chat: mileva
      with: mileva
      service: iMessage

      == 1905-03-17
      19:42 me: Posted the light-quanta paper to the Annalen today.
      19:44 mileva: At last! Did you keep a copy?
      19:45 me +love

  ``+love`` is a tapback on the latest message from someone else. The reactions are
  love, like, dislike, laugh, emphasize and question. Lines starting with ``#`` are
  notes for reviewers.

Real dates are written as they happened. ``shift_years`` moves them onto the
device-era calendar (Einstein's 1905 plays out in 2021), so that backup formats
and iOS versions line up with real years. Wall-clock times are kept in the
persona's time zone, so summer dates pick up daylight saving the way a phone
would record them.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from backupgen import PERSONAS_DIR
from backupgen.container import Host

REACTIONS = {"love": 2000, "like": 2001, "dislike": 2002, "laugh": 2003, "emphasize": 2004, "question": 2005}
_LINE = re.compile(r"^(\d{1,2}):(\d{2})\s+([A-Za-z0-9_-]+)(?::\s(.*)|\s+\+(\w+))\s*$")


class PersonaError(ValueError):
    pass


@dataclass
class Contact:
    id: str
    first: str
    last: str = ""
    nickname: str = ""
    organization: str = ""
    department: str = ""
    job_title: str = ""
    note: str = ""
    phones: list[tuple[str, str]] = field(default_factory=list)  # (label, number as displayed)
    emails: list[tuple[str, str]] = field(default_factory=list)
    handle: str = ""  # Messages handle: E.164 phone or email
    added: Optional[date] = None  # real date the card was created

    @property
    def display_name(self) -> str:
        return " ".join(p for p in (self.first, self.last) if p) or self.organization


@dataclass
class Message:
    real_time: datetime  # naive wall-clock time in the persona's time zone, real (unshifted) year
    sender: str  # "me" or a contact id
    text: Optional[str]
    reaction: Optional[str]
    where: str  # file:line, for error messages


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


@dataclass
class Snapshot:
    id: str
    backup_date: datetime  # aware
    ios: str
    build: str
    product_type: str
    product_name: str
    schema: str  # harvested schema version used for databases
    container: str  # harvested container shape version
    host: Host


@dataclass
class Persona:
    id: str
    name: str
    summary: str
    licence: str
    tz: ZoneInfo
    shift_years: int
    real_start: date
    real_end: date
    owner: Contact
    device_name: str
    contacts: dict[str, Contact]
    chats: list[Chat]
    snapshots: dict[str, Snapshot]
    timeline: list[dict]
    folder: Path
    avoid: list[dict] = field(default_factory=list)  # sensitive topics kept out of the conversations

    def device_time(self, real: datetime) -> datetime:
        """Move a real wall-clock time onto the device calendar and return it as an aware UTC time."""
        shifted = real.replace(year=real.year + self.shift_years, tzinfo=self.tz)
        return shifted.astimezone(timezone.utc)

    def real_time(self, device: datetime) -> datetime:
        local = device.astimezone(self.tz).replace(tzinfo=None)
        return local.replace(year=local.year - self.shift_years)


def _contact(raw: dict) -> Contact:
    return Contact(
        id=raw["id"],
        first=raw.get("first", ""),
        last=raw.get("last", ""),
        nickname=raw.get("nickname", ""),
        organization=raw.get("organization", ""),
        department=raw.get("department", ""),
        job_title=raw.get("job_title", ""),
        note=raw.get("note", ""),
        phones=[tuple(p) for p in raw.get("phones", [])],
        emails=[tuple(e) for e in raw.get("emails", [])],
        handle=raw.get("handle") or _default_handle(raw),
        added=date.fromisoformat(raw["added"]) if raw.get("added") else None,
    )


def _default_handle(raw: dict) -> str:
    if raw.get("phones"):
        return "+" + re.sub(r"\D", "", raw["phones"][0][1])
    if raw.get("emails"):
        return raw["emails"][0][1].lower()
    return ""


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
        hour, minute, sender, text, reaction = match.groups()
        if reaction and reaction not in REACTIONS:
            raise PersonaError(f"{where}: unknown reaction +{reaction}")
        messages.append(Message(datetime(day.year, day.month, day.day, int(hour), int(minute)), sender, text, reaction, where))
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


def load_persona(persona_id: str, root: Path = PERSONAS_DIR) -> Persona:
    folder = root / persona_id
    raw = json.loads((folder / "persona.json").read_text(encoding="utf-8"))
    contacts = {c["id"]: _contact(c) for c in raw["contacts"]}
    owner = contacts.pop(raw["owner"])
    tz = ZoneInfo(raw["timezone"])
    snapshots = {}
    for s in raw["snapshots"]:
        snapshots[s["id"]] = Snapshot(
            id=s["id"],
            backup_date=datetime.fromisoformat(s["backup_date"]).replace(tzinfo=tz),
            ios=s["ios"],
            build=s["build"],
            product_type=s["product_type"],
            product_name=s["product_name"],
            schema=s.get("schema", s["ios"]),
            container=s.get("container", s.get("schema", s["ios"])),
            host=Host(**s.get("host", {})),
        )
    chats = [_parse_chat(p, contacts) for p in sorted((folder / "chats").glob("*.txt"))]
    ids = [c.id for c in chats]
    if len(ids) != len(set(ids)):
        raise PersonaError(f"{persona_id}: duplicate chat ids")
    start, end = (date.fromisoformat(d) for d in raw["real_period"])
    for chat in chats:
        for m in chat.messages:
            if not start <= m.real_time.date() <= end:
                raise PersonaError(f"{m.where}: {m.real_time.date()} is outside the persona's period {start}–{end}")
    timeline_path = folder / "timeline.json"
    research = json.loads(timeline_path.read_text(encoding="utf-8")) if timeline_path.exists() else {}
    return Persona(
        id=raw["id"],
        name=raw["name"],
        summary=raw.get("summary", ""),
        licence=raw.get("licence", ""),
        tz=tz,
        shift_years=raw["shift_years"],
        real_start=start,
        real_end=end,
        owner=owner,
        device_name=raw["device_name"],
        contacts=contacts,
        chats=chats,
        snapshots=snapshots,
        timeline=research.get("events", []),
        folder=folder,
        avoid=research.get("avoid", []),
    )


def list_personas(root: Path = PERSONAS_DIR) -> list[str]:
    return sorted(p.name for p in root.iterdir() if (p / "persona.json").exists())
