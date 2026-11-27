"""What every part of a persona pack shares: the persona, its snapshots, and the loader.

The cards (persona.json's "contacts" and contacts.json) are contacts.py's, beside this one:
load_persona reads them first, since the other areas name people by card id. Each other kind of
file in a pack belongs to an area with a module of its own (AREAS: messages.py, calls.py,
voicemail.py, notes.py, safari.py, photos.py, whatsapp.py, calendar.py, voice_memos.py) that
documents its format and parses it. Each has ``load(pack) -> AreaData``: the Persona attributes
it fills and its dated records. load_persona reads persona.json and the cards, runs those
loaders in turn and checks what spans them: photos sent after they were taken, every record
inside the persona's period.

A new area is a module with a ``load`` plus its name in AREAS and a line per attribute in
Persona; nothing else here changes.
"""

from __future__ import annotations

import dataclasses
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
from backupgen.persona.contacts import Contact, load_cards

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
    options: dict = field(default_factory=dict)  # the snapshot's "options": {data type: {key: value}}


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
    contacts: dict[str, Contact]  # every card but the owner's, by id (contacts.py)
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
    options: dict = field(default_factory=dict)  # persona.json's "options": {data type: {key: value}}

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


def _options(raw: dict, where: str) -> dict:
    """The ``options`` of the pack or of a snapshot: ``{data type: {key: value}}``, ``{}`` when left out.

    Only the shape is checked here; which data types and keys exist is up to the writers (each
    Step's options), and generate.check_options refuses the rest.
    """
    options = raw.get("options", {})
    if not isinstance(options, dict):
        raise PersonaError(f"{where}: options should be a JSON object ({{...}}), not {json.dumps(options)}")
    for kind, values in options.items():
        if not isinstance(values, dict):
            raise PersonaError(f"{where}: options.{kind} should be a JSON object of {kind}'s settings "
                               f"({{\"key\": value}}), not {json.dumps(values)}")
    return options


def _read_json(path: Path) -> object:
    """A pack's JSON file, refusing a key that appears twice in one object.

    ``json`` would keep the last one and drop the first without a word, so a second "options"
    added in another place (which merges cleanly in git), or a field written twice, would quietly
    undo the first.
    """
    def no_duplicates(pairs: list) -> dict:
        obj: dict = {}
        for key, value in pairs:
            if key in obj:
                raise PersonaError(f"{path.name}: the key {key!r} appears twice in one object "
                                   f"(JSON would keep only the last)")
            obj[key] = value
        return obj

    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=no_duplicates)


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
    raw = _read_json(folder / "persona.json")
    contacts = load_cards(folder, raw)
    if raw["owner"] not in contacts:
        raise PersonaError(f"persona.json: the owner {raw['owner']!r} has no card "
                           f"(persona.json's or contacts.json's contacts)")
    owner = contacts.pop(raw["owner"])
    tz = ZoneInfo(raw["timezone"])
    options = _options(raw, "persona.json")
    snapshots = {}
    for s in raw["snapshots"]:
        if s["id"] in snapshots:
            raise PersonaError(f"persona.json snapshot {s['id']}: duplicate id")
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
    filled_by: dict[str, str] = {}  # Persona attribute -> the area module that filled it
    attributes = {f.name for f in dataclasses.fields(Persona)}
    records: list = []
    sent: list = []
    # The area modules import this one, so they're imported here rather than at the top.
    for name in AREAS:
        area = importlib.import_module(f"backupgen.persona.{name}").load(pack)
        where = f"persona/{name}.py"
        unknown = sorted(area.fields.keys() - attributes)
        if unknown:
            raise PersonaError(f"{where}: Persona has no attribute {', '.join(unknown)} "
                               f"(add a line for each to Persona in core.py)")
        clash = sorted(area.fields.keys() & filled_by.keys())
        if clash:
            raise PersonaError(f"{where}: {', '.join(clash)} already filled by {filled_by[clash[0]]}")
        filled_by.update(dict.fromkeys(area.fields, where))
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
    research = _read_json(timeline_path) if timeline_path.exists() else {}
    own = dict(
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
    )
    clash = sorted(fields.keys() & own.keys())
    if clash:
        raise PersonaError(f"{filled_by[clash[0]]}: {', '.join(clash)} already filled by load_persona "
                           f"(persona.json or timeline.json)")
    return Persona(**own, **fields)


def list_personas(root: Path = PERSONAS_DIR) -> list[str]:
    return sorted(p.name for p in root.iterdir() if (p / "persona.json").exists())
