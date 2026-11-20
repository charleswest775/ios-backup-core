"""Tests for CalendarExtractor, events_to_ics and recurrence_rrule.

Each Calendar.sqlitedb is built with backupgen.sqlitegen from the real table
definitions harvested in backupgen/schemas (iOS 13.3.1 to 26.5), so the
queries run against the columns iOS really ships; one more is a bare schema
with almost no columns. The all-day encoding (``_float`` midnight UTC to
23:59:59 of the last day) and the zoned one (the real UTC instant plus an
IANA name) are the ones seen in the public Hickman backups; floating timed
events (the wall clock written as if UTC) follow third-party parsers and
are not checked against a real backup yet. All calendars, events, names and
addresses are made up.
"""

import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, ROOT)

from backupgen import SCHEMAS_DIR  # noqa: E402
from backupgen.container import BackupWriter, Device  # noqa: E402
from backupgen.sqlitegen import build, load_schema  # noqa: E402
from ios_backup_core.backup import open_local_backup  # noqa: E402
from ios_backup_core.extractors.calendar_events import (  # noqa: E402
    CALENDAR_DB_PATH,
    CALENDAR_DOMAIN,
    CalendarExtractor,
    _cocoa,
    _ics_duration,
    _zone_changes,
    events_to_ics,
    recurrence_rrule,
)

VERSIONS = sorted(
    (p.parent.name.removeprefix("ios-") for p in SCHEMAS_DIR.glob("ios-*/Calendar.sqlitedb.json")),
    key=lambda v: tuple(int(n) for n in v.split(".")),
)
LATEST = VERSIONS[-1]
EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)
NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
NY = "America/New_York"


def utc(*parts) -> float:
    """Calendar seconds for a UTC instant (also the stored form of a floating wall clock)."""
    return (datetime(*parts, tzinfo=timezone.utc) - EPOCH).total_seconds()


DAY = 86400


class _FakeBackup:
    """Accessor stub keyed by (domain, path). ``unreadable`` entries are in
    the manifest but can't be extracted."""

    def __init__(self, files=None, unreadable=()):
        self.files = dict(files or {})
        self.unreadable = set(unreadable)

    def get_file(self, relative_path, domain="HomeDomain"):
        return self.files.get((domain, relative_path))

    def list_files(self, domain=None, path_like=None):
        entries = [*self.files, *self.unreadable]
        if domain:
            entries = [e for e in entries if e[0] == domain]
        if path_like:
            pattern = ".*".join(re.escape(p) for p in path_like.split("%"))
            entries = [e for e in entries if re.fullmatch(pattern, e[1], re.IGNORECASE)]
        return [{"hash": "", "domain": d, "path": p} for d, p in entries]


def _backup(path) -> _FakeBackup:
    return _FakeBackup({(CALENDAR_DOMAIN, CALENDAR_DB_PATH): str(path)})


# ----------------------------------------------------------------------
# Building databases
# ----------------------------------------------------------------------

class _Writer:
    """Inserts rows with the defaults real rows have (0 for unused ids, -1 birthday_id)."""

    def __init__(self, db):
        self.db = db

    def has(self, table, column) -> bool:
        t = self.db.table(table)
        return t is not None and t.has(column)

    def add(self, table, **row):
        t = self.db.table(table)
        return t.insert(row) if t is not None else None

    def sql(self, statement, *params):
        self.db.conn.execute(statement, params)

    def prop(self, key, value):
        self.add("_SqliteDatabaseProperties", key=key, value=str(value))

    def store(self, rowid, name, type_, **kw):
        self.add("Store", ROWID=rowid, name=name, type=type_, **kw)

    def calendar(self, rowid, title, store_id, **kw):
        row = dict(ROWID=rowid, title=title, store_id=store_id, supported_entity_types=4,
                   color="#FF9500", flags=0, UUID=f"CAL-{rowid}")
        row.update(kw)
        self.add("Calendar", **row)

    def event(self, rowid, calendar_id, summary, start, end, tz=NY, all_day=0, **kw):
        row = dict(
            ROWID=rowid, calendar_id=calendar_id, summary=summary, start_date=start,
            end_date=end, start_tz=tz, end_tz=tz, all_day=all_day, entity_type=2,
            UUID=f"ROW-{rowid}", unique_identifier=f"uid-{rowid}@example.com", hidden=0,
            has_recurrences=0, has_attendees=0, orig_item_id=0, orig_date=None,
            location_id=0, organizer_id=0, self_attendee_id=0, birthday_id=-1, status=0,
            availability=0, junk_status=0, phantom_master=0,
        )
        row.update(kw)
        self.add("CalendarItem", **row)

    def all_day(self, rowid, calendar_id, summary, day, days=1, **kw):
        start = utc(day.year, day.month, day.day)
        self.event(rowid, calendar_id, summary, start, start + days * DAY - 1,
                   tz="_float", all_day=1, **kw)

    def recurrence(self, owner, frequency, **kw):
        row = dict(owner_id=owner, frequency=frequency, interval=1, week_start=7, count=0,
                   end_date=None, specifier=None, cached_end_date=None, by_month_months=0)
        row.update(kw)
        self.add("Recurrence", **row)

    def identity(self, **kw) -> int:
        return self.add("Identity", **kw)

    def participant(self, rowid, owner, entity, **kw):
        row = dict(ROWID=rowid, owner_id=owner, entity_type=entity, type=1, status=0, role=0,
                   identity_id=0, email=None, phone_number=None, is_self=0, UUID=f"P-{rowid}")
        row.update(kw)
        self.add("Participant", **row)

    def alarm(self, owner, **kw):
        row = dict(calendaritem_owner_id=owner, calendar_owner_id=0, trigger_interval=None,
                   trigger_date=None, type=0, proximity=0, default_alarm=0)
        row.update(kw)
        self.add("Alarm", **row)


def _build(path: Path, version: str, fill) -> Path:
    build(path, version, "Calendar.sqlitedb", lambda db: fill(_Writer(db)))
    return path


# Calendar ids in the rich fixture.
C_ORPHAN, C_LOCAL, C_GROCERY, C_CHORES = 41, 1, 2, 3
C_HOME, C_WORK, C_INBOX, C_NOTIFY, C_OUTBOX = 10, 11, 12, 13, 14
C_HOLIDAYS, C_FEED = 20, 21
C_BIRTHDAYS, C_FB, C_MAIL, C_SIRI, C_OLD_BIRTHDAYS = 30, 31, 32, 33, 34
C_OFFICE, C_ODD = 40, 42


