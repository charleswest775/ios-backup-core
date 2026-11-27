"""Calendar: HomeDomain ``Library/Calendar/Calendar.sqlitedb`` from a pack's calendar.json.

Built on the harvested schema for the snapshot's iOS version (13.3.1 to 17.3 from public
backups, 26.5 from the Simulator). Encodings follow what the public 13.4.1 and 17.3 backups
show, plus third-party parsers where those backups have no examples (see the Calendar
extractor research):

- **Accounts (Store):** 1 the local store ("Default", "Local" from iOS 16, type 0), 2 the
  native store ("Other", type 5) holding Birthdays and Found in Mail, then iCloud (type 2,
  with the owner's name) and "Subscribed Calendars" (type 4) when the pack has calendars there.
- **Calendars:** a local "Default" calendar (no events, ``supported_entity_types`` NULL) and a
  "Birthdays" calendar (no events: birthdays a person types in live in Home), then the pack's.
  A ``holidays`` calendar is Apple's subscription (``external_id`` ``__HolidayCalendar__``, its
  feed in ``subcal_url``); ``suggestions`` is Found in Mail. ``_SqliteDatabaseProperties``
  names the Birthdays and Found in Mail calendars, the native store, the cache time zone and
  ``_ClientVersion`` (13016, 15019, 16103 and 17202 as seen; the 26.5 value is a guess).
  Read-only calendars carry flag 0x1; iCloud colours are stored ``#RRGGBBFF``.
- **Times** are Cocoa seconds (since 2001-01-01 UTC). A timed event stores the real instant
  with an IANA zone in ``start_tz``/``end_tz``; a floating one stores its wall-clock time as if
  it were UTC with ``_float``; an all-day event stores midnight UTC of its first day with
  ``_float`` and ends at ``start + days * 86400 - 1`` (23:59:59 of its last day).
- **People:** the organizer is a Participant with ``entity_type`` 8; attendees are 7, the
  organizer's own attendee row first with role 3 (chair), the rest role 1 (required). Status
  uses iCalendar's PARTSTAT order (0 needs action, 1 accepted, 2 declined, 3 tentative). Each
  has an Identity row (``mailto:`` address). ``organizer_id`` and ``self_attendee_id`` point at
  Participant rows.
- **Repeats:** one Recurrence row per series: frequency 1 daily, 2 weekly, 3 monthly,
  4 yearly (only yearly is checked against real data), ``count`` (0 for none), ``end_date``
  (the last day allowed: midnight UTC for all-day series, the end of that local day for timed
  ones; unverified), BY* rules in Apple's ``specifier`` (``D=+4TH;O=11``, ``D=0TU``),
  ``week_start`` 7 and ``cached_end_date``, the last occurrence's start (NULL for a series
  without end). Deleted occurrences are ExceptionDate rows; a moved one is its own row with
  ``orig_item_id`` and ``orig_date`` (the original start), sharing the series' UID.
- **Alerts** are Alarm rows with a negative ``trigger_interval`` (seconds before the start).
  From iOS 16 every event also has a ``default_alarm = 1`` row with no interval, as on a phone.
- Unused ids are 0 as on a device (``location_id``, ``organizer_id``, ``self_attendee_id``),
  ``birthday_id`` and ``created_by_id`` are -1, ``can_forward`` 1, and ``UUID`` differs from
  ``unique_identifier`` (the iCalendar UID). Events in subscribed calendars have no
  ``creation_date``.

Not written: OccurrenceCache and OccurrenceCacheDays (Apple's ±2-year expansion cache; the
reader doesn't use it), Category links, attachments, travel time and the change journals.
Floating timed events, multi-day all-day events, ExceptionDate, detached occurrences and the
daily/weekly/monthly codes have not been checked against a real backup yet.
"""

from __future__ import annotations

import random
import uuid
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from backupgen.persona.calendar import EventDef
from backupgen.persona.core import Persona, Snapshot
from backupgen.sqlitegen import SchemaDatabase
from backupgen.writers import cocoa_seconds
from backupgen.writers.context import StepContext

