"""The Calendar app in a persona pack.

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
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from backupgen.persona.core import AreaData, Contact, Pack, PersonaError

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


def load(pack: Pack) -> AreaData:
    """Persona.calendars and .events, both None when the pack has no calendar.json.

    Events are dated (for the period check) by when they were made, so they may start after
    the backup, as on a phone.
    """
    if not (pack.folder / "calendar.json").exists():
        return AreaData({"calendars": None, "events": None})
    calendars, events = _parse_calendar(pack.folder / "calendar.json", pack.contacts, pack.owner, pack.tz)
    if pack.shift_years % 400:
        # A series is expanded in real years and its rule written unchanged, so after the shift its
        # start, skips and last start would no longer fall on the rule's days.
        for event in events:
            if event.repeat is not None:
                raise PersonaError(f"{event.where}: a repeating event needs shift_years 0 (or a multiple of 400): "
                                   "shifting by whole years moves weekdays and 29 February off the rule")
    return AreaData({"calendars": calendars, "events": events}, records=events)