def _rich(w: _Writer):
    w.prop("BirthdayCalendarID", C_BIRTHDAYS)
    w.prop("FacebookBirthdayCalendarID", C_FB)
    w.prop("SuggestionsCalendarID", C_MAIL)
    w.prop("NaturalLanguageSuggestionsCalendarID", C_SIRI)
    w.prop("NativeStoreID", 2)
    w.prop("OccurrenceCacheTimeZone", NY)

    w.store(1, "Default", 0)
    w.store(2, "Other", 5)
    w.store(3, "iCloud", 2, disabled=0)
    w.store(4, "Subscribed Calendars", 4, disabled=0)
    w.store(5, "Office", 1, disabled=1)
    w.store(6, "Strange", 9, disabled=0)

    w.calendar(C_LOCAL, "Personal", 1, supported_entity_types=None, color="#FF2968", flags=2)
    w.calendar(C_GROCERY, "Groceries", 1, supported_entity_types=8)
    w.calendar(C_CHORES, "Chores", 1, supported_entity_types=8)
    w.calendar(C_HOME, "Home", 3, color="#34AADCFF", self_identity_email="me@example.com",
               sharing_status=0, display_order=1, flags=0x100900,
               external_id="/1234/calendars/home/")
    w.calendar(C_WORK, "Work", 3, color="#1badf8ff", self_identity_email="me@example.com",
               sharing_status=1, display_order=2, external_id="/1234/calendars/work/")
    w.calendar(C_INBOX, "Inbox", 3, display_order=-1, flags=0x100022,
               external_id="/1234/calendars/inbox/")
    w.calendar(C_NOTIFY, "Notifications", 3, display_order=-1, flags=0x100047,
               external_id="/1234/calendars/notification/")
    w.calendar(C_OUTBOX, "Outbox", 3, display_order=-1, external_id="/1234/calendars/outbox/")
    w.calendar(C_HOLIDAYS, "US Holidays", 4, external_id="__HolidayCalendar__", flags=0x1009,
               display_order=1, sharing_status=0,
               subcal_url="https://calendars.example.com/holidays.ics")
    w.calendar(C_FEED, "Team Feed", 4, color="blue", flags=1, display_order=2)
    w.calendar(C_BIRTHDAYS, "Birthdays", 2, flags=5)
    w.calendar(C_FB, "Friend Birthdays", 2, flags=0x207)
    w.calendar(C_MAIL, "Found in Mail", 2, flags=0x40005)
    w.calendar(C_SIRI, "Found in Siri", 2, flags=0x80007)
    w.calendar(C_OLD_BIRTHDAYS, "Old Birthdays", 2, flags=5)
    w.calendar(C_OFFICE, "Office", 5, sharing_status=2)
    w.calendar(C_ORPHAN, "Lost", 99, sharing_status=7)
    w.calendar(C_ODD, "Odd", 6)
    # A per-calendar default alarm: never an event's alarm.
    w.alarm(0, calendar_owner_id=C_HOME, trigger_interval=-1800)

    # 100: zoned timed event with everything.
    w.add("Location", ROWID=1, title="Clinic", address="1 Main St", latitude=40.7,
          longitude=-74.0, item_owner_id=100)
    pat = w.identity(display_name="Pat Doe", address="mailto:pat@example.com")
    w.participant(1, 100, 8, type=0, identity_id=pat, email="pat@example.com")
    w.participant(2, 100, 7, status=1, role=1, type=1, email="me@example.com")
    w.event(100, C_HOME, "Dentist", utc(2024, 1, 8, 14), utc(2024, 1, 8, 15),
            unique_identifier="dentist@example.com", description="Bring the form; ask about X-rays, too.",
            url="https://clinic.example.com/visit", location_id=1, organizer_id=1,
            self_attendee_id=2, has_attendees=1, creation_date=utc(2023, 12, 1, 10),
            last_modified=utc(2023, 12, 2, 11))
    w.alarm(100, trigger_interval=-900)
    w.alarm(100, default_alarm=1)

    # Dates of every kind.
    w.event(101, C_HOME, "Call the plumber", utc(2024, 2, 3, 9, 30), utc(2024, 2, 3, 10, 30), tz="_float")
    w.all_day(102, C_HOLIDAYS, "Independence Day", datetime(2024, 7, 4), availability=1)
    tokyo_midnight = utc(2024, 3, 14, 15)  # 2024-03-15 00:00 in Tokyo
    w.event(103, C_OFFICE, "Office closed", tokyo_midnight, tokyo_midnight + DAY,
            tz="Asia/Tokyo", all_day=1)
    w.all_day(104, C_HOME, "Lake trip", datetime(2024, 8, 10), days=3)
    w.event(105, C_HOME, "Millennium party", 0.0, DAY - 1.0, tz="_float", all_day=1)
    w.event(106, C_HOME, "Someday", -6.4e10, None)
    w.event(130, C_HOME, "Garbled", "soon", "later")

    # 107/108: weekly series with two deleted dates and one moved occurrence.
    w.event(107, C_HOME, "Climbing", utc(2024, 1, 9, 23, 30), utc(2024, 1, 10, 1, 30),
            unique_identifier="climb@example.com", has_recurrences=1)
    w.recurrence(107, 2, specifier="D=0TU", week_start=7, end_date=utc(2024, 6, 25, 22, 30),
                 cached_end_date=utc(2024, 6, 25, 22, 30))
    w.add("ExceptionDate", owner_id=107, date=utc(2024, 3, 5, 23, 30))
    w.add("ExceptionDate", owner_id=107, date=utc(2024, 2, 13, 23, 30))
    w.event(108, C_HOME, "Climbing (moved)", utc(2024, 2, 21, 23, 30), utc(2024, 2, 22, 1, 30),
            unique_identifier="climb@example.com", orig_item_id=107,
            orig_date=utc(2024, 2, 20, 23, 30))

    # 109: yearly all-day series with a count and one deleted year.
    w.all_day(109, C_HOLIDAYS, "Midsummer", datetime(2024, 6, 16), has_recurrences=1)
    w.recurrence(109, 4, count=6, specifier="D=+3SU;O=6", cached_end_date=utc(2029, 6, 17))
    w.add("ExceptionDate", owner_id=109, date=utc(2025, 6, 15))

    # 110-113: other frequencies and every specifier token form.
    w.event(110, C_HOME, "Rent", utc(2024, 1, 1, 9), utc(2024, 1, 1, 9, 15), tz="_float",
            has_recurrences=1)
    w.recurrence(110, 3, interval=2, specifier="M=1,15", count=0, week_start=0)
    w.event(111, C_WORK, "Standup", utc(2024, 1, 15, 14), utc(2024, 1, 15, 14, 15),
            has_recurrences=1)
    w.recurrence(111, 1, interval=None, count=10)
    w.event(112, C_HOME, "Mystery", utc(2024, 1, 20, 14), utc(2024, 1, 20, 15), has_recurrences=1)
    w.recurrence(112, 9)
    w.event(113, C_HOME, "Tokens", utc(2024, 1, 1, 9), utc(2024, 1, 1, 10), tz="_float",
            has_recurrences=1)
    w.recurrence(113, 4, week_start=1, end_date=utc(2030, 1, 1, 9),
                 cached_end_date=utc(2029, 12, 31, 9),
                 specifier="D=+1MO,-2FR,0WE,TH,xx;M=-1,0,40;O=1,12;S=2;Y=100,-1;W=20;X=5;garbage")

    # 114-116: a phantom master and its two occurrences.
    w.event(114, C_HOME, "Placeholder", utc(2024, 4, 1, 14), utc(2024, 4, 1, 15),
            unique_identifier="series-x@example.com", has_recurrences=1, phantom_master=1)
    w.recurrence(114, 2)
    for rowid, day in ((115, 2), (116, 9)):
        w.event(rowid, C_HOME, "Book club", utc(2024, 4, day, 14), utc(2024, 4, day, 15),
                unique_identifier="series-x@example.com", orig_item_id=114,
                orig_date=utc(2024, 4, day - 1, 14))

    # 117-119: hidden, junk, and a reminder row.
    w.event(117, C_HOME, "Hidden", utc(2024, 5, 1, 14), utc(2024, 5, 1, 15), hidden=1)
    w.event(118, C_HOME, "Junk invite", utc(2024, 5, 2, 14), utc(2024, 5, 2, 15), junk_status=2)
    w.event(119, C_HOME, "A reminder", utc(2024, 5, 3, 14), None, entity_type=3)

    # 120: cancelled.
    w.event(120, C_WORK, "Offsite", utc(2024, 5, 6, 14), utc(2024, 5, 6, 22), status=3,
            availability=3)

    # 121-123: birthdays, one with an unknown year.
    w.all_day(121, C_BIRTHDAYS, "Jo's birthday", datetime(1604, 3, 2), has_recurrences=1)
    w.recurrence(121, 4)
    w.all_day(122, C_BIRTHDAYS, "Ari's birthday", datetime(1990, 5, 1))
    w.all_day(123, C_HOME, "Old fair", datetime(1604, 7, 1))

    # 124/125: locations without usable coordinates or without a name.
    w.add("Location", ROWID=2, title="Somewhere", address=None, latitude=0, longitude=0)
    w.add("Location", ROWID=3, title=None, address=None, latitude=10.5, longitude=20.25)
    w.event(124, C_HOME, "Null island", utc(2024, 6, 1, 14), utc(2024, 6, 1, 15), location_id=2)
    w.event(125, C_HOME, "Coordinates only", utc(2024, 6, 2, 14), utc(2024, 6, 2, 15), location_id=3)

    # 126: a meeting someone else organised; every status, role and type.
    robin = w.identity(first_name="Robin", last_name="Vale", address="mailto:boss@example.com")
    lee = w.identity(display_name="Lee Park", address="/aGVsbG8gd29ybGQ=/principal/")
    sam = w.identity(address="mailto:sam@example.com")
    kim = w.identity(first_name="Kim", address="mailto:kim@example.com")
    w.participant(10, 126, 8, type=0, identity_id=robin, email="boss@example.com")
    w.participant(11, 126, 7, status=0, role=2, type=1, identity_id=lee)
    w.participant(12, 126, 7, status=2, role=3, type=2, identity_id=sam)
    w.participant(13, 126, 7, status=3, role=4, type=3, email="ME@Example.com")
    w.participant(14, 126, 7, status=4, role=0, type=4, phone_number="tel:+15555550100")
    w.participant(15, 126, 7, status=5, role=9, type=0, identity_id=kim, email="kim@example.com")
    w.participant(16, 126, 7, status=6, role=1, type=1, email="alex2@example.com")
    w.participant(17, 126, 7, status=9, role=1, type=7)
    w.event(126, C_WORK, "Planning", utc(2024, 6, 3, 14), utc(2024, 6, 3, 15), has_attendees=1)

    # 127: a meeting I organised (found through organizer_id, not the first organizer row).
    w.participant(20, 127, 8, type=0, email="other@example.com")
    w.participant(21, 127, 8, type=0, email="me@example.com")
    w.participant(22, 127, 7, status=1, role=3, email="me.alias@example.com")
    w.participant(23, 127, 7, status=1, role=1, email="pat@example.com")
    w.event(127, C_WORK, "Review", utc(2024, 6, 4, 14), utc(2024, 6, 4, 15), organizer_id=21,
            self_attendee_id=22, has_attendees=1)

    # 128: alarms of every shape.
    w.event(128, C_HOME, "Alarms", utc(2024, 4, 1, 9), utc(2024, 4, 1, 10))
    w.alarm(128, trigger_date=utc(2024, 4, 1, 8))
    w.alarm(128)
    w.alarm(128, trigger_interval=300)
    w.alarm(128, trigger_interval=0)
    w.alarm(128, trigger_interval=-600, default_alarm=1)

    # 129: text that isn't valid UTF-8, and notes stored as a BLOB.
    w.event(129, C_HOME, "placeholder", utc(2024, 4, 2, 9), utc(2024, 4, 2, 10),
            description=b"Notes in a blob")
    w.sql("UPDATE CalendarItem SET summary = CAST(X'4869F09F' AS TEXT) WHERE ROWID = 129")

    # 131/132: a flight across zones, and a zone name zoneinfo doesn't know.
    w.event(131, C_WORK, "Flight", utc(2024, 9, 1, 22), utc(2024, 9, 2, 6), end_tz="Europe/London")
    w.event(132, C_WORK, "Odd zone", utc(2024, 9, 3, 10), utc(2024, 9, 3, 11), tz="GMT+0400")

    # 133: an event in a reminder list keeps that list.
    w.event(133, C_CHORES, "Sweep", utc(2024, 9, 4, 14), utc(2024, 9, 4, 15))

    # 134/135: a hidden master and its moved occurrence.
    w.event(134, C_WORK, "Hidden series", utc(2024, 10, 1, 14), utc(2024, 10, 1, 15),
            hidden=1, has_recurrences=1, unique_identifier="hidden-series@example.com")
    w.recurrence(134, 2)
    w.event(135, C_WORK, "Hidden series (moved)", utc(2024, 10, 9, 14), utc(2024, 10, 9, 15),
            unique_identifier="hidden-series@example.com", orig_item_id=134,
            orig_date=utc(2024, 10, 8, 14))