DOMAIN = "HomeDomain"
PATH = "Library/Calendar/Calendar.sqlitedb"
FLOAT = "_float"
HOLIDAY_FEED = "https://calendars.icloud.com/holidays/us_en.ics/"
FREQUENCY_CODES = {"daily": 1, "weekly": 2, "monthly": 3, "yearly": 4}
RSVP_CODES = {"needs_action": 0, "accepted": 1, "declined": 2, "tentative": 3}
STATUS_CODES = {"": 0, "confirmed": 1, "tentative": 2, "cancelled": 3}
_CLIENT_VERSIONS = {13: "13016", 15: "15019", 16: "16103", 17: "17202", 26: "26005"}  # 26: unverified
_STORE_TYPES = {0: "local", 1: "exchange", 2: "caldav", 3: "mobileme", 4: "subscribed", 5: "other"}
_SHARING = {0: "not_shared", 1: "shared_by_me", 2: "shared_with_me"}
_COCOA_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)
_READ_ONLY = 0x1


class Written(list):
    """The events written, for the answer key, plus ``calendars``: every calendar in the database.
    A database with no events still counts as present in the answer key (generate.answer_key)."""

    calendars: list


def _uuid(rng: random.Random) -> str:
    return str(uuid.UUID(bytes=rng.randbytes(16), version=4)).upper()


def _instant(cocoa: float) -> datetime:
    return _COCOA_EPOCH + timedelta(seconds=cocoa)


