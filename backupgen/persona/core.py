"""What every part of a persona pack shares: the persona, its contacts and snapshots, and the loader.

Each kind of file in a pack belongs to an area with a module of its own beside this one (AREAS:
messages.py, calls.py, voicemail.py, notes.py, safari.py, photos.py, whatsapp.py, calendar.py,
voice_memos.py) that documents its format and parses it. Each has ``load(pack) -> AreaData``:
the Persona attributes it fills and its dated records. load_persona reads persona.json, runs
those loaders in turn and checks what spans them: photos sent after they were taken, every
record inside the persona's period.

A new area is a module with a ``load`` plus its name in AREAS and a line per attribute in
Persona; nothing else here changes.
"""

from __future__ import annotations

import importlib
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Optional
from zoneinfo import ZoneInfo

from backupgen import PERSONAS_DIR
from backupgen.container import Host

if TYPE_CHECKING:
    from backupgen.persona.calendar import CalendarDef, EventDef
    from backupgen.persona.calls import Call
    from backupgen.persona.messages import Chat
    from backupgen.persona.notes import Note
    from backupgen.persona.photos import Photo, PhotoPerson
    from backupgen.persona.safari import Visit
    from backupgen.persona.voice_memos import VoiceMemoDef
    from backupgen.persona.voicemail import Voicemail
    from backupgen.persona.whatsapp import WAChat, WAPerson

_DURATION = re.compile(r"^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$")

# The pack's areas, each a module in this package with ``load(pack) -> AreaData``, in the order
# load_persona runs them (which decides which error a broken pack shows first, and the order the
# period check goes through their records).
AREAS = ("messages", "calls", "voicemail", "notes", "safari", "photos", "whatsapp", "calendar", "voice_memos")


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
    options: dict = field(default_factory=dict)  # the snapshot's free-form "options" in persona.json


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
    chats: list[Chat]  # this and the attributes from calls on are filled by the areas' loaders (AREAS)
    snapshots: dict[str, Snapshot]
    timeline: list[dict]
    folder: Path
    avoid: list[dict] = field(default_factory=list)  # sensitive topics kept out of the conversations
    calls: Optional[list[Call]] = None  # None: the pack has no calls.txt, so the phone has no call history
    voicemails: Optional[list[Voicemail]] = None
    notes: Optional[list[Note]] = None
    visits: Optional[list[Visit]] = None
    photos: Optional[list[Photo]] = None
    photo_people: list[PhotoPerson] = field(default_factory=list)  # the People album; faces are on the photos
    whatsapp: Optional[list[WAChat]] = None  # None: the pack has no whatsapp/ folder, so the phone has no WhatsApp
    whatsapp_people: dict[str, WAPerson] = field(default_factory=dict)
    whatsapp_documents: dict[str, str] = field(default_factory=dict)  # file name -> text
    calendars: Optional[list[CalendarDef]] = None  # None: the pack has no calendar.json
    events: Optional[list[EventDef]] = None
    voice_memos: Optional[list[VoiceMemoDef]] = None  # None: the pack has no voice_memos.txt
    options: dict = field(default_factory=dict)  # persona.json's free-form "options"

    def device_time(self, real: datetime) -> datetime:
        """Move a real wall-clock time onto the device calendar and return it as an aware UTC time."""
        shifted = real.replace(year=real.year + self.shift_years, tzinfo=self.tz)
        return shifted.astimezone(timezone.utc)

    def real_time(self, device: datetime) -> datetime:
        local = device.astimezone(self.tz).replace(tzinfo=None)
        return local.replace(year=local.year - self.shift_years)


@dataclass
class Pack:
    """What an area's loader gets: the pack's folder and what persona.json says that areas use."""

    id: str  # the pack's folder name
    folder: Path
    contacts: dict[str, Contact]  # everyone but the owner, by id
    owner: Contact
    tz: ZoneInfo
    shift_years: int


@dataclass
class AreaData:
    """What an area's loader gives load_persona."""

    fields: dict  # Persona attribute -> value (None, or left out, when the pack has no file for it)
    records: list = field(default_factory=list)  # dated records (.real_time, .where), checked against the period
    sent_photos: list = field(default_factory=list)  # messages that send a camera-roll photo (.photo, .real_time, .where)


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


def _options(raw: dict, where: str) -> dict:
    """The free-form ``options`` of the pack or of a snapshot: any JSON object, ``{}`` when left out."""
    options = raw.get("options", {})
    if not isinstance(options, dict):
        raise PersonaError(f"{where}: options should be a JSON object ({{...}}), not {json.dumps(options)}")
    return options


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


def load_persona(persona_id: str, root: Path = PERSONAS_DIR) -> Persona:
    folder = root / persona_id
    raw = json.loads((folder / "persona.json").read_text(encoding="utf-8"))
    contacts = {c["id"]: _contact(c) for c in raw["contacts"]}
    owner = contacts.pop(raw["owner"])
    tz = ZoneInfo(raw["timezone"])
    options = _options(raw, "persona.json")
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
            options=_options(s, f"persona.json snapshot {s['id']}"),
        )
    pack = Pack(persona_id, folder, contacts, owner, tz, raw["shift_years"])
    fields: dict = {}
    records: list = []
    sent: list = []
    # The area modules import this one, so they're imported here rather than at the top.
    for name in AREAS:
        area = importlib.import_module(f"backupgen.persona.{name}").load(pack)
        fields.update(area.fields)
        records += area.records
        sent += area.sent_photos
    start, end = (date.fromisoformat(d) for d in raw["real_period"])
    taken = {photo.id: photo.real_time for photo in fields.get("photos") or []}
    for message in sent:
        if message.photo not in taken:
            raise PersonaError(f"{message.where}: no photo {message.photo!r} in photos.json")
        if message.real_time < taken[message.photo]:
            raise PersonaError(f"{message.where}: {message.photo} is sent before it was taken ({taken[message.photo]})")
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
        snapshots=snapshots,
        timeline=research.get("events", []),
        folder=folder,
        avoid=research.get("avoid", []),
        options=options,
        **fields,
    )


def list_personas(root: Path = PERSONAS_DIR) -> list[str]:
    return sorted(p.name for p in root.iterdir() if (p / "persona.json").exists())