@pytest.fixture(scope="module", params=VERSIONS)
def rich(request, tmp_path_factory):
    version = request.param
    path = _build(tmp_path_factory.mktemp(f"cal-{version}") / "Calendar.sqlitedb", version, _rich)
    columns = {c["name"] for c in load_schema(version, "Calendar.sqlitedb")["columns"]["Calendar"]}
    backup = _backup(path)
    ex = CalendarExtractor()
    return {
        "version": version,
        "backup": backup,
        "calendars": ex.list_calendars(backup),
        "events": ex.list_events(backup),
        "has_subcal_url": "subcal_url" in columns,
    }


def _events(rich) -> dict:
    return {e["id"]: e for e in rich["events"]["events"]}


def _calendars(rich) -> dict:
    return {c["id"]: c for c in rich["calendars"]["calendars"]}


# ----------------------------------------------------------------------
# Calendars
# ----------------------------------------------------------------------

def test_calendars_are_listed_in_store_order_without_scheduling_collections(rich):
    result = rich["calendars"]
    assert "error" not in result and "empty_reason" not in result
    assert [c["id"] for c in result["calendars"]] == [
        C_ORPHAN,                                   # no store row: type NULL sorts first
        C_LOCAL, C_CHORES,                          # local (C_GROCERY: reminders only, no events)
        C_OFFICE,                                   # exchange
        C_HOME, C_WORK,                             # caldav, by display_order
        C_HOLIDAYS, C_FEED,                         # subscribed
        C_BIRTHDAYS, C_FB, C_MAIL, C_SIRI, C_OLD_BIRTHDAYS,  # the "Other" store
        C_ODD,                                      # unknown store type
    ]


def test_calendar_record(rich):
    home = _calendars(rich)[C_HOME]
    assert home == {
        "id": C_HOME, "title": "Home", "color": "#34AADC",
        "account": {"id": 3, "name": "iCloud", "type": "caldav", "disabled": False},
        "kind": "normal", "read_only": False, "subscription_url": None,
        "sharing": "not_shared", "owner_email": "me@example.com",
        "event_count": home["event_count"],
    }


def test_calendar_kinds_from_database_properties(rich):
    kinds = {cid: c["kind"] for cid, c in _calendars(rich).items()}
    assert kinds == {
        C_ORPHAN: "normal", C_LOCAL: "normal", C_CHORES: "normal", C_OFFICE: "normal",
        C_HOME: "normal", C_WORK: "normal", C_HOLIDAYS: "holidays", C_FEED: "subscribed",
        C_BIRTHDAYS: "birthdays", C_FB: "birthdays", C_MAIL: "suggestions",
        C_SIRI: "suggestions",
        # Not named by any property (iOS 13's stale BirthdayCalendarID): the
        # "Other" store is EventKit's birthdays source.
        C_OLD_BIRTHDAYS: "birthdays",
        C_ODD: "normal",
    }


def test_calendar_colours_accounts_sharing_and_flags(rich):
    cals = _calendars(rich)
    assert cals[C_LOCAL]["color"] == "#FF2968"
    assert cals[C_WORK]["color"] == "#1badf8"  # case kept, alpha dropped
    assert cals[C_FEED]["color"] is None
    assert cals[C_LOCAL]["account"] == {"id": 1, "name": "Default", "type": "local", "disabled": False}
    assert cals[C_OFFICE]["account"] == {"id": 5, "name": "Office", "type": "exchange", "disabled": True}
    assert cals[C_HOLIDAYS]["account"]["type"] == "subscribed"
    assert cals[C_BIRTHDAYS]["account"]["type"] == "other"
    assert cals[C_ODD]["account"]["type"] == "unknown"
    assert cals[C_ORPHAN]["account"] is None
    assert [cals[c]["sharing"] for c in (C_HOME, C_WORK, C_OFFICE, C_ORPHAN, C_LOCAL)] == [
        "not_shared", "shared_by_me", "shared_with_me", None, None]
    assert cals[C_HOLIDAYS]["read_only"] and cals[C_FEED]["read_only"] and cals[C_BIRTHDAYS]["read_only"]
    assert not cals[C_HOME]["read_only"] and not cals[C_LOCAL]["read_only"]
    expected_url = "https://calendars.example.com/holidays.ics" if rich["has_subcal_url"] else None
    assert cals[C_HOLIDAYS]["subscription_url"] == expected_url
    assert cals[C_LOCAL]["owner_email"] is None


def test_event_count_agrees_with_list_events(rich):
    ex = CalendarExtractor()
    total = 0
    for cal in rich["calendars"]["calendars"]:
        listed = ex.list_events(rich["backup"], calendar_id=cal["id"])
        assert cal["event_count"] == len(listed["events"]), cal["title"]
        total += cal["event_count"]
    assert total == len(rich["events"]["events"])
    assert _calendars(rich)[C_HOME]["event_count"] > 0
    assert _calendars(rich)[C_LOCAL]["event_count"] == 0
    assert _calendars(rich)[C_CHORES]["event_count"] == 1


# ----------------------------------------------------------------------
# Which events are listed
# ----------------------------------------------------------------------

def test_hidden_junk_phantom_and_reminder_rows(rich):
    result = rich["events"]
    ids = set(_events(rich))
    assert result["hidden_count"] == 3  # 117 hidden, 118 junk, 134 hidden master
    assert not ids & {114, 117, 118, 119, 134}
    everything = CalendarExtractor().list_events(rich["backup"], include_hidden=True)
    all_ids = {e["id"] for e in everything["events"]}
    assert everything["hidden_count"] == 0
    assert all_ids - ids == {117, 118, 134}
    assert 114 not in all_ids and 119 not in all_ids


def test_calendar_filter(rich):
    ex = CalendarExtractor()
    home = ex.list_events(rich["backup"], calendar_id=C_HOME)
    assert {e["calendar_id"] for e in home["events"]} == {C_HOME}
    assert home["hidden_count"] == 2
    assert ex.list_events(rich["backup"], calendar_id=str(C_WORK))["hidden_count"] == 1
    assert ex.list_events(rich["backup"], calendar_id=999) == {
        "events": [], "empty_reason": "no_records", "hidden_count": 0}
    assert ex.list_events(rich["backup"], calendar_id="nope") == {
        "events": [], "empty_reason": "no_records", "hidden_count": 0}
    # Not a calendar id: a float never picks the calendar it rounds to, and a
    # number too large for SQLite is no calendar rather than a damaged backup.
    for wrong in (float(C_HOME), C_HOME + 0.9, 2**63, 2**70, 1e20, "9" * 5000, -C_HOME, True):
        assert ex.list_events(rich["backup"], calendar_id=wrong) == {
            "events": [], "empty_reason": "no_records", "hidden_count": 0}, wrong


def test_events_sorted_by_start_then_id_with_undated_last(rich):
    events = rich["events"]["events"]

    def key(e):
        if e["start"]:
            return datetime.fromisoformat(e["start"]).replace(tzinfo=None)
        if e["wall_start"]:
            return datetime.fromisoformat(e["wall_start"])
        if e["start_day"]:
            return datetime.fromisoformat(e["start_day"])
        return None

    dated = [e for e in events if key(e) is not None]
    assert [e["id"] for e in events[len(dated):]] == [106, 130]
    keys = [(key(e), e["id"]) for e in dated]
    assert keys == sorted(keys)
    assert [e["id"] for e in events[:2]] == [121, 123]  # the 1604 dates sort first


# ----------------------------------------------------------------------
# Dates
# ----------------------------------------------------------------------