class _Writer:
    def __init__(self, db: SchemaDatabase, persona: Persona, snapshot: Snapshot, rng: random.Random):
        self.db, self.persona, self.snapshot, self.rng = db, persona, snapshot, rng
        self.version = tuple(int(p) for p in db.schema["ios"].split("."))
        self.owner_email = persona.owner.emails[0][1] if persona.owner.emails else None
        self.identities: dict[tuple, int] = {}
        self.rowids = {"item": 0, "location": 0, "participant": 0, "alarm": 0, "recurrence": 0, "exception": 0}
        self.calendar_rows: dict[str, dict] = {}  # pack calendar id -> answer-key calendar
        self.written = Written()

    def next_id(self, kind: str) -> int:
        self.rowids[kind] += 1
        return self.rowids[kind]

    # ── time ────────────────────────────────────────────────────────────────

    def shift(self, value):
        """A pack date or local time moved onto the device calendar (``shift_years``)."""
        try:
            return value.replace(year=value.year + self.persona.shift_years)
        except ValueError:  # 29 February in a year without one
            return value.replace(year=value.year + self.persona.shift_years, day=28)

    def zone(self, event: EventDef, end: bool = False) -> ZoneInfo:
        name = (event.end_time_zone if end else None) or event.time_zone
        return ZoneInfo(name) if name else self.persona.tz

    def tz_name(self, event: EventDef, end: bool = False) -> str:
        if event.all_day or event.floating:
            return FLOAT
        return self.zone(event, end).key

    def encode(self, event: EventDef, local, end: bool = False) -> float:
        """Cocoa seconds for a pack date or local time of this event, stored the device's way."""
        local = self.shift(local)
        if event.all_day:
            return cocoa_seconds(datetime(local.year, local.month, local.day, tzinfo=timezone.utc))
        if event.floating:
            return cocoa_seconds(local.replace(tzinfo=timezone.utc))
        return cocoa_seconds(local.replace(tzinfo=self.zone(event, end)))

    def typed(self, event: EventDef, cocoa: Optional[float]):
        """A stored date as a reader shows it: a day, a wall-clock time or a UTC instant."""
        if cocoa is None:
            return None
        moment = _instant(cocoa)
        if event.all_day:
            return moment.date().isoformat()
        if event.floating:
            return moment.replace(tzinfo=None).isoformat()
        return moment.isoformat()

    def device(self, real: datetime) -> datetime:
        return self.persona.device_time(real)

    # ── accounts and calendars ──────────────────────────────────────────────

    def identity(self, name: Optional[str], email: str) -> int:
        key = (name, f"mailto:{email}")
        if key not in self.identities:
            self.identities[key] = self.db.table("Identity").insert({"display_name": name, "address": key[1]})
        return self.identities[key]

    def stores_and_calendars(self) -> None:
        stores, calendars = self.db.table("Store"), self.db.table("Calendar")
        pack = self.persona.calendars or []
        accounts = {c.account for c in pack}
        store_ids: dict[str, int] = {}

        def store(key: str, name: str, kind: int, **extra) -> None:
            store_ids[key] = stores.insert({"name": name, "type": kind, "flags": 0, "persistent_id": _uuid(self.rng),
                                            "display_order": len(store_ids), **extra})
            self.store_types[store_ids[key]] = (kind, name)

        self.store_types: dict[int, tuple[int, str]] = {}
        store("local", "Local" if self.version >= (16,) else "Default", 0)  # "Local" on 16.1.2 and 17.3
        store("other", "Other", 5, flags=4)
        if "icloud" in accounts:
            store("icloud", "iCloud", 2, disabled=0, external_id=_uuid(self.rng),
                  owner_name=self.persona.owner.display_name)
        if accounts & {"subscribed", "holidays"}:
            store("subscribed", "Subscribed Calendars", 4, disabled=0, external_id=_uuid(self.rng))
        dsid = str(self.rng.randrange(10 ** 9, 10 ** 10))
        me = self.identity(self.persona.owner.display_name, self.owner_email) if self.owner_email and "icloud" in accounts else -1

        def calendar(key: Optional[str], title: str, store_key: str, color: Optional[str], kind: str, order: int,
                     **extra) -> int:
            rowid = calendars.insert({"store_id": store_ids[store_key], "title": title, "color": color,
                                      "display_order": order, "UUID": _uuid(self.rng), "self_identity_id": -1,
                                      "owner_identity_id": -1, **extra})
            kind_code, store_name = self.store_types[store_ids[store_key]]
            row = {"id": rowid, "title": title, "color": color[:7] if color else None, "kind": kind,
                   "account": {"id": store_ids[store_key], "name": store_name, "type": _STORE_TYPES[kind_code],
                               "disabled": False},
                   "read_only": bool(extra.get("flags", 0) & _READ_ONLY),
                   "subscription_url": extra.get("subcal_url") if self.db.table("Calendar").has("subcal_url") else None,
                   "sharing": _SHARING.get(extra.get("sharing_status")),
                   "owner_email": extra.get("self_identity_email"), "event_count": 0,
                   "_order": (kind_code, store_ids[store_key], order, rowid)}
            self.calendar_rows[key or f"#{rowid}"] = row
            return rowid

        calendar(None, "Default", "local", None, "normal", 0, flags=0x2)
        self.birthdays = calendar(None, "Birthdays", "other", "#8295AF", "birthdays", 0, flags=0x5,
                                  supported_entity_types=4)
        self.suggestions = None
        for order, c in enumerate(pack, 1):
            if c.account == "icloud":
                rowid = calendar(c.id, c.title, "icloud", c.color.upper() + "FF", "normal", order, flags=0x100900,
                                 supported_entity_types=4, external_id=f"/{dsid}/calendars/{c.id}/", sharing_status=0,
                                 self_identity_id=me, owner_identity_id=me, self_identity_email=self.owner_email,
                                 owner_identity_email=self.owner_email)
            elif c.account == "local":
                rowid = calendar(c.id, c.title, "local", c.color.upper(), "normal", order, flags=0,
                                 supported_entity_types=4)
            elif c.account == "holidays":
                rowid = calendar(c.id, c.title, "subscribed", c.color.upper(), "holidays", order, flags=0x1009,
                                 supported_entity_types=4, external_id="__HolidayCalendar__", subcal_url=HOLIDAY_FEED,
                                 refresh_interval=604800)
            elif c.account == "subscribed":
                rowid = calendar(c.id, c.title, "subscribed", c.color.upper(), "subscribed", order, flags=0x1,
                                 supported_entity_types=4, external_id=_uuid(self.rng))
            else:  # suggestions: Found in Mail
                rowid = calendar(c.id, c.title, "other", c.color.upper(), "suggestions", order, flags=0x40005,
                                 supported_entity_types=4)
                self.suggestions = rowid
            self.calendar_rows[c.id]["_rowid"] = rowid

        properties = self.db.table("_SqliteDatabaseProperties")
        values = {"_UniqueIdentifier": _uuid(self.rng), "NativeStoreID": str(store_ids["other"]),
                  "BirthdayCalendarID": str(self.birthdays), "OccurrenceCacheTimeZone": self.persona.tz.key}
        if self.suggestions:
            values["SuggestionsCalendarID"] = str(self.suggestions)
        if self.version[0] in _CLIENT_VERSIONS:
            values["_ClientVersion"] = _CLIENT_VERSIONS[self.version[0]]
        for key, value in values.items():
            properties.insert({"key": key, "value": value})

    # ── events ──────────────────────────────────────────────────────────────

    def person(self, who: str) -> tuple[str, str]:
        contact = self.persona.owner if who == "me" else self.persona.contacts[who]
        return contact.display_name, contact.emails[0][1]

    def people(self, event: EventDef, owner_id: int) -> tuple[int, int, Optional[dict], list[dict]]:
        """Participant rows for a meeting: (organizer_id, self_attendee_id, organizer, attendees)."""
        if event.organizer is None:
            return 0, 0, None, []
        table = self.db.table("Participant")
        name, email = self.person(event.organizer)
        organizer_id = self.next_id("participant")
        table.insert({"ROWID": organizer_id, "entity_type": 8, "type": 0, "status": 0, "pending_status": 0, "role": 0,
                      "identity_id": self.identity(name, email), "owner_id": owner_id, "UUID": _uuid(self.rng),
                      "email": email, "is_self": int(event.organizer == "me")})
        organizer = {"name": name, "email": email, "is_me": event.organizer == "me"}
        self_id, attendees = 0, []
        for who in [event.organizer, *event.attendees]:
            name, email = self.person(who)
            chair = who == event.organizer
            answer = "accepted" if chair else event.statuses.get(who, "needs_action")
            rowid = self.next_id("participant")
            table.insert({"ROWID": rowid, "entity_type": 7, "type": 1, "status": RSVP_CODES[answer],
                          "pending_status": RSVP_CODES[answer], "role": 3 if chair else 1,
                          "identity_id": self.identity(name, email), "owner_id": owner_id, "UUID": _uuid(self.rng),
                          "email": email, "is_self": int(who == "me")})
            if who == "me":
                self_id = rowid
            attendees.append({"name": name, "email": email, "phone": None, "status": answer,
                              "role": "chair" if chair else "required", "type": "person", "is_me": who == "me"})
        return organizer_id, self_id, organizer, attendees

    def location(self, event: EventDef, owner_id: int) -> tuple[int, Optional[dict]]:
        if not event.location and not event.address:
            return 0, None
        rowid = self.next_id("location")
        located = event.lat is not None
        self.db.table("Location").insert({
            "ROWID": rowid, "title": event.location or None, "address": event.address or None,
            "latitude": float(event.lat) if located else None, "longitude": float(event.lon) if located else None,
            "reference_frame": 0, "radius": 141.0 if located else None, "item_owner_id": owner_id,
            "alarm_owner_id": 0, "start_loc_owner_id": 0, "end_loc_owner_id": 0, "client_loc_owner_id": 0,
        })
        latitude, longitude = (float(event.lat), float(event.lon)) if located else (None, None)
        if latitude == 0 and longitude == 0:
            latitude = longitude = None
        return rowid, {"title": event.location or None, "address": event.address or None,
                       "latitude": latitude, "longitude": longitude}

    def alarms(self, event: EventDef, owner_id: int) -> list[dict]:
        table = self.db.table("Alarm")
        common = {"type": 0, "calendaritem_owner_id": owner_id, "calendar_owner_id": -1, "proximity": 0,
                  "disabled": 0, "location_id": 0, "orig_alarm_id": 0, "has_long_interval": 0}
        out = []
        for minutes in event.alerts:
            table.insert({"ROWID": self.next_id("alarm"), "trigger_interval": -minutes * 60, "default_alarm": 0,
                          "UUID": _uuid(self.rng), **common})
            out.append({"seconds_before": minutes * 60})
        if self.version >= (16,):  # iOS 16 on: every event carries a placeholder for the calendar's default alert
            table.insert({"ROWID": self.next_id("alarm"), "trigger_interval": None, "default_alarm": 1,
                          "UUID": _uuid(self.rng), **common})
        return out

    def recurrence(self, event: EventDef, owner_id: int) -> dict:
        repeat = event.repeat
        bounded = repeat.count is not None or repeat.until is not None
        horizon = (repeat.until if not isinstance(repeat.until, datetime) else repeat.until.date()) \
            if repeat.until is not None else date(event.start.year + 200, 12, 31)
        last = repeat.occurrences(event.start, horizon=horizon, limit=None)[-1] if bounded else None
        last_c = self.encode(event, last) if last is not None else None
        until_c = None
        if repeat.until is not None:
            until = repeat.until
            if not event.all_day and not isinstance(until, datetime):
                until = datetime.combine(until, time(23, 59, 59))  # the whole of the last day
            until_c = self.encode(event, until)
        tokens = [f"{'+' if o > 0 else ''}{o}{d}" if o else f"0{d}" for o, d in repeat.by_day]
        parts = ([f"D={','.join(tokens)}"] if tokens else []) + \
                ([f"O={','.join(str(m) for m in repeat.by_month)}"] if repeat.by_month else [])
        self.db.table("Recurrence").insert({
            "ROWID": self.next_id("recurrence"), "frequency": FREQUENCY_CODES[repeat.frequency],
            "interval": repeat.interval, "week_start": 7, "count": repeat.count or 0,
            "cached_end_date": last_c, "cached_end_date_tz": self.tz_name(event) if last_c is not None else None,
            "end_date": until_c, "specifier": ";".join(parts) or None, "by_month_months": 0, "owner_id": owner_id,
            "UUID": _uuid(self.rng),
        })
        return {"frequency": repeat.frequency, "interval": repeat.interval, "count": repeat.count,
                "until": self.typed(event, until_c),
                "by_day": [f"{o}{d}" if o else d for o, d in repeat.by_day], "by_month_day": [],
                "by_month": list(repeat.by_month), "by_set_pos": [], "by_year_day": [], "by_week_no": [],
                "week_start": "SU", "last_start": self.typed(event, last_c)}

    def item(self, event: EventDef, start, end, created: datetime, modified: datetime, uid: str,
             master: Optional[tuple[int, float]] = None) -> dict:
        """One CalendarItem row (a series' master, a single event or a moved occurrence) and what it holds."""
        rowid = self.next_id("item")
        calendar = self.calendar_rows[event.calendar]
        account = next(c.account for c in self.persona.calendars if c.id == event.calendar)
        subscribed = account in ("holidays", "subscribed")
        location_id, location = self.location(event, rowid)
        organizer_id, self_id, organizer, attendees = self.people(event, rowid)
        alarms = self.alarms(event, rowid)
        start_c = self.encode(event, start)
        if event.all_day:
            end_c = start_c + ((end - start).days + 1) * 86400 - 1
        else:
            end_c = self.encode(event, end, end=True)
        free = event.availability == "free" or account == "holidays"
        repeats = event.repeat is not None and master is None
        self.db.table("CalendarItem").insert({
            "ROWID": rowid, "summary": event.title, "location_id": location_id, "client_location_id": 0,
            "description": event.notes or None, "start_date": start_c, "start_tz": self.tz_name(event),
            "end_date": end_c, "end_tz": self.tz_name(event, end=True), "all_day": int(event.all_day),
            "calendar_id": calendar["_rowid"], "orig_item_id": master[0] if master else 0,
            "orig_date": master[1] if master else None, "organizer_id": organizer_id, "self_attendee_id": self_id,
            "status": STATUS_CODES[event.status], "invitation_status": 0, "availability": 1 if free else 0,
            "privacy_level": 0, "url": event.url or None, "last_modified": cocoa_seconds(modified), "sequence_num": 0,
            "birthday_id": -1, "modified_properties": 0, "external_tracking_status": 0,
            "external_id": f"{uid}.ics" if account == "icloud" else None, "unique_identifier": uid, "hidden": 0,
            "has_recurrences": int(repeats), "has_attachment": 0, "has_attendees": int(bool(attendees)),
            "UUID": _uuid(self.rng), "entity_type": 2, "priority": 0, "due_all_day": 0,
            "creation_date": None if subscribed else cocoa_seconds(created), "display_order": 0,
            "created_by_id": -1, "modified_by_id": -1, "invitation_changed_properties": 0, "default_alarm_removed": 0,
            "phantom_master": 0, "travel_advisory_behavior": 0, "start_location_id": 0, "end_location_id": 0,
            "suggested_event_info_id": 0, "can_forward": 1, "location_prediction_state": 0, "fired_ttl": 0,
            "disallow_propose_new_time": 0, "junk_status": 0, "flags": 0, "error_id": 0, "image_id": 0, "color_id": 0,
        })
        calendar["event_count"] += 1
        record = {
            "id": rowid, "pack_id": event.id, "uid": uid, "calendar": calendar["title"],
            "calendar_id": calendar["_rowid"], "title": event.title, "notes": event.notes or None,
            "url": event.url or None, "conference_url": None, "all_day": event.all_day,
            "floating": event.all_day or event.floating,
            "start": None, "end": None, "start_day": None, "end_day": None, "days": None,
            "wall_start": None, "wall_end": None, "time_zone": None, "end_time_zone": None,
            "status": event.status or "none", "availability": "free" if free else "busy", "location": location,
            "recurrence": self.recurrence(event, rowid) if repeats else None, "exceptions": [],
            "detached_from": {"event_id": master[0], "original_start": self.typed(event, master[1])} if master else None,
            "organizer": organizer, "attendees": attendees, "alarms": alarms, "year_unknown": False,
            "created_date": None if subscribed else _instant(cocoa_seconds(created)).isoformat(),
            "modified_date": _instant(cocoa_seconds(modified)).isoformat(),
        }
        if event.all_day:
            days = (end - start).days + 1
            first = _instant(start_c).date()
            record.update(floating=True, start_day=first.isoformat(), days=days,
                          end_day=(first + timedelta(days=days - 1)).isoformat())
        elif event.floating:
            record.update(wall_start=self.typed(event, start_c), wall_end=self.typed(event, end_c))
        else:
            record.update(start=self.typed(event, start_c), end=self.typed(event, end_c),
                          time_zone=self.tz_name(event),
                          end_time_zone=self.tz_name(event, end=True) if event.end_time_zone and
                          self.tz_name(event, end=True) != self.tz_name(event) else None)
        if repeats:
            exceptions = self.db.table("ExceptionDate")
            for original in event.skip:
                exceptions.insert({"ROWID": self.next_id("exception"), "owner_id": rowid,
                                   "date": self.encode(event, original), "sync_order": 0})
                record["exceptions"].append(self.typed(event, self.encode(event, original)))
            record["exceptions"].sort()
        self.written.append(record)
        return record

    def events(self) -> None:
        backup = self.snapshot.backup_date
        made = []
        for n, event in enumerate(self.persona.events or []):
            created = self.device(event.created).replace(second=self.rng.randrange(60))
            modified = min(created + timedelta(seconds=self.rng.randrange(30, 3600)), backup)
            made.append((event.created, n, created, modified, event))
        # Rows in the order they were made; a subscription's arrive together, in the feed's order.
        for _, _, created, modified, event in sorted(made, key=lambda m: m[:2]):
            if created > backup:
                continue  # made after the backup
            uid = _uuid(self.rng)
            master = self.item(event, event.start, event.end, created, modified, uid)
            for original, new_start, new_end in event.moved:
                self.item(event, new_start, new_end, created, modified, uid,
                          master=(master["id"], self.encode(event, original)))
                self.written[-1]["pack_id"] = f"{event.id}@{self.typed(event, self.encode(event, original))}"


