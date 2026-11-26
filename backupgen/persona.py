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
      shared: besso, mileva

      Packing list
      [x] Violin
      [ ] Patent drafts

  ``shared:`` lists the contacts the note is shared with over iCloud, by id; it's
  shared with each by their first email address, else their first phone number.
  ``deleted: 1905-04-01 08:00`` deletes the note then: a backup in the 30 days after
  has it in Recently Deleted, a later one doesn't have it at all.
- ``safari.txt`` (optional): ``09:15 https://example.org/page | Page title`` under
  ``== date`` headings, one line per visit.
- ``photos.json`` (optional): the camera roll, ``{"photos": [...]}``. Each photo has an
  ``id``, ``taken`` (local time, ``YYYY-MM-DDTHH:MM``), optional ``lat``/``lon``, ``place``,
  ``favorite``, ``trashed`` and ``albums`` (album titles). The pixels come either from
  ``url`` (an openly licensed original, checked against its ``sha1`` and cached; record
  ``licence``, ``credit`` and ``commons_page`` with it; ``thumb_url``/``thumb_sha1`` name a
  smaller rendition to fetch first) or from ``placeholder``
  (``{"size": [640, 480], "color": "#88aacc"}``, drawn locally, for tests).
  Optional ``people`` are the groups the Photos app made of the faces it found (its People
  album): each has an ``id``, optional ``name`` (else unnamed, as most groups are; a
  ``contact`` id links the group to that card and names it after it unless ``name`` says
  otherwise), ``merged_into`` (another person's id: a group the user merged into theirs),
  ``pet`` (``dog`` or ``cat``) and ``key`` (the photo id of the face Photos shows for them;
  else their first). A photo's ``faces`` list
  ``{"person": id, "x": 0.4, "y": 0.45, "size": 0.18}``: the face box's centre and size as
  fractions of the photo, plus ``"hidden": true`` for a face the user hid from People.
- ``whatsapp/*.txt`` (optional): WhatsApp chats, written like ``chats/*.txt``:

      chat: uo-friends
      type: group               individual (the default), group, broadcast or status
      with: hana, claire
      name: MLA '18             groups and broadcast lists
      created_by: hana          groups: who made it (default me)
      addressing: lid           groups: members by LID, as newer groups are (WhatsApp 2.23 on)

      == 2024-07-08
      19:00 notice: encrypted   "Messages and calls are end-to-end encrypted."
      19:00 notice: created     groups: the group was made (by created_by)
      19:00 notice: added me    groups: someone was added ("added hana" for others)
      19:02 hana: Hello
      19:03 me > hana: A reply to Hana's latest message
      19:03 claire +❤️          a reaction (any emoji) on the latest message from someone else
      19:04 me [photo:rainier-01] An optional caption
      19:04 hana [photo:missing] A photo WhatsApp no longer kept: only its thumbnail is left
      19:05 hana [voice:12s]
      19:06 me [document:Plan.pdf]          its text comes from whatsapp/documents/Plan.pdf.txt
      19:07 me [location:45.52,-122.68 Place name]
      19:08 me [live-location:45.52,-122.68]
      19:09 hana [call:video 4m12s]         voice or video; a length, missed (incoming) or unanswered (outgoing)
      19:10 hana [deleted]                  deleted for everyone
      19:11* hana: Starred                  a * after the time stars the message

  ``whatsapp/people.txt`` lists what people call themselves on WhatsApp, and who's on it
  without being a contact: ``lucia - Lucía 🌵`` (a contact: the number is their first phone)
  or ``dani +1 760 555 0150 Dani Reyes`` (not in the address book: id, number, name).
- ``calendar.json`` (optional): the Calendar app, ``{"calendars": [...], "events": [...]}``.
  A calendar has an ``id``, ``title``, ``color`` (``#RRGGBB``) and ``account``: ``icloud``,
  ``local`` (On My iPhone), ``subscribed``, ``holidays`` (Apple's holiday subscription, at most
  one) or ``suggestions`` (Found in Mail, at most one). An event has:

      {"id": "dd-presentation", "calendar": "work", "title": "Cottonwood DD presentation",
       "start": "2025-11-18T14:00", "end": "2025-11-18T15:30", "created": "2025-10-28T09:10",
       "location": "Riverbend Parks Foundation", "address": "...", "lat": 45.5, "lon": -122.6,
       "notes": "...", "url": "https://...", "organizer": "owen", "attendees": ["me", "priya"],
       "statuses": {"me": "accepted", "priya": "tentative"}, "alerts": [30]}

  ``start``/``end`` are wall-clock times in the persona's time zone, or in ``time_zone`` (an
  IANA name) when given; ``end_time_zone`` gives the end its own zone (a flight). A date-only
  ``start`` makes an all-day event whose ``end`` is its last day, inclusive (the same day when
  left out). ``"floating": true`` makes a timed event that keeps its wall-clock time in every
  zone. ``created`` (local) is checked against the persona's period and the backup date, so
  events may start after the backup. Meetings (in ``icloud`` calendars) have an ``organizer``,
  ``"me"`` or a contact id; ``attendees`` (contact ids, or ``"me"`` when someone else
  organises) need email addresses, and the organiser is added as the chair; ``statuses``
  answer for attendees: ``accepted``,
  ``declined``, ``tentative`` or ``needs_action`` (the default). ``alerts`` are minutes before
  the start (negative: after it, as in "9 AM on the day" of an all-day event, -540).
  ``repeat`` is ``{"frequency": "daily|weekly|monthly|yearly", "interval": 2, "count": 10,
  "until": "2026-06-30", "by_day": ["TU"] or ["4TH"], "by_month": [11]}`` (count and until
  are alternatives; leave both out for a series without end). ``skip`` lists occurrences
  deleted from a series (dates, or local times for a timed series) and ``moved`` lists
  occurrences given a new time: ``{"from": "2025-11-18", "start": "2025-11-19T19:00",
  "end": "2025-11-19T21:00"}``. ``status`` is ``confirmed``, ``tentative`` or ``cancelled``;
  ``"availability": "free"`` shows the time as free. Repeating events need ``shift_years`` 0
  (or a multiple of 400): a shift by whole years moves weekdays and 29 February.
- ``voice_memos.txt`` (optional): Voice Memos recordings under ``== date`` headings, one per
  line: start time, length, title, then any of ``(favorite)``, ``(deleted)`` (in Recently
  Deleted), ``(icloud)`` (kept in iCloud only, so not in the backup) and ``(folder: Name)``.
  Indented ``>`` lines after a recording are its transcript, joined with single spaces (not
  for an ``(icloud)`` recording: the transcript lives in the audio file):

      == 2025-11-17
      21:10 19m40s DD presentation run-through (favorite) (folder: Work)
        > Okay, from the top. The river comes first.

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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from backupgen import PERSONAS_DIR
from backupgen.container import Host

REACTIONS = {"love": 2000, "like": 2001, "dislike": 2002, "laugh": 2003, "emphasize": 2004, "question": 2005}
CALL_SERVICES = ("phone", "facetime-audio", "facetime-video")
_LINE = re.compile(r"^(\d{1,2}):(\d{2})\s+([A-Za-z0-9_-]+)(?::\s(.*)|\s+\+(\w+)|\s+\[photo:([A-Za-z0-9_-]+)\])\s*$")
_CALL = re.compile(r"^(\d{1,2}):(\d{2})\s+(in|out)\s+(\+\d+|[A-Za-z0-9_-]+)\s+(missed|[\dhms]+)(?:\s+(\S+))?\s*$")
_VOICEMAIL = re.compile(r"^(\d{1,2}):(\d{2})\s+(\+\d+|[A-Za-z0-9_-]+)\s+([\dhms]+):\s(.+)$")
_DURATION = re.compile(r"^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$")
_VISIT = re.compile(r"^(\d{1,2}):(\d{2})\s+(\S+)\s+\|\s+(.+)$")
_NOTE_HEADERS = ("folder", "created", "modified", "pinned", "shared", "deleted")
_WA_LINE = re.compile(
    r"^(\d{1,2}):(\d{2})(\*)?\s+([A-Za-z0-9_-]+)(?:\s+>\s+([A-Za-z0-9_-]+))?"
    r"(?::\s(.*)|\s+\+(\S+)|\s+\[([a-z-]+)(?::([^\]]*))?\](?:\s+(.*))?)\s*$"
)
_WA_HEADERS = ("chat", "type", "with", "name", "created_by", "addressing")
_WA_TYPES = ("individual", "group", "broadcast", "status")
_WA_PERSON = re.compile(r"^([A-Za-z0-9_-]+)\s+(-|\+\d[\d ]*\d)\s+([^\d\s].*)$")
WA_MISSING_PHOTO = "missing"
_WA_ANSWERABLE = ("text", "photo", "voice", "document", "location", "live_location")  # what replies and reactions point at
CALENDAR_ACCOUNTS = ("icloud", "local", "subscribed", "holidays", "suggestions")
FREQUENCIES = ("daily", "weekly", "monthly", "yearly")
WEEKDAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
RSVPS = ("accepted", "declined", "tentative", "needs_action")
EVENT_STATUSES = ("confirmed", "tentative", "cancelled")
_EVENT_KEYS = {"id", "calendar", "title", "start", "end", "created", "time_zone", "end_time_zone", "floating", "location",
               "address", "lat", "lon", "notes", "url", "organizer", "attendees", "statuses", "alerts", "repeat", "skip",
               "moved", "status", "availability"}
_REPEAT_KEYS = {"frequency", "interval", "count", "until", "by_day", "by_month"}
_BY_DAY = re.compile(r"^([+-]?\d{1,2})?(MO|TU|WE|TH|FR|SA|SU)$")
_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")
_MEMO = re.compile(r"^(\d{1,2}):(\d{2})\s+([\dhms]+)\s+(.+)$")
_MEMO_FLAG = re.compile(r"\s*\((favorite|deleted|icloud|folder:[^()]*)\)\s*$")


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
    shared_with: tuple[str, ...] = ()  # contact ids it's shared with over iCloud
    deleted: datetime | None = None  # when it went to Recently Deleted, a real wall-clock time

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
    licence: str = ""
    credit: str = ""
    source: str = ""  # the original's page, e.g. on Wikimedia Commons
    thumb_url: str = ""  # a smaller rendition of the original, tried first
    thumb_sha1: str = ""
    faces: list[Face] = field(default_factory=list)


@dataclass
class Face:
    """A face the Photos app found in a photo: its box's centre and size as fractions of the photo."""

    person: str  # a PhotoPerson id
    x: float
    y: float
    size: float
    hidden: bool = False  # hidden from People by the user


@dataclass
class PhotoPerson:
    """A group the Photos app made of one person's (or pet's) faces: a row of its People album."""

    id: str
    name: Optional[str]  # None: never named, like most groups
    contact: Optional[str]  # a contact id the user linked the group to
    merged_into: Optional[str]  # another PhotoPerson id
    pet: Optional[str]  # "dog" or "cat"
    key: Optional[str]  # the photo id of the face Photos shows for them


@dataclass
class WAPerson:
    """Someone on WhatsApp: a contact, or a number that isn't in the address book."""

    id: str
    number: str  # E.164, "+15035550102"
    push_name: str  # the name they gave WhatsApp
    contact: Optional[Contact] = None


@dataclass
class WAMessage:
    real_time: datetime
    sender: str  # "me", a person id, or "notice"
    kind: str  # text, photo, voice, document, location, live_location, call, deleted, notice, reaction
    where: str
    text: str = ""  # the text, or a photo's caption
    starred: bool = False
    reply_to: Optional[str] = None  # whose latest message this answers ("me" or a person id)
    reaction: str = ""  # the emoji
    photo: str = ""  # a photos.json id, or WA_MISSING_PHOTO
    duration: int = 0  # voice notes and calls, seconds
    document: str = ""  # file name
    lat: Optional[float] = None
    lon: Optional[float] = None
    place: str = ""
    video: bool = False  # calls
    outcome: str = ""  # calls: answered, missed or unanswered
    notice: str = ""  # encrypted, created or added
    member: str = ""  # who an "added" notice is about: "me" or a person id


@dataclass
class WAChat:
    id: str
    type: str  # individual, group, broadcast or status
    participants: list[str]  # person ids (a broadcast list's recipients)
    name: str
    creator: str  # groups: "me" or a person id
    lid: bool  # groups: members addressed by LID
    messages: list[WAMessage]


@dataclass
class CalendarDef:
    id: str
    title: str
    color: str  # "#RRGGBB"
    account: str  # one of CALENDAR_ACCOUNTS


@dataclass
class Repeat:
    """A recurrence rule, the subset of RFC 5545 the Calendar app writes."""

    frequency: str  # one of FREQUENCIES
    interval: int = 1
    count: Optional[int] = None  # occurrences, the first included
    until: Optional[date | datetime] = None  # last allowed start: a date (the whole day) or a local time
    by_day: list[tuple[int, str]] = field(default_factory=list)  # (ordinal or 0, "TU")
    by_month: list[int] = field(default_factory=list)

    def occurrences(self, start: date | datetime, horizon: Optional[date] = None,
                    limit: Optional[int] = 5000) -> list:
        """The series' starts in order, beginning with ``start`` (local, like ``start``).

        Series without an end stop at ``horizon`` (default: 10 years after the start), and every
        series after ``limit`` starts (None: no limit, for finding a bounded series' last start).
        """
        horizon = horizon or date(start.year + 10, 12, 31)
        timed = isinstance(start, datetime)
        first = start.date() if timed else start
        out: list = []
        for day in self._days(first, horizon):
            if day > horizon:
                break
            when = datetime.combine(day, start.time()) if timed else day
            if when < start:
                continue
            if isinstance(self.until, datetime):
                if datetime.combine(day, start.time() if timed else datetime.min.time()) > self.until:
                    break
            elif self.until is not None and day > self.until:
                break
            out.append(when)
            if (self.count is not None and len(out) >= self.count) or (limit is not None and len(out) >= limit):
                break
        return out

    def _days(self, first: date, horizon: date):
        """Every day the rule picks, from the period holding ``first`` up to ``horizon``."""
        step = self.interval
        if self.frequency == "daily":
            day = first
            while day <= horizon:
                yield day
                day += timedelta(days=step)
        elif self.frequency == "weekly":
            weekdays = sorted({WEEKDAYS.index(d) for _, d in self.by_day} or {first.weekday()})
            sunday = first - timedelta(days=(first.weekday() + 1) % 7)  # weeks start on Sunday (WKST=SU)
            while sunday <= horizon:
                for wd in sorted(weekdays, key=lambda w: (w + 1) % 7):
                    day = sunday + timedelta(days=(wd + 1) % 7)
                    if day <= horizon:
                        yield day
                sunday += timedelta(weeks=step)
        elif self.frequency in ("monthly", "yearly"):
            year, month = first.year, first.month
            while date(year, month, 1) <= horizon:
                months = [month] if self.frequency == "monthly" else (self.by_month or [first.month])
                for m in sorted(months):
                    yield from sorted(self._days_in_month(year, m, first))
                if self.frequency == "monthly":
                    month += step
                    year, month = year + (month - 1) // 12, (month - 1) % 12 + 1
                else:
                    year += step

    def _days_in_month(self, year: int, month: int, first: date):
        length = (date(year + month // 12, month % 12 + 1, 1) - date(year, month, 1)).days
        if not self.by_day:
            if first.day <= length:
                yield date(year, month, first.day)
            return
        for ordinal, name in self.by_day:
            matches = [date(year, month, d) for d in range(1, length + 1) if date(year, month, d).weekday() == WEEKDAYS.index(name)]
            if ordinal == 0:
                yield from matches
            elif -len(matches) <= ordinal <= len(matches) and ordinal:
                yield matches[ordinal - 1 if ordinal > 0 else ordinal]


@dataclass
class EventDef:
    """One calendar.json event as written in the pack: local wall-clock times, real (unshifted) years."""

    id: str
    calendar: str  # a CalendarDef id
    title: str
    start: date | datetime  # a date for all-day events
    end: date | datetime  # all-day: the last day, inclusive
    created: datetime
    where: str
    time_zone: Optional[str] = None  # None: the persona's zone (ignored for all-day and floating events)
    end_time_zone: Optional[str] = None
    floating: bool = False
    location: str = ""
    address: str = ""
    lat: Optional[float] = None
    lon: Optional[float] = None
    notes: str = ""
    url: str = ""
    organizer: Optional[str] = None  # "me" or a contact id
    attendees: list[str] = field(default_factory=list)  # besides the organizer, who is the chair
    statuses: dict[str, str] = field(default_factory=dict)
    alerts: list[int] = field(default_factory=list)  # minutes before the start
    repeat: Optional[Repeat] = None
    skip: list = field(default_factory=list)  # original starts, same kind as start
    moved: list[tuple] = field(default_factory=list)  # (original start, new start, new end)
    status: str = ""  # "" (none) or one of EVENT_STATUSES
    availability: str = ""  # "" (busy) or "free"

    @property
    def all_day(self) -> bool:
        return not isinstance(self.start, datetime)

    @property
    def real_time(self) -> datetime:
        return self.created


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
    photo_people: list[PhotoPerson] = field(default_factory=list)  # the People album; faces are on the photos
    whatsapp: Optional[list[WAChat]] = None  # None: the pack has no whatsapp/ folder, so the phone has no WhatsApp
    whatsapp_people: dict[str, WAPerson] = field(default_factory=dict)
    whatsapp_documents: dict[str, str] = field(default_factory=dict)  # file name -> text
    calendars: Optional[list[CalendarDef]] = None  # None: the pack has no calendar.json
    events: Optional[list[EventDef]] = None
    voice_memos: Optional[list[VoiceMemoDef]] = None  # None: the pack has no voice_memos.txt

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


def _parse_note(path: Path, contacts: dict[str, Contact]) -> Note:
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
    deleted = datetime.fromisoformat(headers["deleted"]) if "deleted" in headers else None
    if deleted and deleted < modified:
        raise PersonaError(f"{path.name}: deleted before it was last modified")
    shared = tuple(c.strip() for c in headers.get("shared", "").split(",") if c.strip())
    for who in shared:
        if who not in contacts:
            raise PersonaError(f"{path.name}: shared with {who!r}, who isn't in contacts")
        if not contacts[who].emails and not contacts[who].phones:
            raise PersonaError(f"{path.name}: shared with {who!r}, who has no email address or phone number")
    return Note(path.stem, headers.get("folder", "Notes"), created, modified,
                headers.get("pinned", "no").lower() in ("yes", "true"), text, path.name, shared, deleted)


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
            raw.get("licence", ""), raw.get("credit", ""), raw.get("commons_page", ""),
            raw.get("thumb_url", ""), raw.get("thumb_sha1", ""),
            [_parse_face(face, f"{where} face {m}") for m, face in enumerate(raw.get("faces", []), 1)],
        ))
    return photos


def _parse_face(raw: dict, where: str) -> Face:
    try:
        face = Face(str(raw["person"]), float(raw["x"]), float(raw["y"]), float(raw["size"]), bool(raw.get("hidden")))
    except (KeyError, TypeError, ValueError):
        raise PersonaError(f"{where}: a face needs person, x, y and size") from None
    if not (0 <= face.x <= 1 and 0 <= face.y <= 1 and 0 < face.size <= 1):
        raise PersonaError(f"{where}: x and y go from 0 to 1, size from above 0 to 1")
    return face


_PETS = ("dog", "cat")


def _parse_photo_people(path: Path, photos: list[Photo], contacts: dict[str, Contact]) -> list[PhotoPerson]:
    people: list[PhotoPerson] = []
    for n, raw in enumerate(json.loads(path.read_text(encoding="utf-8")).get("people", []), 1):
        where = f"{path.name} person {n} ({raw.get('id', '?')})"
        if not raw.get("id") or any(p.id == raw["id"] for p in people):
            raise PersonaError(f"{where}: needs an id of its own")
        if raw.get("contact") and raw["contact"] not in contacts:
            raise PersonaError(f"{where}: no contact {raw['contact']!r}")
        if raw.get("pet") and raw["pet"] not in _PETS:
            raise PersonaError(f"{where}: pet is dog or cat")
        people.append(PhotoPerson(raw["id"], raw.get("name"), raw.get("contact"), raw.get("merged_into"),
                                  raw.get("pet"), raw.get("key")))
    ids = {p.id for p in people}
    for person in people:
        if person.merged_into is not None and (person.merged_into not in ids or person.merged_into == person.id):
            raise PersonaError(f"{path.name}: {person.id} is merged into {person.merged_into!r}, which isn't another person")
        if person.key and not any(f.person == person.id for p in photos if p.id == person.key for f in p.faces):
            raise PersonaError(f"{path.name}: {person.id}'s key photo {person.key!r} has no face of theirs")
    for photo in photos:
        for face in photo.faces:
            if face.person not in ids:
                raise PersonaError(f"{photo.where}: a face of {face.person!r}, who isn't in people")
    return people


def _parse_wa_people(folder: Path, contacts: dict[str, Contact]) -> dict[str, WAPerson]:
    """Everyone with a phone number can be on WhatsApp; people.txt adds names and non-contacts."""
    people = {}
    for cid, contact in contacts.items():
        if contact.phones:
            number = "+" + re.sub(r"\D", "", contact.phones[0][1])
            people[cid] = WAPerson(cid, number, contact.first or contact.display_name, contact)
    path = folder / "people.txt"
    if not path.exists():
        return people
    for n, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line, where = raw.strip(), f"whatsapp/people.txt:{n}"
        if not line or line.startswith("#"):
            continue
        match = _WA_PERSON.match(line)
        if not match:
            raise PersonaError(f"{where}: can't parse {line!r} (write: id - Name, or id +1 555 0100 Name)")
        pid, number, name = match.group(1), match.group(2).strip(), match.group(3).strip()
        if number == "-":
            if pid not in people:
                raise PersonaError(f"{where}: {pid!r} isn't a contact with a phone number")
            people[pid].push_name = name
        else:
            if pid in contacts:
                raise PersonaError(f"{where}: {pid!r} is a contact; write '{pid} - {name}'")
            people[pid] = WAPerson(pid, "+" + re.sub(r"\D", "", number), name)
    return people


def _wa_tag(tag: str, arg: str, where: str) -> dict:
    """The fields of a bracketed WhatsApp message, e.g. ``[call:video 4m12s]``."""
    arg = (arg or "").strip()
    if tag == "photo":
        if not arg:
            raise PersonaError(f"{where}: [photo:<id>] needs a photo id")
        return {"kind": "photo", "photo": arg}
    if tag == "voice":
        return {"kind": "voice", "duration": _seconds(arg, where)}
    if tag == "document":
        if not arg:
            raise PersonaError(f"{where}: [document:<file name>] needs a file name")
        return {"kind": "document", "document": arg}
    if tag in ("location", "live-location"):
        match = re.match(r"^(-?\d+(?:\.\d+)?),\s*(-?\d+(?:\.\d+)?)(?:\s+(.+))?$", arg)
        if not match:
            raise PersonaError(f"{where}: write [{tag}:<lat>,<lon> <place>]")
        lat, lon = float(match.group(1)), float(match.group(2))
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise PersonaError(f"{where}: {lat},{lon} isn't a place on Earth")
        kind = "live_location" if tag == "live-location" else "location"
        return {"kind": kind, "lat": lat, "lon": lon, "place": (match.group(3) or "").strip()}
    if tag == "call":
        match = re.match(r"^(voice|video)\s+(missed|unanswered|[\dhms]+)$", arg)
        if not match:
            raise PersonaError(f"{where}: write [call:voice 4m12s], [call:video missed] or [call:voice unanswered]")
        medium, length = match.groups()
        if length in ("missed", "unanswered"):
            return {"kind": "call", "video": medium == "video", "outcome": length}
        return {"kind": "call", "video": medium == "video", "outcome": "answered", "duration": _seconds(length, where)}
    if tag == "deleted":
        if arg:
            raise PersonaError(f"{where}: [deleted] takes nothing else")
        return {"kind": "deleted"}
    raise PersonaError(f"{where}: unknown [{tag}]")


def _parse_wa_chat(path: Path, people: dict[str, WAPerson]) -> WAChat:
    headers: dict[str, str] = {}
    messages: list[WAMessage] = []
    day: Optional[date] = None
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.rstrip()
        where = f"whatsapp/{path.name}:{number}"
        if not line or line.startswith("#"):
            continue
        if line.startswith("=="):
            day = date.fromisoformat(line[2:].strip())
            continue
        header = re.match(rf"^({'|'.join(_WA_HEADERS)}):\s*(.*)$", line)
        if header and day is None:
            headers[header.group(1)] = header.group(2).strip()
            continue
        match = _WA_LINE.match(line)
        if not match or day is None:
            raise PersonaError(f"{where}: can't parse {line!r}")
        hour, minute, star, sender, reply_to, text, reaction, tag, arg, caption = match.groups()
        when = datetime(day.year, day.month, day.day, int(hour), int(minute))
        fields: dict = {}
        if sender == "notice":
            words = (text or "").split()
            if tag or reaction or reply_to or star or not words:
                raise PersonaError(f"{where}: write 'notice: encrypted', 'notice: created' or 'notice: added <who>'")
            if words == ["encrypted"] or words == ["created"]:
                fields = {"kind": "notice", "notice": words[0]}
            elif len(words) == 2 and words[0] == "added":
                fields = {"kind": "notice", "notice": "added", "member": words[1]}
            else:
                raise PersonaError(f"{where}: unknown notice {text!r}")
        elif reaction:
            if reply_to or star:
                raise PersonaError(f"{where}: a reaction can't reply or be starred")
            fields = {"kind": "reaction", "reaction": reaction}
        elif tag:
            fields = _wa_tag(tag, arg, where)
            if caption:
                if fields["kind"] != "photo":
                    raise PersonaError(f"{where}: only photos take a caption")
                fields["text"] = caption.strip()
            if reply_to:
                raise PersonaError(f"{where}: only text messages can reply")
        else:
            fields = {"kind": "text", "text": text}
        messages.append(WAMessage(when, sender, where=where, starred=bool(star), reply_to=reply_to, **fields))

    chat_type = headers.get("type", "individual")
    if chat_type not in _WA_TYPES:
        raise PersonaError(f"whatsapp/{path.name}: type must be one of {', '.join(_WA_TYPES)}")
    participants = [p.strip() for p in headers.get("with", "").split(",") if p.strip()]
    for p in participants:
        if p not in people:
            raise PersonaError(f"whatsapp/{path.name}: {p!r} isn't a contact with a phone or in whatsapp/people.txt")
    if not participants:
        raise PersonaError(f"whatsapp/{path.name}: needs a with: line")
    if chat_type == "individual" and len(participants) != 1:
        raise PersonaError(f"whatsapp/{path.name}: a one-to-one chat has one other person (type: group for more)")
    if chat_type in ("group", "broadcast") and not headers.get("name"):
        raise PersonaError(f"whatsapp/{path.name}: a {chat_type} needs a name: line")
    creator = headers.get("created_by", "me")
    if creator != "me" and creator not in participants:
        raise PersonaError(f"whatsapp/{path.name}: created_by {creator!r} isn't in the group")
    chat = WAChat(headers.get("chat", path.stem), chat_type, participants, headers.get("name", ""), creator,
                  headers.get("addressing", "phone") == "lid", messages)

    previous = None
    seen: list[WAMessage] = []
    for m in messages:
        if previous and m.real_time < previous:
            raise PersonaError(f"{m.where}: messages must be in time order")
        previous = m.real_time
        if m.sender not in ("me", "notice") and m.sender not in participants:
            raise PersonaError(f"{m.where}: {m.sender!r} isn't in this chat")
        if chat_type == "broadcast" and m.sender != "me":
            raise PersonaError(f"{m.where}: only you write in a broadcast list")
        if chat_type == "status" and m.sender in ("me", "notice"):
            raise PersonaError(f"{m.where}: status updates here are other people's")
        if chat_type == "status" and m.kind != "text":
            raise PersonaError(f"{m.where}: status updates here are text")
        if m.kind == "notice" and m.notice in ("created", "added") and chat_type != "group":
            raise PersonaError(f"{m.where}: only groups have {m.notice} notices")
        if m.kind == "notice" and m.notice == "added" and m.member != "me" and m.member not in participants:
            raise PersonaError(f"{m.where}: {m.member!r} isn't in this group")
        if m.kind == "call" and chat_type != "individual":
            raise PersonaError(f"{m.where}: calls are written in one-to-one chats")
        if m.kind == "call" and m.outcome == "missed" and m.sender == "me":
            raise PersonaError(f"{m.where}: 'missed' is for calls you didn't pick up; yours are 'unanswered'")
        if m.kind == "call" and m.outcome == "unanswered" and m.sender != "me":
            raise PersonaError(f"{m.where}: 'unanswered' is for your calls; theirs are 'missed'")
        if m.reply_to:
            if m.reply_to not in ("me", *participants):
                raise PersonaError(f"{m.where}: {m.reply_to!r} isn't in this chat")
            if not any(t.sender == m.reply_to and t.kind in _WA_ANSWERABLE for t in seen):
                raise PersonaError(f"{m.where}: no earlier message from {m.reply_to} to reply to")
        if m.kind == "reaction" and not any(t.sender != m.sender and t.kind in _WA_ANSWERABLE for t in seen):
            raise PersonaError(f"{m.where}: a reaction with nothing to react to")
        seen.append(m)
    return chat


def _parse_whatsapp(folder: Path, contacts: dict[str, Contact]):
    people = _parse_wa_people(folder, contacts)
    chats = [_parse_wa_chat(p, people) for p in sorted(folder.glob("*.txt")) if p.name != "people.txt"]
    ids = [c.id for c in chats]
    if len(ids) != len(set(ids)):
        raise PersonaError("whatsapp: duplicate chat ids")
    documents = {p.name.removesuffix(".txt"): p.read_text(encoding="utf-8").strip("\n")
                 for p in sorted((folder / "documents").glob("*.txt"))} if (folder / "documents").is_dir() else {}
    for chat in chats:
        for m in chat.messages:
            if m.kind == "document" and m.document not in documents:
                raise PersonaError(f"{m.where}: no whatsapp/documents/{m.document}.txt")
    return chats, people, documents


def _when(value, where: str, what: str):
    """A pack date (``YYYY-MM-DD``) or local time (``YYYY-MM-DDTHH:MM``)."""
    try:
        if isinstance(value, str) and len(value) == 10:
            return date.fromisoformat(value)
        moment = datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        moment = None
    if moment is None or moment.tzinfo is not None:
        raise PersonaError(f"{where}: {what} should be a date (YYYY-MM-DD) or a local time (YYYY-MM-DDTHH:MM), "
                           f"not {value!r}")
    return moment


def _zone(name, where: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise PersonaError(f"{where}: unknown time zone {name!r}") from None


def _number(value, where: str, what: str, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise PersonaError(f"{where}: {what} should be a whole number of at least {minimum}")
    return value


def _parse_repeat(raw, all_day: bool, where: str) -> Repeat:
    if not isinstance(raw, dict) or set(raw) - _REPEAT_KEYS:
        raise PersonaError(f"{where}: repeat takes {', '.join(sorted(_REPEAT_KEYS))}")
    frequency = raw.get("frequency")
    if frequency not in FREQUENCIES:
        raise PersonaError(f"{where}: repeat frequency should be one of {', '.join(FREQUENCIES)}")
    repeat = Repeat(frequency, _number(raw.get("interval", 1), where, "repeat interval"))
    if "count" in raw and "until" in raw:
        raise PersonaError(f"{where}: a series ends after a count or on a date, not both")
    if "count" in raw:
        repeat.count = _number(raw["count"], where, "repeat count")
    if "until" in raw:
        repeat.until = _when(raw["until"], where, "repeat until")
        if all_day and isinstance(repeat.until, datetime):
            raise PersonaError(f"{where}: an all-day series ends on a date")
    for key, example in (("by_day", '["TU"]'), ("by_month", "[11]")):
        if not isinstance(raw.get(key, []), list):
            raise PersonaError(f"{where}: repeat {key} is a list, e.g. {example}")
    for token in raw.get("by_day", []):
        match = _BY_DAY.match(token) if isinstance(token, str) else None
        ordinal = int(match.group(1) or 0) if match else 0
        if not match or not -5 <= ordinal <= 5:
            raise PersonaError(f"{where}: by_day entries look like TU, 4TH or -1MO, not {token!r}")
        if ordinal and frequency not in ("monthly", "yearly"):
            raise PersonaError(f"{where}: {token} (a numbered weekday) needs a monthly or yearly series")
        repeat.by_day.append((ordinal, match.group(2)))
    for month in raw.get("by_month", []):
        if isinstance(month, bool) or not isinstance(month, int) or not 1 <= month <= 12:
            raise PersonaError(f"{where}: by_month entries are month numbers, 1 to 12")
        repeat.by_month.append(month)
    if repeat.by_month and frequency != "yearly":
        raise PersonaError(f"{where}: by_month goes with a yearly series")
    if frequency == "yearly" and repeat.by_day and not repeat.by_month:
        raise PersonaError(f"{where}: a yearly series on a weekday needs by_month")
    return repeat


def _occurrence(value, event: EventDef, where: str, what: str):
    """An original start in a series: a date (a timed series' time of day is added) or a local time."""
    when = _when(value, where, what)
    if event.all_day and isinstance(when, datetime):
        raise PersonaError(f"{where}: {what} of an all-day series is a date")
    if not event.all_day and not isinstance(when, datetime):
        when = datetime.combine(when, event.start.time())
    horizon = (when.date() if isinstance(when, datetime) else when) + timedelta(days=1)
    if when not in event.repeat.occurrences(event.start, horizon=horizon):
        raise PersonaError(f"{where}: {what} {value} isn't an occurrence of the series")
    return when


def _check_person(who, contacts: dict[str, Contact], owner: Contact, where: str) -> None:
    person = owner if who == "me" else contacts.get(who)
    if person is None:
        raise PersonaError(f"{where}: unknown contact {who!r}")
    if not person.emails:
        raise PersonaError(f"{where}: {who} has no email address, so can't be invited")


def _parse_event(raw, n: int, calendars: dict[str, CalendarDef], contacts: dict[str, Contact], owner: Contact,
                 tz: ZoneInfo) -> EventDef:
    where = f"calendar.json event {n} ({raw.get('id', '?') if isinstance(raw, dict) else '?'})"
    if not isinstance(raw, dict):
        raise PersonaError(f"{where}: an event is an object")
    unknown = set(raw) - _EVENT_KEYS
    if unknown:
        raise PersonaError(f"{where}: unknown field {min(unknown)!r}")
    for key in ("id", "calendar", "title", "start", "created"):
        if not raw.get(key):
            raise PersonaError(f"{where}: needs {key}")
    if raw["calendar"] not in calendars:
        raise PersonaError(f"{where}: no calendar {raw['calendar']!r}")
    start = _when(raw["start"], where, "start")
    all_day = not isinstance(start, datetime)
    if "end" not in raw and not all_day:
        raise PersonaError(f"{where}: a timed event needs an end")
    end = _when(raw.get("end", raw["start"]), where, "end")
    if isinstance(end, datetime) == all_day:
        raise PersonaError(f"{where}: start and end should both be dates (all day) or both times")
    created = _when(raw["created"], where, "created")
    if not isinstance(created, datetime):
        raise PersonaError(f"{where}: created is a local time (YYYY-MM-DDTHH:MM)")
    event = EventDef(raw["id"], raw["calendar"], raw["title"], start, end, created, where,
                     floating=raw.get("floating", False), location=raw.get("location", ""),
                     address=raw.get("address", ""), notes=raw.get("notes", ""), url=raw.get("url", ""),
                     status=raw.get("status", ""), availability=raw.get("availability", ""))
    if not isinstance(event.floating, bool):
        raise PersonaError(f"{where}: floating is true or false")
    if event.floating and all_day:
        raise PersonaError(f"{where}: all-day events float already; floating is for timed events")
    for key in ("time_zone", "end_time_zone"):
        if key in raw:
            if all_day or event.floating:
                raise PersonaError(f"{where}: {key} is for timed events that aren't floating")
            _zone(raw[key], where)
            setattr(event, key, raw[key])
    if all_day or event.floating:
        backwards = end < start
    else:
        start_zone = ZoneInfo(event.time_zone) if event.time_zone else tz
        end_zone = ZoneInfo(event.end_time_zone) if event.end_time_zone else start_zone
        backwards = end.replace(tzinfo=end_zone) < start.replace(tzinfo=start_zone)
    if backwards:
        raise PersonaError(f"{where}: ends before it starts")
    if ("lat" in raw) != ("lon" in raw):
        raise PersonaError(f"{where}: lat and lon go together")
    if "lat" in raw:
        event.lat, event.lon = raw["lat"], raw["lon"]
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (event.lat, event.lon)) \
                or not (-90 <= event.lat <= 90 and -180 <= event.lon <= 180):
            raise PersonaError(f"{where}: {event.lat},{event.lon} isn't a place on Earth")
    for key in ("title", "location", "address", "notes", "url"):
        if not isinstance(getattr(event, key), str):
            raise PersonaError(f"{where}: {key} is text")
    if event.status and event.status not in EVENT_STATUSES:
        raise PersonaError(f"{where}: status should be one of {', '.join(EVENT_STATUSES)}")
    if event.availability not in ("", "busy", "free"):
        raise PersonaError(f"{where}: availability is busy or free")
    event.availability = "" if event.availability == "busy" else event.availability

    organizer, attendees = raw.get("organizer"), raw.get("attendees", [])
    if not isinstance(attendees, list):
        raise PersonaError(f"{where}: attendees is a list of contact ids")
    if attendees and organizer is None:
        raise PersonaError(f"{where}: a meeting with attendees needs an organizer")
    if organizer is not None and calendars[raw["calendar"]].account != "icloud":
        raise PersonaError(f"{where}: invitations go through a server; put meetings in an icloud calendar")
    if organizer is not None:
        _check_person(organizer, contacts, owner, where)
        for who in attendees:
            _check_person(who, contacts, owner, where)
        if organizer in attendees:
            raise PersonaError(f"{where}: the organizer is added as the chair; leave {organizer} out of attendees")
        if len(set(attendees)) != len(attendees):
            raise PersonaError(f"{where}: someone is invited twice")
        if organizer != "me" and "me" not in attendees:
            raise PersonaError(f"{where}: {organizer} organizes it, so 'me' should be among the attendees")
    event.organizer, event.attendees = organizer, list(attendees)
    statuses = raw.get("statuses", {})
    if not isinstance(statuses, dict):
        raise PersonaError(f"{where}: statuses maps attendees to their answers")
    for who, answer in statuses.items():
        if who not in attendees:
            raise PersonaError(f"{where}: {who!r} answers but isn't an attendee")
        if answer not in RSVPS:
            raise PersonaError(f"{where}: {who}'s answer should be one of {', '.join(RSVPS)}")
    event.statuses = dict(statuses)
    alerts = raw.get("alerts", [])
    if not isinstance(alerts, list) or not all(isinstance(a, int) and not isinstance(a, bool) for a in alerts):
        raise PersonaError(f"{where}: alerts are minutes before the start, e.g. [15, 1440]")
    event.alerts = list(alerts)

    for key in ("skip", "moved"):
        if not isinstance(raw.get(key, []), list):
            raise PersonaError(f"{where}: {key} is a list of occurrences")
    if "repeat" in raw:
        event.repeat = _parse_repeat(raw["repeat"], all_day, where)
        until = event.repeat.until
        if until is not None and until < (start if isinstance(until, datetime) or all_day else start.date()):
            raise PersonaError(f"{where}: the repeat ends (until {raw['repeat']['until']}) before the event starts")
        if event.repeat.occurrences(event.start, limit=1) != [event.start]:
            raise PersonaError(f"{where}: the start isn't one of the series' own days")
    elif raw.get("skip") or raw.get("moved"):
        raise PersonaError(f"{where}: skip and moved are for repeating events")
    event.skip = [_occurrence(value, event, where, "skip") for value in raw.get("skip", [])]
    for change in raw.get("moved", []):
        if not isinstance(change, dict) or set(change) != {"from", "start", "end"}:
            raise PersonaError(f"{where}: a moved occurrence is {{\"from\", \"start\", \"end\"}}")
        original = _occurrence(change["from"], event, where, "moved from")
        new_start, new_end = _when(change["start"], where, "moved start"), _when(change["end"], where, "moved end")
        if isinstance(new_start, datetime) == all_day or isinstance(new_end, datetime) == all_day:
            raise PersonaError(f"{where}: a moved occurrence keeps the series' kind (all day or timed)")
        if new_end < new_start:
            raise PersonaError(f"{where}: the moved occurrence ends before it starts")
        event.moved.append((original, new_start, new_end))
    originals = event.skip + [m[0] for m in event.moved]
    if len(set(originals)) != len(originals):
        raise PersonaError(f"{where}: an occurrence is skipped or moved twice")
    return event


def _parse_calendar(path: Path, contacts: dict[str, Contact], owner: Contact,
                    tz: ZoneInfo) -> tuple[list[CalendarDef], list[EventDef]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise PersonaError(f"{path.name}: not valid JSON ({e})") from None
    if not isinstance(raw, dict) or not isinstance(raw.get("calendars"), list) or not isinstance(raw.get("events", []), list):
        raise PersonaError(f"{path.name}: expected {{\"calendars\": [...], \"events\": [...]}}")
    calendars: dict[str, CalendarDef] = {}
    for n, c in enumerate(raw["calendars"], 1):
        where = f"{path.name} calendar {n}"
        if not isinstance(c, dict) or set(c) != {"id", "title", "color", "account"}:
            raise PersonaError(f"{where}: a calendar has id, title, color and account")
        if c["id"] in calendars:
            raise PersonaError(f"{where}: duplicate id {c['id']!r}")
        if not isinstance(c["color"], str) or not _COLOR.match(c["color"]):
            raise PersonaError(f"{where}: color looks like #1BADF8")
        if c["account"] not in CALENDAR_ACCOUNTS:
            raise PersonaError(f"{where}: account should be one of {', '.join(CALENDAR_ACCOUNTS)}")
        if not c["title"] or not isinstance(c["title"], str):
            raise PersonaError(f"{where}: needs a title")
        calendars[c["id"]] = CalendarDef(c["id"], c["title"], c["color"], c["account"])
    for account in ("holidays", "suggestions"):
        if sum(c.account == account for c in calendars.values()) > 1:
            raise PersonaError(f"{path.name}: a phone has one {account} calendar at most")
    events = [_parse_event(e, n, calendars, contacts, owner, tz) for n, e in enumerate(raw.get("events", []), 1)]
    ids = [e.id for e in events]
    if len(ids) != len(set(ids)):
        raise PersonaError(f"{path.name}: duplicate event ids")
    return list(calendars.values()), events


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
    notes = [_parse_note(p, contacts) for p in sorted((folder / "notes").glob("*.txt"))] if (folder / "notes").is_dir() else None
    visits = _parse_visits(folder / "safari.txt") if (folder / "safari.txt").exists() else None
    photos = _parse_photos(folder / "photos.json") if (folder / "photos.json").exists() else None
    photo_people = _parse_photo_people(folder / "photos.json", photos, contacts) if photos is not None else []
    whatsapp, wa_people, wa_documents = (_parse_whatsapp(folder / "whatsapp", contacts)
                                         if (folder / "whatsapp").is_dir() else (None, {}, {}))
    calendars, events = (_parse_calendar(folder / "calendar.json", contacts, owner, tz)
                         if (folder / "calendar.json").exists() else (None, None))
    if raw["shift_years"] % 400:
        # A series is expanded in real years and its rule written unchanged, so after the shift its
        # start, skips and last start would no longer fall on the rule's days.
        for event in events or []:
            if event.repeat is not None:
                raise PersonaError(f"{event.where}: a repeating event needs shift_years 0 (or a multiple of 400): "
                                   "shifting by whole years moves weekdays and 29 February off the rule")
    voice_memos = _parse_voice_memos(folder / "voice_memos.txt") if (folder / "voice_memos.txt").exists() else None
    start, end = (date.fromisoformat(d) for d in raw["real_period"])
    wa_messages = [m for chat in whatsapp or [] for m in chat.messages]
    # Events are checked by when they were made, so they may start after the backup, as on a phone.
    records = ([m for chat in chats for m in chat.messages] + (calls or []) + (voicemails or []) + (notes or [])
               + (visits or []) + (photos or []) + wa_messages + (events or []) + (voice_memos or []))
    taken = {photo.id: photo.real_time for photo in photos or []}
    sent = [m for chat in chats for m in chat.messages if m.photo]
    sent += [m for m in wa_messages if m.photo and m.photo != WA_MISSING_PHOTO]
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
        photo_people=photo_people,
        whatsapp=whatsapp,
        whatsapp_people=wa_people,
        whatsapp_documents=wa_documents,
        calendars=calendars,
        events=events,
        voice_memos=voice_memos,
    )


def list_personas(root: Path = PERSONAS_DIR) -> list[str]:
    return sorted(p.name for p in root.iterdir() if (p / "persona.json").exists())