def test_zoned_event_record(rich):
    assert _events(rich)[100] == {
        "id": 100, "uid": "dentist@example.com", "calendar_id": C_HOME, "title": "Dentist",
        "notes": "Bring the form; ask about X-rays, too.",
        "url": "https://clinic.example.com/visit", "conference_url": None,
        "all_day": False, "floating": False,
        "start": "2024-01-08T14:00:00+00:00", "end": "2024-01-08T15:00:00+00:00",
        "start_day": None, "end_day": None, "days": None,
        "wall_start": None, "wall_end": None,
        "time_zone": NY, "end_time_zone": None,
        "status": "none", "availability": "busy",
        "location": {"title": "Clinic", "address": "1 Main St", "latitude": 40.7, "longitude": -74.0},
        "recurrence": None, "exceptions": [], "detached_from": None,
        "organizer": {"name": "Pat Doe", "email": "pat@example.com", "is_me": False},
        "attendees": [{"name": None, "email": "me@example.com", "phone": None, "status": "accepted",
                       "role": "required", "type": "person", "is_me": True}],
        "alarms": [{"seconds_before": 900}],
        "year_unknown": False,
        "created_date": "2023-12-01T10:00:00+00:00",
        "modified_date": "2023-12-02T11:00:00+00:00",
    }


def _dates(event) -> dict:
    keys = ("all_day", "floating", "start", "end", "start_day", "end_day", "days",
            "wall_start", "wall_end", "time_zone", "end_time_zone")
    return {k: event[k] for k in keys}


NO_DATES = dict(start=None, end=None, start_day=None, end_day=None, days=None,
                wall_start=None, wall_end=None, time_zone=None, end_time_zone=None)


def test_floating_timed_event_is_a_wall_clock(rich):
    assert _dates(_events(rich)[101]) == {
        **NO_DATES, "all_day": False, "floating": True,
        "wall_start": "2024-02-03T09:30:00", "wall_end": "2024-02-03T10:30:00"}


def test_all_day_floating_event_keeps_its_date(rich):
    assert _dates(_events(rich)[102]) == {
        **NO_DATES, "all_day": True, "floating": True,
        "start_day": "2024-07-04", "end_day": "2024-07-04", "days": 1}


def test_all_day_event_with_a_real_zone_uses_the_date_there(rich):
    assert _dates(_events(rich)[103]) == {
        **NO_DATES, "all_day": True, "floating": False, "time_zone": "Asia/Tokyo",
        "start_day": "2024-03-15", "end_day": "2024-03-15", "days": 1}


def test_multi_day_all_day_event(rich):
    assert _dates(_events(rich)[104]) == {
        **NO_DATES, "all_day": True, "floating": True,
        "start_day": "2024-08-10", "end_day": "2024-08-12", "days": 3}


def test_zero_is_2001_01_01(rich):
    assert _events(rich)[105]["start_day"] == "2001-01-01"


def test_distant_past_and_garbage_dates_are_null(rich):
    for eid in (106, 130):
        assert _dates(_events(rich)[eid]) == {
            **NO_DATES, "all_day": False, "floating": False, "time_zone": NY}


def test_flight_keeps_its_end_zone_and_odd_zones_are_kept_raw(rich):
    flight, odd = _events(rich)[131], _events(rich)[132]
    assert flight["time_zone"] == NY and flight["end_time_zone"] == "Europe/London"
    assert flight["end"] == "2024-09-02T06:00:00+00:00"
    assert odd["time_zone"] == "GMT+0400" and odd["start"] == "2024-09-03T10:00:00+00:00"


@pytest.mark.parametrize("value, expected", [
    (None, None), ("123", None), (b"1", None), (True, None),
    (float("nan"), None), (float("inf"), None), (6.3e10, None), (-6.3e10, None),
    (0, datetime(2001, 1, 1, tzinfo=timezone.utc)),
    (0.0, datetime(2001, 1, 1, tzinfo=timezone.utc)),
    (-86400, datetime(2000, 12, 31, tzinfo=timezone.utc)),
    (6.2e10, EPOCH + timedelta(seconds=6.2e10)),
])
def test_cocoa_dates(value, expected):
    assert _cocoa(value) == expected


# ----------------------------------------------------------------------
# Recurrence
# ----------------------------------------------------------------------

EMPTY_BY = dict(by_day=[], by_month_day=[], by_month=[], by_set_pos=[], by_year_day=[], by_week_no=[])


def test_weekly_series_with_exceptions(rich):
    climb = _events(rich)[107]
    assert climb["recurrence"] == {
        "frequency": "weekly", "interval": 1, "count": None,
        "until": "2024-06-25T22:30:00+00:00",
        **EMPTY_BY, "by_day": ["TU"], "week_start": "SU",
        "last_start": "2024-06-25T22:30:00+00:00",
    }
    assert climb["exceptions"] == ["2024-02-13T23:30:00+00:00", "2024-03-05T23:30:00+00:00"]
    assert climb["detached_from"] is None


def test_detached_occurrence_points_at_its_master(rich):
    moved = _events(rich)[108]
    assert moved["detached_from"] == {"event_id": 107, "original_start": "2024-02-20T23:30:00+00:00"}
    assert moved["recurrence"] is None and moved["uid"] == "climb@example.com"


def test_yearly_all_day_series_is_typed_as_dates(rich):
    midsummer = _events(rich)[109]
    assert midsummer["recurrence"] == {
        "frequency": "yearly", "interval": 1, "count": 6, "until": None,
        **EMPTY_BY, "by_day": ["3SU"], "by_month": [6], "week_start": "SU",
        "last_start": "2029-06-17",
    }
    assert midsummer["exceptions"] == ["2025-06-15"]


def test_monthly_and_daily_series(rich):
    events = _events(rich)
    assert events[110]["recurrence"] == {
        "frequency": "monthly", "interval": 2, "count": None, "until": None,
        **EMPTY_BY, "by_month_day": [1, 15], "week_start": None, "last_start": None,
    }
    assert events[111]["recurrence"]["frequency"] == "daily"
    assert events[111]["recurrence"]["count"] == 10
    assert events[111]["recurrence"]["interval"] == 1


def test_unknown_frequency_has_no_rule(rich):
    assert _events(rich)[112]["recurrence"] is None


def test_every_specifier_token_form(rich):
    tokens = _events(rich)[113]["recurrence"]
    assert tokens == {
        "frequency": "yearly", "interval": 1, "count": None, "until": "2030-01-01T09:00:00",
        "by_day": ["1MO", "-2FR", "WE", "TH"], "by_month_day": [-1], "by_month": [1, 12],
        "by_set_pos": [2], "by_year_day": [100, -1], "by_week_no": [20],
        "week_start": "MO", "last_start": "2029-12-31T09:00:00",
    }


def test_phantom_masters_children_stand_alone(rich):
    events = _events(rich)
    for eid in (115, 116):
        assert events[eid]["detached_from"] is None
        assert events[eid]["uid"] == "series-x@example.com"


def test_child_of_a_hidden_master(rich):
    assert _events(rich)[135]["detached_from"] is None
    everything = CalendarExtractor().list_events(rich["backup"], include_hidden=True)
    child = next(e for e in everything["events"] if e["id"] == 135)
    assert child["detached_from"] == {"event_id": 134, "original_start": "2024-10-08T14:00:00+00:00"}


def _mixed(w: _Writer):
    """Occurrences changed between timed and all-day (not seen in a real backup yet)."""
    w.store(3, "iCloud", 2, disabled=0)
    w.calendar(10, "Home", 3)
    # Mondays 9:00 in New York; the Mar 16 occurrence made all-day.
    w.event(1, 10, "Standup", utc(2026, 3, 9, 13), utc(2026, 3, 9, 13, 30), has_recurrences=1,
            unique_identifier="standup@example.com")
    w.recurrence(1, 2, specifier="D=0MO", count=10, cached_end_date=utc(2026, 5, 11, 13))
    w.all_day(2, 10, "Standup (all day)", datetime(2026, 3, 16),
              unique_identifier="standup@example.com", orig_item_id=1, orig_date=utc(2026, 3, 16, 13))
    # Tuesdays 08:00 in Tokyo (Monday 23:00 UTC); the Mar 17 occurrence made all-day.
    w.event(3, 10, "Tokyo call", utc(2026, 3, 9, 23), utc(2026, 3, 10, 0), tz="Asia/Tokyo",
            has_recurrences=1, unique_identifier="tokyo@example.com")
    w.recurrence(3, 2, specifier="D=0TU", count=10, cached_end_date=utc(2026, 5, 11, 23))
    w.all_day(4, 10, "Tokyo call (all day)", datetime(2026, 3, 17),
              unique_identifier="tokyo@example.com", orig_item_id=3, orig_date=utc(2026, 3, 16, 23))
    # An all-day Friday series; the Mar 20 occurrence given a time in New York.
    w.all_day(5, 10, "Market day", datetime(2026, 3, 13), has_recurrences=1,
              unique_identifier="market@example.com")
    w.recurrence(5, 2, specifier="D=0FR", count=10, cached_end_date=utc(2026, 5, 15))
    w.event(6, 10, "Market day (timed)", utc(2026, 3, 20, 14), utc(2026, 3, 20, 16),
            unique_identifier="market@example.com", orig_item_id=5, orig_date=utc(2026, 3, 20))