def populate(db: SchemaDatabase, persona: Persona, snapshot: Snapshot, rng: random.Random) -> Written:
    """Fill Calendar.sqlitedb from the pack. Returns the events written, with ``.calendars`` set."""
    writer = _Writer(db, persona, snapshot, rng)
    writer.stores_and_calendars()
    writer.events()
    rows = sorted(writer.calendar_rows.values(), key=lambda c: c["_order"])
    writer.written.calendars = [{k: v for k, v in c.items() if not k.startswith("_")} for c in rows]
    return writer.written


def step(ctx: StepContext) -> Optional[Written]:
    """Calendar.sqlitedb, from a random generator of its own. Returns the events written, with ``.calendars``."""
    persona, snapshot = ctx.persona, ctx.snapshot
    if persona.calendars is None:
        return None
    events: list[Written] = []
    rng = random.Random(f"{persona.id}/{snapshot.id}/calendar")
    database = ctx.build("Calendar.sqlitedb", lambda db: events.append(populate(db, persona, snapshot, rng)))
    ctx.writer.add_file(DOMAIN, PATH, database)
    return events[0]


def answer_key(written: Optional[Written]) -> Optional[dict]:
    """Shaped like the reader's records: every calendar, and the events in row order. None: no calendar.json."""
    if written is None:
        return None
    return {"calendars": written.calendars, "events": list(written)}
