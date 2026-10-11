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
  love, like, dislike, laugh, emphasize and question. ``19:46 mileva [photo:bern-1]``
  sends a photo from ``photos.json``. Lines starting with ``#`` are notes for reviewers.
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
- ``notes/*.txt`` (optional): one note per file. Headers, a blank line, then the text,
  whose first line is the title. ``- item`` lines become a dashed list and
  ``[ ] item`` / ``[x] item`` a checklist:

      folder: Trips
      created: 1905-03-02 09:12
      modified: 1905-03-10 18:40
      pinned: yes

      Packing list
      [x] Violin
      [ ] Patent drafts

- ``safari.txt`` (optional): ``09:15 https://example.org/page | Page title`` under
  ``== date`` headings, one line per visit.
- ``photos.json`` (optional): the camera roll, ``{"photos": [...]}``. Each photo has an
  ``id``, ``taken`` (local time, ``YYYY-MM-DDTHH:MM``), optional ``lat``/``lon``, ``place``,
  ``favorite``, ``trashed`` and ``albums`` (album titles). The pixels come either from
  ``url`` (an openly licensed original, checked against its ``sha1`` and cached; record
  ``licence``, ``credit`` and ``commons_page`` with it) or from ``placeholder``
  (``{"size": [640, 480], "color": "#88aacc"}``, drawn locally, for tests).

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
CALL_SERVICES = ("phone", "facetime-audio", "facetime-video")
_LINE = re.compile(r"^(\d{1,2}):(\d{2})\s+([A-Za-z0-9_-]+)(?::\s(.*)|\s+\+(\w+)|\s+\[photo:([A-Za-z0-9_-]+)\])\s*$")
_CALL = re.compile(r"^(\d{1,2}):(\d{2})\s+(in|out)\s+(\+\d+|[A-Za-z0-9_-]+)\s+(missed|[\dhms]+)(?:\s+(\S+))?\s*$")
_VOICEMAIL = re.compile(r"^(\d{1,2}):(\d{2})\s+(\+\d+|[A-Za-z0-9_-]+)\s+([\dhms]+):\s(.+)$")
_DURATION = re.compile(r"^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$")
_VISIT = re.compile(r"^(\d{1,2}):(\d{2})\s+(\S+)\s+\|\s+(.+)$")
_NOTE_HEADERS = ("folder", "created", "modified", "pinned")


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


@dataclass
class Note:
    id: str
    folder: str
    created: datetime  # real wall-clock times, like messages
    modified: datetime
    pinned: bool
    text: str  # as typed: the first line is the title, "- " and "[ ] " mark list items
    where: str

    @property
    def real_time(self) -> datetime:
        return self.created


@dataclass
class Visit:
    real_time: datetime
    url: str
    title: str
    where: str


@dataclass
class Photo:
    id: str
    real_time: datetime  # when it was taken, local wall-clock time
    lat: Optional[float]
    lon: Optional[float]
    place: str
    favorite: bool
    trashed: bool
    albums: list[str]
    url: str  # an openly licensed original, or "" for a placeholder
    sha1: str
    placeholder: Optional[tuple[int, int, str]]  # (width, height, "#rrggbb")
    where: str


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
    calls: Optional[list[Call]] = None  # None: the pack has no calls.txt, so the phone has no call history
    voicemails: Optional[list[Voicemail]] = None
    notes: Optional[list[Note]] = None
    visits: Optional[list[Visit]] = None
    photos: Optional[list[Photo]] = None

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


def _seconds(text: str, where: str) -> int:
    match = _DURATION.match(text)
    if not text or not match:
        raise PersonaError(f"{where}: can't read duration {text!r} (write it like 4m12s, 38s or 1h02m)")
    hours, minutes, seconds = (int(g or 0) for g in match.groups())
    return hours * 3600 + minutes * 60 + seconds


def _dated_lines(path: Path):
    """(day, line, where) for every line under a ``== date`` heading, skipping blanks and # notes."""
    day: Optional[date] = None
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line, where = raw.rstrip(), f"{path.name}:{number}"
        if not line or line.startswith("#"):
            continue
        if line.startswith("=="):
            day = date.fromisoformat(line[2:].strip())
        elif day is None:
            raise PersonaError(f"{where}: needs a == date heading first")
        else:
            yield day, line, where


def _check_who(who: str, contacts: dict[str, Contact], where: str) -> None:
    if not who.startswith("+") and who not in contacts:
        raise PersonaError(f"{where}: unknown contact {who!r}")


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


def _parse_note(path: Path) -> Note:
    head, _, body = path.read_text(encoding="utf-8").partition("\n\n")
    headers = {}
    for number, line in enumerate(head.splitlines(), 1):
        key, _, value = line.partition(":")
        if key.strip() not in _NOTE_HEADERS or not value.strip():
            raise PersonaError(f"{path.name}:{number}: expected one of {', '.join(_NOTE_HEADERS)}, got {line!r}")
        headers[key.strip()] = value.strip()
    if "created" not in headers:
        raise PersonaError(f"{path.name}: needs a created: header")
    text = body.strip("\n")
    if not text:
        raise PersonaError(f"{path.name}: the note is empty")
    created = datetime.fromisoformat(headers["created"])
    modified = datetime.fromisoformat(headers.get("modified", headers["created"]))
    if modified < created:
        raise PersonaError(f"{path.name}: modified before it was created")
    return Note(path.stem, headers.get("folder", "Notes"), created, modified,
                headers.get("pinned", "no").lower() in ("yes", "true"), text, path.name)


def _parse_visits(path: Path) -> list[Visit]:
    visits = []
    for day, line, where in _dated_lines(path):
        match = _VISIT.match(line)
        if not match or not match.group(3).startswith(("http://", "https://")):
            raise PersonaError(f"{where}: can't parse {line!r} (write HH:MM <url> | <title>)")
        hour, minute, url, title = match.groups()
        visits.append(Visit(datetime(day.year, day.month, day.day, int(hour), int(minute)), url, title.strip(), where))
    return visits


def _parse_photos(path: Path) -> list[Photo]:
    photos, seen = [], set()
    for n, raw in enumerate(json.loads(path.read_text(encoding="utf-8"))["photos"], 1):
        where = f"{path.name} photo {n} ({raw.get('id', '?')})"
        if raw["id"] in seen:
            raise PersonaError(f"{where}: duplicate id")
        seen.add(raw["id"])
        placeholder = raw.get("placeholder")
        if bool(placeholder) == bool(raw.get("url")):
            raise PersonaError(f"{where}: needs either url (with sha1) or placeholder")
        if raw.get("url") and not raw.get("sha1"):
            raise PersonaError(f"{where}: a url needs the original's sha1")
        if ("lat" in raw) != ("lon" in raw):
            raise PersonaError(f"{where}: lat and lon go together")
        photos.append(Photo(
            raw["id"], datetime.fromisoformat(raw["taken"]), raw.get("lat"), raw.get("lon"), raw.get("place", ""),
            bool(raw.get("favorite")), bool(raw.get("trashed")), list(raw.get("albums", [])),
            raw.get("url", ""), raw.get("sha1", ""),
            (*placeholder["size"], placeholder["color"]) if placeholder else None, where,
        ))
    return photos


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
    calls = _parse_calls(folder / "calls.txt", contacts) if (folder / "calls.txt").exists() else None
    voicemails = _parse_voicemails(folder / "voicemails.txt", contacts) if (folder / "voicemails.txt").exists() else None
    notes = [_parse_note(p) for p in sorted((folder / "notes").glob("*.txt"))] if (folder / "notes").is_dir() else None
    visits = _parse_visits(folder / "safari.txt") if (folder / "safari.txt").exists() else None
    photos = _parse_photos(folder / "photos.json") if (folder / "photos.json").exists() else None
    start, end = (date.fromisoformat(d) for d in raw["real_period"])
    records = [m for chat in chats for m in chat.messages] + (calls or []) + (voicemails or []) + (notes or []) + (visits or []) + (photos or [])
    photo_ids = {photo.id for photo in photos or []}
    for message in (m for chat in chats for m in chat.messages if m.photo):
        if message.photo not in photo_ids:
            raise PersonaError(f"{message.where}: no photo {message.photo!r} in photos.json")
    for record in records:
        if not start <= record.real_time.date() <= end:
            raise PersonaError(f"{record.where}: {record.real_time.date()} is outside the persona's period {start}–{end}")
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
        calls=calls,
        voicemails=voicemails,
        notes=notes,
        visits=visits,
        photos=photos,
    )


def list_personas(root: Path = PERSONAS_DIR) -> list[str]:
    return sorted(p.name for p in root.iterdir() if (p / "persona.json").exists())