def test_original_start_is_typed_like_the_series_start(tmp_path):
    path = _build(tmp_path / "c.sqlitedb", LATEST, _mixed)
    events = {e["id"]: e for e in CalendarExtractor().list_events(_backup(path))["events"]}
    assert events[2]["start_day"] == "2026-03-16"
    assert events[2]["detached_from"] == {"event_id": 1, "original_start": "2026-03-16T13:00:00+00:00"}
    assert events[4]["detached_from"] == {"event_id": 3, "original_start": "2026-03-16T23:00:00+00:00"}
    assert events[6]["start"] == "2026-03-20T14:00:00+00:00"
    assert events[6]["detached_from"] == {"event_id": 5, "original_start": "2026-03-20"}

    ics = events_to_ics(list(events.values()), now=NOW)
    assert "RECURRENCE-ID;TZID=America/New_York:20260316T090000" in _vevent(ics, "Standup (all day)")
    assert "RECURRENCE-ID;TZID=Asia/Tokyo:20260317T080000" in _vevent(ics, "Tokyo call (all day)")
    assert "RECURRENCE-ID;VALUE=DATE:20260320" in _vevent(ics, "Market day (timed)")


# ----------------------------------------------------------------------
# Other fields
# ----------------------------------------------------------------------

def test_status_and_availability(rich):
    events = _events(rich)
    assert (events[120]["status"], events[120]["availability"]) == ("cancelled", "unavailable")
    assert (events[102]["status"], events[102]["availability"]) == ("none", "free")


def test_unknown_birth_years(rich):
    events = _events(rich)
    assert events[121]["year_unknown"] is True and events[121]["start_day"] == "1604-03-02"
    assert events[122]["year_unknown"] is False
    assert events[123]["year_unknown"] is False  # 1604, but not a birthdays calendar


def test_locations(rich):
    events = _events(rich)
    assert events[124]["location"] == {"title": "Somewhere", "address": None,
                                       "latitude": None, "longitude": None}
    assert events[125]["location"] is None
    assert events[101]["location"] is None


def test_people_without_contacts(rich):
    meeting = _events(rich)[126]
    assert meeting["organizer"] == {"name": "Robin Vale", "email": "boss@example.com", "is_me": False}
    assert meeting["attendees"] == [
        {"name": "Lee Park", "email": None, "phone": None, "status": "needs_action",
         "role": "optional", "type": "person", "is_me": False},
        {"name": None, "email": "sam@example.com", "phone": None, "status": "declined",
         "role": "chair", "type": "room", "is_me": False},
        {"name": None, "email": "ME@Example.com", "phone": None, "status": "tentative",
         "role": "non_participant", "type": "resource", "is_me": True},
        {"name": None, "email": None, "phone": "+15555550100", "status": "delegated",
         "role": "unknown", "type": "group", "is_me": False},
        {"name": "Kim", "email": "kim@example.com", "phone": None, "status": "completed",
         "role": "unknown", "type": "unknown", "is_me": False},
        {"name": None, "email": "alex2@example.com", "phone": None, "status": "in_process",
         "role": "required", "type": "person", "is_me": False},
        {"name": None, "email": None, "phone": None, "status": "unknown",
         "role": "required", "type": "unknown", "is_me": False},
    ]
    # An iCloud principal path is never an email address.
    for event in rich["events"]["events"]:
        for person in event["attendees"] + ([event["organizer"]] if event["organizer"] else []):
            assert not person["email"] or "/" not in person["email"]


def test_people_named_from_contacts(rich):
    contacts = {"sam@example.com": "Sam Example", "+15555550100": "Dana Phone",
                "alex2@example.com": "Alex Two", "me@example.com": "Me Myself",
                "kim@example.com": "Kimberly Long"}
    result = CalendarExtractor().list_events(rich["backup"], calendar_id=C_WORK, contacts=contacts)
    meeting = next(e for e in result["events"] if e["id"] == 126)
    assert [a["name"] for a in meeting["attendees"]] == [
        "Lee Park", "Sam Example", "Me Myself", "Dana Phone", "Kim", "Alex Two", None]
    assert meeting["organizer"]["name"] == "Robin Vale"


def test_invitee_emails_are_never_matched_by_their_digits(tmp_path):
    def fill(w):
        w.store(3, "iCloud", 2, disabled=0)
        w.calendar(10, "Home", 3)
        w.participant(1, 1, 8, type=0, email="mailto:mike86@gmail.com")
        w.participant(2, 1, 7, status=1, email="mailto:mike86@gmail.com")
        w.participant(3, 1, 7, status=1, email="x.551234567@y.org")
        w.participant(4, 1, 7, status=1, email="Bob7@Example.com")
        w.event(1, 10, "Lunch", utc(2024, 6, 3, 16), utc(2024, 6, 3, 17), organizer_id=1, has_attendees=1)
    path = _build(tmp_path / "c.sqlitedb", LATEST, fill)
    # As ContactResolver.load_contacts stores them: a carrier's "*86" voicemail
    # number under its bare digits, phones under a 9-digit suffix, emails as
    # saved and lower-cased.
    contacts = {"*86": "Voicemail", "86": "Voicemail", "+186": "Voicemail",
                "+33 5 51 23 45 67": "Phone Owner", "~551234567": "Phone Owner",
                "bob7@example.com": "Bob Seven"}
    [lunch] = CalendarExtractor().list_events(_backup(path), contacts=contacts)["events"]
    assert lunch["organizer"]["name"] is None
    assert [(a["name"], a["email"]) for a in lunch["attendees"]] == [
        (None, "mike86@gmail.com"), (None, "x.551234567@y.org"), ("Bob Seven", "Bob7@Example.com")]
    description = next(line for line in _vevent(events_to_ics([lunch], now=NOW), "Lunch")
                       if line.startswith("DESCRIPTION:"))
    assert description == ("DESCRIPTION:Organizer: mike86@gmail.com\\nInvitees: mike86@gmail.com (accepted)\\, "
                           "x.551234567@y.org (accepted)\\, Bob Seven <Bob7@Example.com> (accepted)")


def test_organizer_found_by_id_and_me_by_self_attendee(rich):
    review = _events(rich)[127]
    assert review["organizer"] == {"name": None, "email": "me@example.com", "is_me": True}
    assert [(a["email"], a["is_me"]) for a in review["attendees"]] == [
        ("me.alias@example.com", True), ("pat@example.com", False)]


def test_alarms(rich):
    assert _events(rich)[128]["alarms"] == [
        {"at": "2024-04-01T08:00:00+00:00"}, {"seconds_before": -300}, {"seconds_before": 0}]


def test_invalid_utf8_and_blob_text(rich):
    event = _events(rich)[129]
    assert event["title"].startswith("Hi") and "�" in event["title"]
    assert event["notes"] == "Notes in a blob"


# ----------------------------------------------------------------------
# Empty, damaged and unusual databases
# ----------------------------------------------------------------------

def test_not_in_backup():
    ex = CalendarExtractor()
    assert ex.list_calendars(_FakeBackup()) == {"calendars": [], "empty_reason": "not_in_backup"}
    assert ex.list_events(_FakeBackup()) == {"events": [], "empty_reason": "not_in_backup"}


def test_only_extras_or_another_domain_is_not_in_backup(tmp_path):
    path = _build(tmp_path / "c.sqlitedb", LATEST, _rich)
    backup = _FakeBackup({
        (CALENDAR_DOMAIN, "Library/Calendar/Extras.db"): str(path),
        ("AppDomain-com.example.app", CALENDAR_DB_PATH): str(path),
    })
    assert CalendarExtractor().list_events(backup) == {"events": [], "empty_reason": "not_in_backup"}


def test_listed_but_unreadable_is_an_error():
    backup = _FakeBackup(unreadable={(CALENDAR_DOMAIN, CALENDAR_DB_PATH)})
    for result, key in ((CalendarExtractor().list_calendars(backup), "calendars"),
                        (CalendarExtractor().list_events(backup), "events")):
        assert result[key] == []
        assert result["error"] == ("Calendar is in this backup but could not be read. "
                                   "The backup may be incomplete or damaged.")
        assert "empty_reason" not in result


def test_corrupt_file_is_an_error(tmp_path):
    bad = tmp_path / "Calendar.sqlitedb"
    bad.write_bytes(b"not a database" * 100)
    result = CalendarExtractor().list_events(_backup(bad))
    assert result["events"] == []
    assert result["error"].startswith("Calendar is in this backup but could not be read. ")
    assert result["error"].endswith(")") and "empty_reason" not in result


def test_a_failing_query_is_an_error(tmp_path, monkeypatch):
    path = _build(tmp_path / "c.sqlitedb", LATEST, _rich)

    def boom(*args, **kwargs):
        raise sqlite3.DatabaseError("disk image is malformed")

    monkeypatch.setattr(CalendarExtractor, "_event_rows", boom)
    for key, result in (("events", CalendarExtractor().list_events(_backup(path))),
                        ("calendars", CalendarExtractor().list_calendars(_backup(path)))):
        assert result == {key: [], "error": (
            "Calendar is in this backup but could not be read. The backup may be "
            "incomplete or damaged. (disk image is malformed)")}


@pytest.mark.parametrize("version", VERSIONS)
def test_empty_database(tmp_path, version):
    path = _build(tmp_path / "c.sqlitedb", version, lambda w: None)
    ex = CalendarExtractor()
    assert ex.list_events(_backup(path)) == {"events": [], "empty_reason": "no_records", "hidden_count": 0}
    assert ex.list_calendars(_backup(path)) == {"calendars": [], "empty_reason": "no_records"}


def test_calendars_without_events(tmp_path):
    def fill(w):
        w.store(1, "Default", 0)
        w.calendar(1, "Personal", 1)
        w.calendar(2, "Inbox", 1, external_id="/9/calendars/inbox/")
    path = _build(tmp_path / "c.sqlitedb", LATEST, fill)
    ex = CalendarExtractor()
    assert [(c["id"], c["event_count"]) for c in ex.list_calendars(_backup(path))["calendars"]] == [(1, 0)]
    assert ex.list_events(_backup(path)) == {"events": [], "empty_reason": "no_records", "hidden_count": 0}


def test_apples_placeholder_title_reads_as_calendar(tmp_path):
    def fill(w):
        w.store(1, "Default", 0)
        w.calendar(1, "Default", 1, supported_entity_types=None)
        w.calendar(2, "DEFAULT_CALENDAR_NAME", 1, supported_entity_types=4)
        w.calendar(3, "DEFAULT_TASK_CALENDAR_NAME", 1, supported_entity_types=8)
        w.event(1, 2, "Haircut", utc(2020, 2, 3, 15), utc(2020, 2, 3, 16))
    path = _build(tmp_path / "c.sqlitedb", VERSIONS[0], fill)
    ex = CalendarExtractor()
    calendars = ex.list_calendars(_backup(path))["calendars"]
    assert [(c["id"], c["title"]) for c in calendars] == [(1, "Default"), (2, "Calendar")]
    ics = events_to_ics(ex.list_events(_backup(path), calendar_id=2)["events"], [calendars[1]], now=NOW)
    assert "X-WR-CALNAME:Calendar" in _unfold(ics)


def test_only_scheduling_collections_is_no_records(tmp_path):
    def fill(w):
        w.store(1, "iCloud", 2)
        w.calendar(1, "Inbox", 1, external_id="/9/calendars/inbox/")
        w.calendar(2, "Notifications", 1, external_id="/9/calendars/notification/")
    path = _build(tmp_path / "c.sqlitedb", LATEST, fill)
    assert CalendarExtractor().list_calendars(_backup(path)) == {"calendars": [], "empty_reason": "no_records"}


def test_every_event_hidden(tmp_path):
    def fill(w):
        w.calendar(1, "Home", 1)
        w.event(1, 1, "a", utc(2024, 1, 1), utc(2024, 1, 1, 1), hidden=1)
        w.event(2, 1, "b", utc(2024, 1, 2), utc(2024, 1, 2, 1), junk_status=1)
    path = _build(tmp_path / "c.sqlitedb", LATEST, fill)
    assert CalendarExtractor().list_events(_backup(path)) == {
        "events": [], "empty_reason": "no_records", "hidden_count": 2}
    assert CalendarExtractor().list_calendars(_backup(path))["calendars"][0]["event_count"] == 0


def _raw_db(path: Path, *statements) -> Path:
    conn = sqlite3.connect(path)
    for statement in statements:
        conn.execute(statement)
    conn.commit()
    conn.close()
    return path


def test_database_without_tables_is_no_records(tmp_path):
    path = _raw_db(tmp_path / "c.sqlitedb", "CREATE TABLE t (x)", "DROP TABLE t")
    ex = CalendarExtractor()
    assert ex.list_events(_backup(path)) == {"events": [], "empty_reason": "no_records", "hidden_count": 0}
    assert ex.list_calendars(_backup(path)) == {"calendars": [], "empty_reason": "no_records"}


@pytest.mark.parametrize("tables", [
    ("CREATE TABLE Event (ROWID INTEGER PRIMARY KEY, summary TEXT, start_date REAL)",),
    ("CREATE TABLE Calendar (ROWID INTEGER PRIMARY KEY, title TEXT)",
     "CREATE TABLE Event (ROWID INTEGER PRIMARY KEY, summary TEXT, calendar_id INTEGER)"),
])
def test_older_layout_is_unsupported(tmp_path, tables):
    path = _raw_db(tmp_path / "c.sqlitedb", *tables)
    message = "This backup's calendar is in an older format OpenExtract can't read yet."
    ex = CalendarExtractor()
    assert ex.list_events(_backup(path)) == {
        "events": [], "error": message, "empty_reason": "unsupported_format"}
    assert ex.list_calendars(_backup(path)) == {
        "calendars": [], "error": message, "empty_reason": "unsupported_format"}


def test_bare_schema_reads_missing_columns_as_none(tmp_path):
    path = _raw_db(
        tmp_path / "c.sqlitedb",
        "CREATE TABLE Calendar (title TEXT)",
        "CREATE TABLE CalendarItem (summary TEXT, start_date REAL, end_date REAL, calendar_id INTEGER)",
        "INSERT INTO Calendar (ROWID, title) VALUES (7, 'Only one')",
        f"INSERT INTO CalendarItem (ROWID, summary, start_date, end_date, calendar_id) "
        f"VALUES (3, 'Bare', {utc(2024, 1, 1, 12)}, {utc(2024, 1, 1, 13)}, 7)",
        "CREATE TABLE Recurrence (owner_id INTEGER, frequency INTEGER)",
        "INSERT INTO Recurrence VALUES (3, 2)",
    )
    ex = CalendarExtractor()
    assert ex.list_calendars(_backup(path)) == {"calendars": [{
        "id": 7, "title": "Only one", "color": None, "account": None, "kind": "normal",
        "read_only": False, "subscription_url": None, "sharing": None, "owner_email": None,
        "event_count": 1}]}
    result = ex.list_events(_backup(path))
    assert result["hidden_count"] == 0
    [event] = result["events"]
    assert event["id"] == 3 and event["title"] == "Bare" and event["uid"] is None
    assert _dates(event) == {**NO_DATES, "all_day": False, "floating": False,
                             "start": "2024-01-01T12:00:00+00:00", "end": "2024-01-01T13:00:00+00:00"}
    assert event["status"] == "none" and event["availability"] is None
    assert event["recurrence"]["frequency"] == "weekly"  # no has_recurrences column: the row decides
    assert event["organizer"] is None and event["attendees"] == [] and event["alarms"] == []


def test_found_through_the_manifest(tmp_path):
    canonical = _build(tmp_path / "a.sqlitedb", LATEST, _rich)
    other = _build(tmp_path / "b.sqlitedb", LATEST, lambda w: (
        w.calendar(1, "Elsewhere", 1), w.event(1, 1, "x", utc(2024, 1, 1), utc(2024, 1, 1, 1))))
    elsewhere = "Library/Calendar/Copy/Calendar.sqlitedb"
    ex = CalendarExtractor()
    only_other = _FakeBackup({(CALENDAR_DOMAIN, elsewhere): str(other)})
    assert [c["title"] for c in ex.list_calendars(only_other)["calendars"]] == ["Elsewhere"]
    both = _FakeBackup({(CALENDAR_DOMAIN, elsewhere): str(other),
                        (CALENDAR_DOMAIN, CALENDAR_DB_PATH): str(canonical)})
    assert "Home" in [c["title"] for c in ex.list_calendars(both)["calendars"]]


def test_reads_through_a_real_backup(tmp_path):
    data = _build(tmp_path / "c.sqlitedb", LATEST, _rich).read_bytes()
    device = Device(name="Test iPhone", product_type="iPhone15,4", product_name="iPhone 15",
                    ios="17.3", build="21D50", phone_number="+44 7700 900001")
    writer = BackupWriter(tmp_path / "out", device, datetime(2024, 11, 1, tzinfo=timezone.utc),
                          "17.3", seed=1)
    writer.add_file(CALENDAR_DOMAIN, CALENDAR_DB_PATH, data)
    writer.add_file(CALENDAR_DOMAIN, "Library/Calendar/Extras.db", b"not this one")
    accessor = open_local_backup(str(writer.finalize()))
    ex = CalendarExtractor()
    calendars = ex.list_calendars(accessor)["calendars"]
    events = ex.list_events(accessor)
    assert sum(c["event_count"] for c in calendars) == len(events["events"])
    assert events["hidden_count"] == 3


# ----------------------------------------------------------------------
# recurrence_rrule
# ----------------------------------------------------------------------

def _rule(**kw):
    return {"frequency": "weekly", "interval": 1, "count": None, "until": None, **EMPTY_BY,
            "week_start": None, "last_start": None, **kw}


@pytest.mark.parametrize("rule, kind, expected", [
    (_rule(), "zoned", "FREQ=WEEKLY"),
    (_rule(frequency="daily", interval=3), "zoned", "FREQ=DAILY;INTERVAL=3"),
    (_rule(count=5, until="2024-06-01T00:00:00+00:00"), "zoned", "FREQ=WEEKLY;COUNT=5"),
    (_rule(until="2024-06-25T22:30:00+00:00"), "zoned", "FREQ=WEEKLY;UNTIL=20240625T223000Z"),
    (_rule(until="2024-06-25"), "all_day", "FREQ=WEEKLY;UNTIL=20240625"),
    (_rule(until="2024-06-25T18:30:00"), "floating", "FREQ=WEEKLY;UNTIL=20240625T183000"),
    (_rule(until="2024-06-25T22:30:00+00:00"), "all_day", "FREQ=WEEKLY;UNTIL=20240625"),
    (_rule(until="2024-06-25"), "zoned", "FREQ=WEEKLY;UNTIL=20240625T235959Z"),
    (_rule(frequency="monthly", by_day=["-1FR"], by_set_pos=[-1]), "zoned",
     "FREQ=MONTHLY;BYDAY=-1FR;BYSETPOS=-1"),
    (_rule(frequency="yearly", by_day=["3SU"], by_month=[6], week_start="SU"), "all_day",
     "FREQ=YEARLY;BYDAY=3SU;BYMONTH=6;WKST=SU"),
    (_rule(frequency="yearly", by_month_day=[1, -1], by_year_day=[100], by_week_no=[20]), "zoned",
     "FREQ=YEARLY;BYMONTHDAY=1,-1;BYYEARDAY=100;BYWEEKNO=20"),
    (_rule(by_day=["TU", "bad", 3]), "zoned", "FREQ=WEEKLY;BYDAY=TU"),
    (_rule(frequency="hourly"), "zoned", ""),
    (None, "zoned", ""),
])
def test_recurrence_rrule(rule, kind, expected):
    assert recurrence_rrule(rule, kind) == expected


# ----------------------------------------------------------------------
# events_to_ics
# ----------------------------------------------------------------------

def _unfold(ics: str) -> list[str]:
    return ics.replace("\r\n ", "").split("\r\n")[:-1]


def _components(ics: str, name: str) -> list[list[str]]:
    found, current = [], None
    for line in _unfold(ics):
        if line == f"BEGIN:{name}":
            current = []
        elif line == f"END:{name}" and current is not None:
            found.append(current)
            current = None
        elif current is not None:
            current.append(line)
    return found


def _vevent(ics: str, summary: str) -> list[str]:
    return next(v for v in _components(ics, "VEVENT") if f"SUMMARY:{summary}" in v)


@pytest.fixture(scope="module")
def latest(tmp_path_factory):
    path = _build(tmp_path_factory.mktemp("ics") / "Calendar.sqlitedb", LATEST, _rich)
    backup = _backup(path)
    ex = CalendarExtractor()
    events = ex.list_events(backup)["events"]
    calendars = ex.list_calendars(backup)["calendars"]
    return {"events": events, "calendars": calendars,
            "ics": events_to_ics(events, calendars, now=NOW)}


def test_ics_wrapper_line_endings_and_folding(latest):
    ics = latest["ics"]
    assert ics.startswith("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//OpenExtract//Calendar//EN\r\n"
                          "CALSCALE:GREGORIAN\r\nMETHOD:PUBLISH\r\n")
    assert ics.endswith("END:VCALENDAR\r\n")
    assert "\n" not in ics.replace("\r\n", "")
    for line in ics.split("\r\n"):
        assert len(line.encode("utf-8")) <= 75
    assert "X-WR-CALNAME" not in ics  # more than one calendar
    assert ics == events_to_ics(latest["events"], latest["calendars"], now=NOW)  # deterministic
    dated = [e for e in latest["events"] if e["start"] or e["wall_start"] or e["start_day"]]
    assert ics.count("BEGIN:VEVENT") == len(dated)
    assert "Someday" not in ics and "Garbled" not in ics


def test_ics_has_no_organizer_or_attendee_properties(latest):
    for line in _unfold(latest["ics"]):
        assert not line.startswith(("ORGANIZER", "ATTENDEE"))


def test_ics_zoned_event(latest):
    dentist = _vevent(latest["ics"], "Dentist")
    assert dentist[:4] == ["UID:dentist@example.com", "DTSTAMP:20231202T110000Z",
                           "DTSTART;TZID=America/New_York:20240108T090000",
                           "DTEND;TZID=America/New_York:20240108T100000"]
    assert ("DESCRIPTION:Bring the form\\; ask about X-rays\\, too.\\n\\n"
            "Organizer: Pat Doe <pat@example.com>\\nInvitees: me@example.com (accepted)") in dentist
    assert "LOCATION:Clinic\\, 1 Main St" in dentist
    assert "GEO:40.7;-74" in dentist
    assert "URL:https://clinic.example.com/visit" in dentist
    assert "TRANSP:OPAQUE" in dentist
    assert "CREATED:20231201T100000Z" in dentist and "LAST-MODIFIED:20231202T110000Z" in dentist
    assert not any(line.startswith("STATUS") for line in dentist)
    assert dentist[-5:] == ["BEGIN:VALARM", "ACTION:DISPLAY", "DESCRIPTION:Reminder",
                            "TRIGGER:-PT15M", "END:VALARM"]


def test_ics_floating_and_all_day_events(latest):
    ics = latest["ics"]
    plumber = _vevent(ics, "Call the plumber")
    assert "DTSTART:20240203T093000" in plumber and "DTEND:20240203T103000" in plumber
    july = _vevent(ics, "Independence Day")
    assert "DTSTART;VALUE=DATE:20240704" in july and "DTEND;VALUE=DATE:20240705" in july
    assert "TRANSP:TRANSPARENT" in july
    trip = _vevent(ics, "Lake trip")
    assert "DTSTART;VALUE=DATE:20240810" in trip and "DTEND;VALUE=DATE:20240813" in trip
    tokyo = _vevent(ics, "Office closed")
    assert "DTSTART;VALUE=DATE:20240315" in tokyo and "DTEND;VALUE=DATE:20240316" in tokyo
    assert "DTSTART;VALUE=DATE:20010101" in _vevent(ics, "Millennium party")


def test_ics_unknown_zone_is_written_in_utc(latest):
    odd = _vevent(latest["ics"], "Odd zone")
    assert "DTSTART:20240903T100000Z" in odd and "DTEND:20240903T110000Z" in odd
    assert "TZID:GMT+0400" not in latest["ics"]


def test_ics_flight_uses_each_end_zone(latest):
    flight = _vevent(latest["ics"], "Flight")
    assert "DTSTART;TZID=America/New_York:20240901T180000" in flight
    assert "DTEND;TZID=Europe/London:20240902T070000" in flight
    assert "TZID:Europe/London" in _unfold(latest["ics"])


def test_ics_series_exceptions_and_moved_occurrence(latest):
    ics = latest["ics"]
    climb = _vevent(ics, "Climbing")
    assert "DTSTART;TZID=America/New_York:20240109T183000" in climb
    assert "RRULE:FREQ=WEEKLY;UNTIL=20240625T223000Z;BYDAY=TU;WKST=SU" in climb
    assert "EXDATE;TZID=America/New_York:20240213T183000,20240305T183000" in climb
    moved = _vevent(ics, "Climbing (moved)")
    assert "UID:climb@example.com" in climb and "UID:climb@example.com" in moved
    assert "RECURRENCE-ID;TZID=America/New_York:20240220T183000" in moved
    assert not any(line.startswith(("RRULE", "EXDATE")) for line in moved)

    midsummer = _vevent(ics, "Midsummer")
    assert "RRULE:FREQ=YEARLY;COUNT=6;BYDAY=3SU;BYMONTH=6;WKST=SU" in midsummer
    assert "EXDATE;VALUE=DATE:20250615" in midsummer
    assert "RRULE:FREQ=MONTHLY;INTERVAL=2;BYMONTHDAY=1,15" in _vevent(ics, "Rent")
    assert "RRULE:FREQ=DAILY;COUNT=10;WKST=SU" in _vevent(ics, "Standup")
    tokens = _vevent(ics, "Tokens")
    assert ("RRULE:FREQ=YEARLY;UNTIL=20300101T090000;BYDAY=1MO,-2FR,WE,TH;BYMONTHDAY=-1;"
            "BYMONTH=1,12;BYYEARDAY=100,-1;BYWEEKNO=20;BYSETPOS=2;WKST=MO") in tokens
    assert not any(line.startswith("RRULE") for line in _vevent(ics, "Mystery"))


def test_ics_orphaned_occurrences_get_their_own_uids(latest):
    ics = latest["ics"]
    uids = [line for v in _components(ics, "VEVENT") for line in v if line.startswith("UID:")]
    book_club = [v for v in _components(ics, "VEVENT") if "SUMMARY:Book club" in v]
    assert len(book_club) == 2
    assert {next(x for x in v if x.startswith("UID:")) for v in book_club} == {
        "UID:series-x@example.com", "UID:series-x@example.com-116"}
    assert not any(line.startswith("RECURRENCE-ID") for v in book_club for line in v)
    # Only a series and its moved occurrences share a UID.
    shared = {u for u in uids if uids.count(u) > 1}
    assert shared == {"UID:climb@example.com"}

    # A moved occurrence exported without its series stands alone.
    moved = next(e for e in latest["events"] if e["id"] == 108)
    alone = events_to_ics([moved], now=NOW)
    assert "UID:climb@example.com-108" in _unfold(alone)
    assert "RECURRENCE-ID" not in alone


def test_ics_alarms_status_and_calendar_name(latest):
    ics = latest["ics"]
    alarms = _vevent(ics, "Alarms")
    assert [line for line in alarms if line.startswith("TRIGGER")] == [
        "TRIGGER;VALUE=DATE-TIME:20240401T080000Z", "TRIGGER:PT5M", "TRIGGER:PT0S"]
    assert "STATUS:CANCELLED" in _vevent(ics, "Offsite")
    home = next(c for c in latest["calendars"] if c["id"] == C_HOME)
    single = events_to_ics([e for e in latest["events"] if e["calendar_id"] == C_HOME], [home], now=NOW)
    assert "X-WR-CALNAME:Home" in _unfold(single)
    # No modified date: DTSTAMP is the export time.
    assert "DTSTAMP:20260102T030405Z" in _vevent(ics, "Call the plumber")


def test_ics_people_in_description(latest):
    planning = _vevent(latest["ics"], "Planning")
    description = next(line for line in planning if line.startswith("DESCRIPTION:"))
    assert description == (
        "DESCRIPTION:Organizer: Robin Vale <boss@example.com>\\nInvitees: Lee Park (needs action)\\, "
        "sam@example.com (declined)\\, ME@Example.com (tentative)\\, +15555550100 (delegated)\\, "
        "Kim <kim@example.com> (completed)\\, alex2@example.com (in process)")


def _event(**kw):
    base = {
        "id": 1, "uid": "u1@example.com", "calendar_id": 1, "title": "T", "notes": None,
        "url": None, "conference_url": None, "all_day": False, "floating": False,
        "start": "2024-05-01T12:00:00+00:00", "end": "2024-05-01T13:00:00+00:00",
        "start_day": None, "end_day": None, "days": None, "wall_start": None, "wall_end": None,
        "time_zone": NY, "end_time_zone": None, "status": "none", "availability": "busy",
        "location": None, "recurrence": None, "exceptions": [], "detached_from": None,
        "organizer": None, "attendees": [], "alarms": [], "year_unknown": False,
        "created_date": None, "modified_date": None,
    }
    base.update(kw)
    return base


def test_ics_text_escaping_and_folding_keeps_utf8_whole():
    notes = "a;b,c\\d\nnext\r\nline\x07 " + "é" * 60 + " 🙂" * 20
    ics = events_to_ics([_event(notes=notes, title="Ünïcødé, title; ok")], now=NOW)
    raw_lines = ics.split("\r\n")
    for line in raw_lines:
        assert len(line.encode("utf-8")) <= 75
        line.encode("utf-8").decode("utf-8")  # each physical line is whole UTF-8
    assert any(line.startswith(" ") for line in raw_lines)
    vevent = _components(ics, "VEVENT")[0]
    description = next(line for line in vevent if line.startswith("DESCRIPTION:"))
    assert description == ("DESCRIPTION:a\\;b\\,c\\\\d\\nnext\\nline " + "é" * 60 + " 🙂" * 20)
    assert "SUMMARY:Ünïcødé\\, title\\; ok" in vevent


def test_ics_location_dedupe_and_conference_link():
    ics = events_to_ics([
        _event(id=1, title="A", location={"title": "Cafe", "address": "Cafe\n2 High St",
                                         "latitude": None, "longitude": None},
               conference_url="https://meet.example.com/x"),
        _event(id=2, uid="u2", title="B", location={"title": "Cafe", "address": "Cafe",
                                                   "latitude": 1.5, "longitude": None}),
    ], now=NOW)
    a, b = _vevent(ics, "A"), _vevent(ics, "B")
    assert "LOCATION:Cafe\\n2 High St" in a and "URL:https://meet.example.com/x" in a
    assert "LOCATION:Cafe" in b and not any(line.startswith("GEO") for line in b)


def test_ics_vtimezone_for_new_york_2024():
    ics = events_to_ics([_event()], now=NOW)
    [zone] = _components(ics, "VTIMEZONE")
    assert zone[0] == "TZID:America/New_York"
    text = "\n".join(zone)
    assert ("BEGIN:DAYLIGHT\nDTSTART:20240310T020000\nTZOFFSETFROM:-0500\nTZOFFSETTO:-0400\n"
            "TZNAME:EDT\nEND:DAYLIGHT") in text
    assert ("BEGIN:STANDARD\nDTSTART:20241103T020000\nTZOFFSETFROM:-0400\nTZOFFSETTO:-0500\n"
            "TZNAME:EST\nEND:STANDARD") in text
    # The range is the event's year with one year either side.
    starts = [line for line in zone if line.startswith("DTSTART:")]
    assert starts[0].startswith("DTSTART:2023") and starts[-1].startswith("DTSTART:2025")
    assert ics.index("BEGIN:VTIMEZONE") < ics.index("BEGIN:VEVENT")


def test_zone_changes_found_to_the_minute():
    changes = _zone_changes(NY, 2024, 2024)
    assert [c[0] for c in changes] == [datetime(2024, 3, 10, 7, tzinfo=timezone.utc),
                                       datetime(2024, 11, 3, 6, tzinfo=timezone.utc)]
    assert [(c[1], c[2]) for c in changes] == [(timedelta(hours=-5), timedelta(hours=-4)),
                                              (timedelta(hours=-4), timedelta(hours=-5))]


def test_ics_vtimezone_for_a_zone_without_changes():
    ics = events_to_ics([_event(time_zone="Asia/Kolkata")], now=NOW)
    [zone] = _components(ics, "VTIMEZONE")
    assert zone == ["TZID:Asia/Kolkata", "BEGIN:STANDARD", "DTSTART:19700101T000000",
                    "TZOFFSETFROM:+0530", "TZOFFSETTO:+0530", "TZNAME:IST", "END:STANDARD"]
    assert "DTSTART;TZID=Asia/Kolkata:20240501T173000" in _unfold(ics)


def _last_observance(ics: str) -> str:
    return [line for line in _components(ics, "VTIMEZONE")[0] if line.startswith("DTSTART:")][-1]


def test_ics_vtimezone_reaches_ten_years_past_the_export_for_open_ended_series():
    # A lesson every Tuesday since 2014: clients that read the file's VTIMEZONE
    # must still get this year's and the coming years' summer offsets.
    since_2014 = _event(start="2014-07-08T13:00:00+00:00", end=None, recurrence=_rule(by_day=["TU"]))
    ics = events_to_ics([since_2014], now=NOW)
    assert _last_observance(ics) == "DTSTART:20361102T020000"  # NOW is 2026
    assert "DTSTART:20260308T020000" in _components(ics, "VTIMEZONE")[0]
    # Starting after the export: ten years past its own start, with no 2037 cap.
    from_2034 = events_to_ics([_event(start="2034-05-01T12:00:00+00:00", end=None, recurrence=_rule())],
                              now=NOW)
    assert _last_observance(from_2034).startswith("DTSTART:2044")
    # The same series exported later reaches further: the range follows ``now``.
    later = events_to_ics([since_2014], now=datetime(2030, 6, 1, tzinfo=timezone.utc))
    assert _last_observance(later).startswith("DTSTART:2040")


def test_ics_vtimezone_for_a_series_with_an_end_stops_near_it():
    counted = _event(start="2014-07-08T13:00:00+00:00", end=None, recurrence=_rule(count=5))
    assert _last_observance(events_to_ics([counted], now=NOW)).startswith("DTSTART:2015")
    until = _event(start="2014-07-08T13:00:00+00:00", end=None,
                   recurrence=_rule(until="2016-07-05T13:00:00+00:00"))
    assert _last_observance(events_to_ics([until], now=NOW)).startswith("DTSTART:2017")


def test_ics_floating_until_and_all_day_exdate_typing():
    floating = _event(start=None, end=None, floating=True, time_zone=None,
                      wall_start="2024-01-01T09:00:00", wall_end="2024-01-01T08:00:00",
                      recurrence=_rule(until="2024-03-01T09:00:00"),
                      exceptions=["2024-01-08T09:00:00"])
    all_day = _event(id=2, uid="u2", title="D", start=None, end=None, all_day=True, floating=True,
                     time_zone=None, start_day="2024-01-01", end_day=None, days=1,
                     recurrence=_rule(until="2024-03-01"), exceptions=["2024-01-08"])
    ics = events_to_ics([floating, all_day], now=NOW)
    f, d = _vevent(ics, "T"), _vevent(ics, "D")
    assert "DTSTART:20240101T090000" in f and not any(x.startswith("DTEND") for x in f)
    assert "RRULE:FREQ=WEEKLY;UNTIL=20240301T090000" in f and "EXDATE:20240108T090000" in f
    assert "DTEND;VALUE=DATE:20240102" in d
    assert "RRULE:FREQ=WEEKLY;UNTIL=20240301" in d and "EXDATE;VALUE=DATE:20240108" in d
    assert "VTIMEZONE" not in ics


@pytest.mark.parametrize("seconds_before, expected", [
    (900, "-PT15M"), (0, "PT0S"), (-300, "PT5M"), (86400, "-P1D"), (90000, "-P1DT1H"),
    (3605, "-PT1H0M5S"), (5400, "-PT1H30M"), (45, "-PT45S"), (172800 + 61, "-P2DT1M1S"),
])
def test_alarm_durations(seconds_before, expected):
    assert _ics_duration(seconds_before) == expected
