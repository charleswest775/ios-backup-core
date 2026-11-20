"""
Calendar extraction from Calendar.sqlitedb, and export to iCalendar (.ics).

The database is ``HomeDomain`` ``Library/Calendar/Calendar.sqlitedb`` on every
version checked: iOS 13.3.1, 13.4.1, 15.3.1, 16.1.2 and 17.3 (public Hickman
backups) and the iOS 26.5 table definitions from the Simulator. It is found
through the manifest, preferring that path; ``Extras.db`` (alarm-fire state)
and the iOS 16+ ``AuxDatabase`` per-account databases are not read.

What a backup holds:

- ``Store``: one row per account (local, iCloud and other CalDAV accounts,
  Exchange, the "Subscribed Calendars" store, and the "Other" store that
  holds Birthdays and the Siri suggestion calendars).
- ``Calendar``: one row per calendar, plus each CalDAV account's scheduling
  inbox/outbox and notification collections, which are left out here. Local
  reminder lists can still appear; a calendar that can't hold events and has
  none is left out too.
- ``CalendarItem``: events (``entity_type = 2``; reminders moved to their own
  database in iOS 13). A repeating event is one row, the series master, with
  a ``Recurrence`` row; an occurrence that was moved or edited is its own row
  pointing back at the master (``orig_item_id``/``orig_date``), and a deleted
  occurrence is an ``ExceptionDate`` row. Series are returned as rules, never
  expanded into occurrences.
- ``Participant``/``Identity`` (organizer and invitees), ``Alarm``,
  ``Location``.

How dates are stored:

- Seconds since 2001-01-01 UTC, as REAL. 0 is a real date (2001-01-01), and
  NSDate's distantPast/distantFuture (about ±6.3e10) mean "no date".
- A timed event with a zone name (``start_tz``, an IANA name such as
  ``America/New_York``) is a real instant. ``_float`` means no zone: the
  value is the wall clock written as if it were UTC, so it must never be
  shifted. All-day events are ``_float`` midnight of the first day, ending at
  23:59:59 of the last day (inclusive), in every checked backup. An all-day
  event with a real zone (possible on Exchange) is read as its date in that
  zone; that case is not checked against a real backup yet.
- Unknown zone names (including odd forms like ``GMT+0400``) fall back to
  UTC; the raw name is kept in ``time_zone``.

What is checked against real data and what isn't:

- Checked on the Hickman backups: event, all-day and recurring-master counts;
  yearly rules (frequency 4) and their ``specifier`` BY-parts, which expand
  to exactly the dates in Apple's own OccurrenceCache; ``cached_end_date`` as
  the start of the last occurrence; organizer/attendee rows; iOS 16+
  ``default_alarm`` placeholder rows.
- From third-party parsers (iLEAPP, icalPal and others), not checked against
  a real backup yet: frequency codes 1 daily, 2 weekly, 3 monthly; ``status``
  (3 = cancelled) and ``availability`` codes; Participant ``status`` in
  iCalendar PARTSTAT order; ``sharing_status`` codes; birthdays stored with
  the year 1604 when the year is unknown.
- Inferred: ``read_only`` from ``Calendar.flags & 1`` (every read-only
  calendar seen has it, writable ones don't); week_start 1..7 = Monday..Sunday.
- Not in any checked backup: floating timed events, multi-day all-day
  events, detached occurrences, exception dates, phantom masters, hidden or
  junk events, Exchange and local events.

On iOS 13 the Birthdays calendar is not the one ``BirthdayCalendarID``
names (that row no longer exists), so a calendar in the "Other" store
(``Store.type`` 5, EventKit's birthdays source) that no property names is
treated as a birthdays calendar too.
"""

import math
import re
import sqlite3
import sys
import traceback
from collections import Counter
from collections.abc import Iterable
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

from ios_backup_core.contacts import resolve_contact
from ios_backup_core.db import connect_readonly, get_database

CALENDAR_DOMAIN = "HomeDomain"
CALENDAR_DB_PATH = "Library/Calendar/Calendar.sqlitedb"
FLOATING_TZ = "_float"

_NOT_READABLE = (
    "Calendar is in this backup but could not be read. "
    "The backup may be incomplete or damaged."
)
_UNSUPPORTED = "This backup's calendar is in an older format OpenExtract can't read yet."

_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)
# NSDate distantPast / distantFuture are about ±6.3e10 seconds from 2001.
_DISTANT = 6.3e10
_MAX_ROWID = 2**63 - 1

_STORE_TYPES = {0: "local", 1: "exchange", 2: "caldav", 3: "mobileme",
                4: "subscribed", 5: "other", 6: "reminders"}
_SUBSCRIBED_STORE, _BIRTHDAYS_STORE = 4, 5
_SHARING = {0: "not_shared", 1: "shared_by_me", 2: "shared_with_me"}
_STATUS = {0: "none", 1: "confirmed", 2: "tentative", 3: "cancelled"}
_AVAILABILITY = {0: "busy", 1: "free", 2: "tentative", 3: "unavailable"}
# Participant.status follows iCalendar PARTSTAT order, not EKParticipantStatus.
_PARTSTAT = {0: "needs_action", 1: "accepted", 2: "declined", 3: "tentative",
             4: "delegated", 5: "completed", 6: "in_process"}
_ROLES = {0: "unknown", 1: "required", 2: "optional", 3: "chair", 4: "non_participant"}
_PARTICIPANT_TYPES = {0: "unknown", 1: "person", 2: "room", 3: "resource", 4: "group"}
_FREQUENCIES = {1: "daily", 2: "weekly", 3: "monthly", 4: "yearly"}
_WEEKDAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")

_EVENT_ENTITY = 2
_ATTENDEE, _ORGANIZER = 7, 8
_EVENTS_BIT = 1 << _EVENT_ENTITY  # supported_entity_types is a 1 << entity_type mask
_HOLIDAY_EXTERNAL_ID = "__HolidayCalendar__"
_SCHEDULING_SUFFIXES = ("/inbox/", "/outbox/", "/notification/")
_BIRTHDAY_PROPS = ("BirthdayCalendarID", "FacebookBirthdayCalendarID")
_SUGGESTION_PROPS = ("SuggestionsCalendarID", "NaturalLanguageSuggestionsCalendarID")
# Apple's untranslated placeholder for the local calendar (seen on iOS 13.3.1 and
# 13.4.1), which the Calendar app shows as "Calendar".
_PLACEHOLDER_TITLES = {"DEFAULT_CALENDAR_NAME": "Calendar"}
_UNKNOWN_YEAR = 1604

_COLOR = re.compile(r"#([0-9A-Fa-f]{6})(?:[0-9A-Fa-f]{2})?")
_EMAIL = re.compile(r"[^\s@/:<>]+@[^\s@/:<>]+")
_BYDAY = re.compile(r"([+-]?\d{1,2})?(MO|TU|WE|TH|FR|SA|SU)")
# BY-part key in Apple's specifier → (record key, smallest and largest absolute value).
_SPECIFIER_PARTS = {
    "M": ("by_month_day", 1, 31),
    "O": ("by_month", 1, 12),
    "S": ("by_set_pos", 1, 366),
    "Y": ("by_year_day", 1, 366),
    "W": ("by_week_no", 1, 53),
}


# ----------------------------------------------------------------------
# Value helpers: they never raise.
# ----------------------------------------------------------------------

def _text(value) -> str | None:
    """Return a TEXT value as str. Some rows store text columns as bytes."""
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray, memoryview)):
        value = bytes(value).decode("utf-8", errors="replace")
    value = str(value).strip()
    return value or None


def _int(value) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _row_id(value) -> int | None:
    """An id a caller asked for: an int or a string of digits that fits a ROWID.

    None for anything else (booleans, floats, negative or too-large numbers),
    so 1.9 never picks calendar 1 and a huge number never reaches SQLite.
    """
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    number = _int(value)
    return number if number is not None and 0 <= number <= _MAX_ROWID else None


def _float(value) -> float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) or math.isinf(number) else number


def _flag(value) -> bool:
    return bool(_int(value))


def _cocoa(value) -> datetime | None:
    """A Calendar date (seconds since 2001-01-01 UTC) as an aware UTC datetime.

    None for NULL, anything that isn't a number, NaN and NSDate's
    distantPast/distantFuture. 0 is a real date: 2001-01-01.
    """
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    seconds = float(value)
    if math.isnan(seconds) or abs(seconds) >= _DISTANT:
        return None
    try:
        return _EPOCH + timedelta(seconds=seconds)
    except OverflowError:
        return None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


@lru_cache(maxsize=256)
def _zone(name: str | None):
    """ZoneInfo for an IANA name, or None (``_float``, unknown or odd names)."""
    if not name or name == FLOATING_TZ:
        return None
    try:
        return ZoneInfo(name)
    except Exception:
        return None


def _when(value: datetime | None, kind: str, zone) -> str | None:
    """Format an instant the way the event's start is typed.

    all_day → "YYYY-MM-DD" (the date in the event's zone, UTC for floating),
    floating → naive "YYYY-MM-DDTHH:MM:SS", zoned → ISO UTC.
    """
    if value is None:
        return None
    if kind == "all_day":
        return (value.astimezone(zone) if zone is not None else value).date().isoformat()
    if kind == "floating":
        return value.replace(tzinfo=None).isoformat(timespec="seconds")
    return value.isoformat()


def _color(value) -> str | None:
    """"#RRGGBBAA" → "#RRGGBB" (case kept); anything else → None."""
    text = _text(value)
    match = _COLOR.fullmatch(text) if text else None
    return f"#{match.group(1)}" if match else None


def _email(value) -> str | None:
    """A plain email address, from "x@y" or "mailto:x@y". Never a principal path."""
    text = _text(value)
    if not text:
        return None
    if text.lower().startswith("mailto:"):
        text = text[len("mailto:"):].strip()
    return text if _EMAIL.fullmatch(text) else None


def _phone(value) -> str | None:
    text = _text(value)
    if text and text.lower().startswith("tel:"):
        text = text[len("tel:"):].strip() or None
    return text


def _columns(cursor, table: str) -> set:
    cursor.execute(f'PRAGMA table_info("{table}")')
    return {row[1] for row in cursor.fetchall()}


def _select(columns: set, wanted: Iterable[str], table_alias: str = "", prefix: str = "") -> str:
    """SELECT list with the columns that exist; missing ones read as NULL.

    ROWID is always selected: it exists whether or not the table declares it.
    ``prefix`` is added to the output names (for joined tables).
    """
    source = f"{table_alias}." if table_alias else ""
    parts = []
    for name in wanted:
        if name == "ROWID" or name in columns:
            parts.append(f'{source}"{name}" AS "{prefix}{name}"' if name != "ROWID"
                         else f'{source}ROWID AS "{prefix}ROWID"')
        else:
            parts.append(f'NULL AS "{prefix}{name}"')
    return ", ".join(parts)


def _log(message: str) -> None:
    print(f"[calendar] {message}", file=sys.stderr, flush=True)


# ----------------------------------------------------------------------
# Extractor
# ----------------------------------------------------------------------

_CALENDAR_COLUMNS = (
    "ROWID", "store_id", "title", "color", "display_order", "external_id",
    "supported_entity_types", "flags", "sharing_status", "self_identity_email",
    "subcal_url",
)
_STORE_COLUMNS = ("ROWID", "name", "type", "disabled")

_EVENT_COLUMNS = (
    "ROWID", "UUID", "unique_identifier", "calendar_id", "summary", "description",
    "start_date", "start_tz", "end_date", "end_tz", "all_day", "status",
    "availability", "url", "conference_url", "has_recurrences", "orig_item_id",
    "orig_date", "organizer_id", "self_attendee_id", "creation_date",
    "last_modified", "hidden", "junk_status", "phantom_master", "location_id",
)


class CalendarExtractor:
    """Extracts calendars and events from Calendar.sqlitedb."""

    CALENDAR_DB_PATH = CALENDAR_DB_PATH

    # -- opening -------------------------------------------------------

    def _find_database(self, backup):
        """Return (domain, path) of Calendar.sqlitedb in the manifest, or None."""
        try:
            entries = backup.list_files(path_like="%Calendar.sqlitedb") or []
        except Exception:
            entries = []
        entries = [
            e for e in entries
            if (e.get("domain") or CALENDAR_DOMAIN) == CALENDAR_DOMAIN
            and (e.get("path") or "").endswith("Calendar.sqlitedb")
        ]
        entries.sort(key=lambda e: (e.get("path") != CALENDAR_DB_PATH, e.get("path") or ""))
        if entries:
            return CALENDAR_DOMAIN, entries[0]["path"]
        return None

    def _open(self, backup, result_key: str):
        """Open the database. Returns (conn, None) or (None, empty_or_error_result)."""
        found = self._find_database(backup)
        if not found:
            return None, {result_key: [], "empty_reason": "not_in_backup"}
        domain, path = found
        try:
            db_path = get_database(backup, path, domain=domain)
        except Exception as e:
            _log(f"couldn't extract {path}: {e}")
            db_path = None
        if not db_path:
            return None, {result_key: [], "error": _NOT_READABLE}
        try:
            conn = connect_readonly(db_path, backup)
        except sqlite3.Error as e:
            return None, {result_key: [], "error": f"{_NOT_READABLE} ({e})"}
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA temp_store = MEMORY")
        # Titles, notes and names come from invitations and feeds: one value
        # that isn't valid UTF-8 must not fail the whole calendar.
        conn.text_factory = lambda raw: raw.decode("utf-8", "replace")
        return conn, None

    @staticmethod
    def _layout(cursor) -> tuple[set, str]:
        """(table names, "ok" | "empty" | "unsupported")."""
        tables = {r[0] for r in cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "CalendarItem" in tables:
            return tables, "ok"
        if any(not t.startswith("sqlite_") for t in tables):
            # iOS 4 and earlier kept events in an "Event" table.
            return tables, "unsupported"
        return tables, "empty"

    # -- calendars -----------------------------------------------------

    @staticmethod
    def _properties(cursor, tables) -> dict:
        if "_SqliteDatabaseProperties" not in tables:
            return {}
        try:
            return {
                _text(row["key"]): _text(row["value"])
                for row in cursor.execute("SELECT key, value FROM _SqliteDatabaseProperties")
            }
        except sqlite3.Error as e:
            _log(f"couldn't read database properties: {e}")
            return {}

    def _calendar_rows(self, cursor, tables) -> list[dict]:
        """Every Calendar row with its store and kind, in display order."""
        if "Calendar" not in tables:
            return []
        props = self._properties(cursor, tables)

        def ids(keys):
            return {n for n in (_int(props.get(k)) for k in keys) if n is not None}

        birthday_ids, suggestion_ids = ids(_BIRTHDAY_PROPS), ids(_SUGGESTION_PROPS)

        cal_cols = _columns(cursor, "Calendar")
        select = _select(cal_cols, _CALENDAR_COLUMNS, "c")
        join = ""
        if "Store" in tables and "store_id" in cal_cols:
            store_cols = _columns(cursor, "Store")
            select += ", " + _select(store_cols, _STORE_COLUMNS, "s", prefix="store_")
            join = " LEFT JOIN Store s ON s.ROWID = c.store_id"
        else:
            select += ", " + ", ".join(f'NULL AS "store_{n}"' for n in _STORE_COLUMNS)
        rows = cursor.execute(
            f"SELECT {select} FROM Calendar c{join} "
            'ORDER BY "store_type", "store_ROWID", "display_order", "ROWID"'
        ).fetchall()

        calendars = []
        for row in rows:
            cid = _int(row["ROWID"])
            store_type = _int(row["store_type"])
            has_store = row["store_ROWID"] is not None
            external_id = _text(row["external_id"]) or ""
            if external_id == _HOLIDAY_EXTERNAL_ID:
                kind = "holidays"
            elif cid in birthday_ids:
                kind = "birthdays"
            elif cid in suggestion_ids:
                kind = "suggestions"
            elif store_type == _SUBSCRIBED_STORE:
                kind = "subscribed"
            elif store_type == _BIRTHDAYS_STORE:
                kind = "birthdays"
            else:
                kind = "normal"
            flags = _int(row["flags"]) or 0
            title = _text(row["title"])
            calendars.append({
                "id": cid,
                "title": _PLACEHOLDER_TITLES.get(title, title),
                "color": _color(row["color"]),
                "account": {
                    "id": _int(row["store_ROWID"]),
                    "name": _text(row["store_name"]),
                    "type": _STORE_TYPES.get(store_type, "unknown"),
                    "disabled": _flag(row["store_disabled"]),
                } if has_store else None,
                "kind": kind,
                "read_only": bool(flags & 1),
                "subscription_url": _text(row["subcal_url"]),
                "sharing": _SHARING.get(_int(row["sharing_status"])),
                "owner_email": _text(row["self_identity_email"]),
                "_scheduling": external_id.lower().endswith(_SCHEDULING_SUFFIXES),
                "_supported": _int(row["supported_entity_types"]),
            })
        return calendars

    def list_calendars(self, backup) -> dict:
        """List the calendars, with how many events each has.

        Returns ``{"calendars": [...]}``. Each calendar has its ``account``
        (the store: ``id``, ``name``, ``type`` and ``disabled``; None when
        the store row is missing) and a ``kind``: ``"holidays"`` (Apple's
        built-in holiday subscription), ``"birthdays"``, ``"suggestions"``
        (Found in Mail / Siri), ``"subscribed"`` or ``"normal"``.
        ``event_count`` is exactly what ``list_events(calendar_id=id)``
        returns by default.

        Left out: each CalDAV account's scheduling inbox/outbox and
        notification collections, and lists that can only hold reminders
        and have no events. Apple's untranslated placeholder title
        ``DEFAULT_CALENDAR_NAME`` (iOS 13) reads as "Calendar", as the
        Calendar app shows it.

        Empty results: ``"not_in_backup"`` (no Calendar.sqlitedb),
        ``"no_records"`` (no calendars), ``"unsupported_format"`` with an
        ``error`` (an older layout). A database that is listed but can't be
        read is an ``error`` with no ``empty_reason``.
        """
        conn, early = self._open(backup, "calendars")
        if early is not None:
            return early
        try:
            cursor = conn.cursor()
            tables, layout = self._layout(cursor)
            if layout == "unsupported":
                return {"calendars": [], "error": _UNSUPPORTED, "empty_reason": "unsupported_format"}
            if layout == "empty":
                return {"calendars": [], "empty_reason": "no_records"}

            rows, _ = self._event_rows(cursor, tables)
            counts = Counter(_int(r["calendar_id"]) for r in rows)
            item_cols = _columns(cursor, "CalendarItem")
            any_events = Counter()
            if "calendar_id" in item_cols:
                where = f" WHERE entity_type = {_EVENT_ENTITY}" if "entity_type" in item_cols else ""
                for row in cursor.execute(
                        f"SELECT calendar_id, COUNT(*) FROM CalendarItem{where} GROUP BY calendar_id"):
                    any_events[_int(row[0])] = row[1]

            calendars = []
            for cal in self._calendar_rows(cursor, tables):
                supported = cal.pop("_supported")
                if cal.pop("_scheduling"):
                    continue
                if (supported is not None and not supported & _EVENTS_BIT
                        and not any_events.get(cal["id"])):
                    continue
                cal["event_count"] = counts.get(cal["id"], 0)
                calendars.append(cal)
        except Exception as e:
            traceback.print_exc()
            return {"calendars": [], "error": f"{_NOT_READABLE} ({e})"}
        finally:
            conn.close()

        if not calendars:
            return {"calendars": [], "empty_reason": "no_records"}
        return {"calendars": calendars}

    # -- events --------------------------------------------------------

    def _event_rows(self, cursor, tables, calendar_id: int | None = None,
                    include_hidden: bool = False) -> tuple[list, int]:
        """(event rows to list, number left out as hidden or junk)."""
        cols = _columns(cursor, "CalendarItem")
        where, params = [], []
        if "entity_type" in cols:
            where.append(f"entity_type = {_EVENT_ENTITY}")
        if calendar_id is not None:
            if "calendar_id" not in cols:
                return [], 0
            where.append("calendar_id = ?")
            params.append(calendar_id)
        sql = f"SELECT {_select(cols, _EVENT_COLUMNS)} FROM CalendarItem"
        if where:
            sql += " WHERE " + " AND ".join(where)
        kept, hidden = [], 0
        for row in cursor.execute(sql + " ORDER BY ROWID", params):
            if _flag(row["phantom_master"]):
                continue  # a placeholder master: its occurrences are listed on their own
            if not include_hidden and (_flag(row["hidden"]) or (_int(row["junk_status"]) or 0) > 0):
                hidden += 1
                continue
            kept.append(row)
        return kept, hidden

    def _side_table(self, cursor, tables, table, wanted, owner, order=()):
        """Rows of a related table grouped by owner id. A table that fails is empty."""
        grouped: dict = {}
        if table not in tables:
            return grouped
        try:
            cols = _columns(cursor, table)
            if owner not in cols:
                return grouped
            sort = ", ".join([f'"{c}"' for c in order if c in cols] + ["ROWID"])
            sql = f'SELECT {_select(cols, wanted)} FROM "{table}" ORDER BY {sort}'
            for row in cursor.execute(sql):
                grouped.setdefault(_int(row[owner]), []).append(row)
        except sqlite3.Error as e:
            _log(f"couldn't read {table}: {e}")
            return {}
        return grouped

    def _participants(self, cursor, tables) -> list:
        if "Participant" not in tables:
            return []
        try:
            cols = _columns(cursor, "Participant")
            select = _select(cols, ("ROWID", "entity_type", "type", "status", "role",
                                    "owner_id", "email", "phone_number"), "p")
            identity = ("display_name", "address", "first_name", "last_name")
            join = ""
            if "Identity" in tables and "identity_id" in cols:
                select += ", " + _select(_columns(cursor, "Identity"), identity, "i")
                join = " LEFT JOIN Identity i ON i.ROWID = p.identity_id"
            else:
                select += ", " + ", ".join(f'NULL AS "{n}"' for n in identity)
            where = " WHERE p.entity_type IN (7, 8)" if "entity_type" in cols else ""
            return cursor.execute(
                f"SELECT {select} FROM Participant p{join}{where} ORDER BY p.ROWID").fetchall()
        except sqlite3.Error as e:
            _log(f"couldn't read Participant: {e}")
            return []

    def _locations(self, cursor, tables) -> dict:
        if "Location" not in tables:
            return {}
        try:
            cols = _columns(cursor, "Location")
            rows = cursor.execute(
                f"SELECT {_select(cols, ('ROWID', 'title', 'address', 'latitude', 'longitude'))} "
                "FROM Location").fetchall()
            return {_int(r["ROWID"]): r for r in rows}
        except sqlite3.Error as e:
            _log(f"couldn't read Location: {e}")
            return {}

    def list_events(self, backup, calendar_id: int | None = None, include_hidden: bool = False,
                    contacts: dict | None = None) -> dict:
        """List events, series as one row each, by start.

        Returns ``{"events": [...], "hidden_count": n}``. ``calendar_id``
        (an int or a string of digits) limits the list to one calendar; any
        other value, such as a float or a number too large to be a calendar
        id, lists nothing (``"no_records"``). Events marked hidden or junk are
        left out unless ``include_hidden``; ``hidden_count`` says how many
        were. ``contacts`` (from ContactResolver) only names people who have
        no name of their own.

        Dates never mix types in one field:

        - timed event with a zone: ``start``/``end`` ISO UTC, ``time_zone``
          the event's zone name (``end_time_zone`` only when the end's
          differs);
        - floating timed event (no zone): ``wall_start``/``wall_end`` naive
          "YYYY-MM-DDTHH:MM:SS", ``floating`` true;
        - all-day: ``start_day``/``end_day`` "YYYY-MM-DD", the last day
          inclusive, and ``days``.

        Exception dates and ``recurrence.until``/``last_start`` are typed like
        the event's start. ``detached_from.original_start`` is typed like its
        series' start (the event ``detached_from.event_id``), which differs
        from the occurrence's own when one occurrence of a timed series was
        made all-day or the other way round (not seen in a real backup yet).

        Empty results: ``"not_in_backup"``, ``"no_records"`` (with
        ``hidden_count``), ``"unsupported_format"`` with an ``error``. A
        database that is listed but can't be read is an ``error`` with no
        ``empty_reason``.
        """
        conn, early = self._open(backup, "events")
        if early is not None:
            return early
        try:
            cursor = conn.cursor()
            tables, layout = self._layout(cursor)
            if layout == "unsupported":
                return {"events": [], "error": _UNSUPPORTED, "empty_reason": "unsupported_format"}
            if layout == "empty":
                return {"events": [], "empty_reason": "no_records", "hidden_count": 0}
            wanted = None
            if calendar_id is not None:
                wanted = _row_id(calendar_id)
                if wanted is None:
                    return {"events": [], "empty_reason": "no_records", "hidden_count": 0}
            rows, hidden = self._event_rows(cursor, tables, wanted, include_hidden)
            events = self._build_events(cursor, tables, rows, contacts) if rows else []
        except Exception as e:
            traceback.print_exc()
            return {"events": [], "error": f"{_NOT_READABLE} ({e})"}
        finally:
            conn.close()

        if not events:
            return {"events": [], "empty_reason": "no_records", "hidden_count": hidden}
        return {"events": events, "hidden_count": hidden}

    def _build_events(self, cursor, tables, rows, contacts) -> list[dict]:
        try:
            calendars = {c["id"]: c for c in self._calendar_rows(cursor, tables)}
        except sqlite3.Error as e:
            _log(f"couldn't read Calendar: {e}")
            calendars = {}
        locations = self._locations(cursor, tables)
        alarms = self._side_table(
            cursor, tables, "Alarm",
            ("ROWID", "calendaritem_owner_id", "trigger_interval", "trigger_date", "default_alarm"),
            "calendaritem_owner_id")
        recurrences = self._side_table(
            cursor, tables, "Recurrence",
            ("ROWID", "owner_id", "frequency", "interval", "week_start", "count",
             "end_date", "specifier", "cached_end_date"),
            "owner_id")
        exceptions = self._side_table(
            cursor, tables, "ExceptionDate", ("ROWID", "owner_id", "date"), "owner_id",
            order=("date",))
        attendees: dict = {}
        organizers: dict = {}
        organizer_rows: dict = {}
        for p in self._participants(cursor, tables):
            entity = _int(p["entity_type"])
            owner = _int(p["owner_id"])
            if entity == _ORGANIZER:
                organizer_rows[_int(p["ROWID"])] = p
                organizers.setdefault(owner, p)
            elif entity == _ATTENDEE:
                attendees.setdefault(owner, []).append(p)

        has_recurrence_column = "has_recurrences" in _columns(cursor, "CalendarItem")
        listed = {_int(r["ROWID"]) for r in rows}
        # How each listed row's start is typed: a moved occurrence's original start
        # is typed like its series' start, which can differ from its own.
        kinds = {_int(r["ROWID"]): self._kind_zone(r) for r in rows}
        events = []
        for row in rows:
            eid = _int(row["ROWID"])
            calendar = calendars.get(_int(row["calendar_id"])) or {}
            owner_email = calendar.get("owner_email")
            me = (_int(row["self_attendee_id"]) or 0, owner_email)

            organizer_id = _int(row["organizer_id"]) or 0
            organizer_row = organizer_rows.get(organizer_id) if organizer_id > 0 else None
            if organizer_row is None:
                organizer_row = organizers.get(eid)

            event = self._event(row, calendar)
            kind, zone = event.pop("_kind"), event.pop("_zone")
            event["exceptions"] = [
                v for v in (_when(_cocoa(x["date"]), kind, zone) for x in exceptions.get(eid, []))
                if v is not None
            ]
            orig_item = _int(row["orig_item_id"]) or 0
            if orig_item > 0 and orig_item != eid and orig_item in listed:
                series_kind, series_zone = kinds[orig_item]
                event["detached_from"] = {
                    "event_id": orig_item,
                    "original_start": _when(_cocoa(row["orig_date"]), series_kind, series_zone),
                }
            rec_rows = recurrences.get(eid)
            if rec_rows and (_flag(row["has_recurrences"]) or not has_recurrence_column):
                event["recurrence"] = _recurrence(rec_rows[0], kind, zone)
            event["organizer"] = (_person(organizer_row, contacts, me, attendee=False)
                                  if organizer_row is not None else None)
            event["attendees"] = [_person(p, contacts, me, attendee=True)
                                  for p in attendees.get(eid, [])]
            event["alarms"] = _alarms(alarms.get(eid, []))
            location_id = _int(row["location_id"]) or 0
            location = locations.get(location_id) if location_id > 0 else None
            event["location"] = _location(location) if location is not None else None
            events.append(event)

        # Re-insert keys in the documented order.
        order = ("id", "uid", "calendar_id", "title", "notes", "url", "conference_url",
                 "all_day", "floating", "start", "end", "start_day", "end_day", "days",
                 "wall_start", "wall_end", "time_zone", "end_time_zone", "status",
                 "availability", "location", "recurrence", "exceptions", "detached_from",
                 "organizer", "attendees", "alarms", "year_unknown", "created_date",
                 "modified_date")
        events.sort(key=lambda e: (e["_sort"] is None, e["_sort"] or datetime.min, e["id"] or 0))
        return [{k: e[k] for k in order} for e in events]

    @staticmethod
    def _kind_zone(row) -> tuple[str, object]:
        """(how the row's start is typed: "all_day" | "floating" | "zoned", its ZoneInfo or None)."""
        start_tz = _text(row["start_tz"])
        floating = start_tz == FLOATING_TZ
        kind = "all_day" if _flag(row["all_day"]) else ("floating" if floating else "zoned")
        return kind, None if floating else _zone(start_tz)

    @staticmethod
    def _event(row, calendar: dict) -> dict:
        """The date fields and plain columns of one event."""
        start_tz, end_tz = _text(row["start_tz"]), _text(row["end_tz"])
        all_day = _flag(row["all_day"])
        floating = start_tz == FLOATING_TZ
        kind, zone = CalendarExtractor._kind_zone(row)
        start, end = _cocoa(row["start_date"]), _cocoa(row["end_date"])

        event = {
            "id": _int(row["ROWID"]),
            "uid": _text(row["unique_identifier"]) or _text(row["UUID"]),
            "calendar_id": _int(row["calendar_id"]),
            "title": _text(row["summary"]),
            "notes": _text(row["description"]),
            "url": _text(row["url"]),
            "conference_url": _text(row["conference_url"]),
            "all_day": all_day,
            "floating": floating,
            "start": None, "end": None,
            "start_day": None, "end_day": None, "days": None,
            "wall_start": None, "wall_end": None,
            "time_zone": None, "end_time_zone": None,
            "status": _STATUS.get(_int(row["status"]), "none"),
            "availability": _AVAILABILITY.get(_int(row["availability"])),
            "location": None, "recurrence": None, "exceptions": [], "detached_from": None,
            "organizer": None, "attendees": [], "alarms": [],
            "year_unknown": False,
            "created_date": _iso(_cocoa(row["creation_date"])),
            "modified_date": _iso(_cocoa(row["last_modified"])),
            "_kind": kind, "_zone": zone, "_sort": None,
        }
        if kind == "all_day":
            if start is not None:
                first = date.fromisoformat(_when(start, kind, zone))
                days = 1
                if end is not None:
                    days = max(1, round(((end - start).total_seconds() + 1) / 86400))
                event["start_day"] = first.isoformat()
                event["days"] = days
                event["end_day"] = (first + timedelta(days=days - 1)).isoformat()
                event["_sort"] = datetime(first.year, first.month, first.day)
            if not floating:
                event["time_zone"] = start_tz
        elif kind == "floating":
            event["wall_start"] = _when(start, kind, None)
            event["wall_end"] = _when(end, kind, None)
            if start is not None:
                event["_sort"] = start.replace(tzinfo=None)
        else:
            event["start"] = _iso(start)
            event["end"] = _iso(end)
            event["time_zone"] = start_tz
            event["end_time_zone"] = end_tz if end_tz != start_tz else None
            if start is not None:
                event["_sort"] = start.replace(tzinfo=None)
        if calendar.get("kind") == "birthdays" and event["_sort"] is not None:
            event["year_unknown"] = event["_sort"].year == _UNKNOWN_YEAR
        return event


def _recurrence(row, kind: str, zone) -> dict | None:
    frequency = _FREQUENCIES.get(_int(row["frequency"]))
    if frequency is None:
        return None
    interval = _int(row["interval"])
    count = _int(row["count"])
    rule = {
        "frequency": frequency,
        "interval": interval if interval and interval > 0 else 1,
        "count": count if count and count > 0 else None,
        "until": _when(_cocoa(row["end_date"]), kind, zone),
        "by_day": [], "by_month_day": [], "by_month": [], "by_set_pos": [],
        "by_year_day": [], "by_week_no": [],
        "week_start": None,
        "last_start": _when(_cocoa(row["cached_end_date"]), kind, zone),
    }
    rule.update(_parse_specifier(_text(row["specifier"])))
    week_start = _int(row["week_start"])
    if week_start is not None and 1 <= week_start <= 7:
        rule["week_start"] = _WEEKDAYS[week_start - 1]
    order = ("frequency", "interval", "count", "until", "by_day", "by_month_day", "by_month",
             "by_set_pos", "by_year_day", "by_week_no", "week_start", "last_start")
    return {k: rule[k] for k in order}


def _parse_specifier(specifier: str | None) -> dict:
    """Apple's BY-parts ("D=+3SU;O=6") as record lists. Unknown parts are ignored."""
    parsed: dict = {}
    for part in (specifier or "").split(";"):
        key, _, values = part.partition("=")
        key = key.strip().upper()
        tokens = [t.strip() for t in values.split(",") if t.strip()]
        if key == "D":
            days = []
            for token in tokens:
                match = _BYDAY.fullmatch(token.upper())
                if not match:
                    continue
                number = int(match.group(1)) if match.group(1) else 0
                days.append(f"{number}{match.group(2)}" if number else match.group(2))
            parsed["by_day"] = days
        elif key in _SPECIFIER_PARTS:
            name, low, high = _SPECIFIER_PARTS[key]
            numbers = []
            for token in tokens:
                number = _int(token)
                if number is not None and low <= abs(number) <= high:
                    numbers.append(number)
            parsed[name] = numbers
    return parsed


def _person(p, contacts: dict | None, me: tuple, *, attendee: bool) -> dict:
    """An organizer or attendee. ``me`` is (self_attendee_id, the calendar's owner email)."""
    self_attendee, owner_email = me
    name = (_text(p["display_name"])
            or " ".join(x for x in (_text(p["first_name"]), _text(p["last_name"])) if x)
            or None)
    email = _email(p["email"]) or _email(p["address"])
    phone = _phone(p["phone_number"])
    if not name and contacts:
        if email:
            # Only the address itself: never phone matching on the digits in an email.
            name = contacts.get(email) or contacts.get(email.lower())
        elif phone:
            name = resolve_contact(phone, contacts)
        name = name or None
    is_me = bool((self_attendee > 0 and _int(p["ROWID"]) == self_attendee)
                 or (email and owner_email and email.lower() == owner_email.lower()))
    record = {"name": name, "email": email}
    if attendee:
        record.update({
            "phone": phone,
            "status": _PARTSTAT.get(_int(p["status"]), "unknown"),
            "role": _ROLES.get(_int(p["role"]), "unknown"),
            "type": _PARTICIPANT_TYPES.get(_int(p["type"]), "unknown"),
        })
    record["is_me"] = is_me
    return record


def _alarms(rows) -> list[dict]:
    alarms = []
    for row in rows:
        if _flag(row["default_alarm"]):
            continue  # iOS 16+ adds one placeholder per event
        interval = _float(row["trigger_interval"])
        if interval is not None:
            alarms.append({"seconds_before": int(-interval) if interval else 0})
            continue
        at = _cocoa(row["trigger_date"])
        if at is not None:
            alarms.append({"at": at.isoformat()})
    return alarms


def _location(row) -> dict | None:
    title, address = _text(row["title"]), _text(row["address"])
    if not title and not address:
        return None
    latitude, longitude = _float(row["latitude"]), _float(row["longitude"])
    if latitude is None or longitude is None or (latitude == 0 and longitude == 0):
        latitude = longitude = None
    return {"title": title, "address": address, "latitude": latitude, "longitude": longitude}


# ----------------------------------------------------------------------
# iCalendar export
# ----------------------------------------------------------------------

_ICS_CONTROL = re.compile("[\x00-\x08\x0b-\x1f\x7f\ud800-\udfff]")
_ICS_RAW_CONTROL = re.compile("[\x00-\x1f\x7f\ud800-\udfff]")
_TZID_UNSAFE = re.compile(r'[;:,"\x00-\x1f]')
_STATUS_ICS = {"confirmed": "CONFIRMED", "tentative": "TENTATIVE", "cancelled": "CANCELLED"}
_VTIMEZONE_MIN_YEAR, _VTIMEZONE_MAX_YEAR = 1900, 2100
_OPEN_ENDED_YEARS = 10


def _ics_text(value) -> str:
    """Escape a TEXT value (RFC 5545 §3.3.11) and drop control characters."""
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    text = _ICS_CONTROL.sub("", text)
    return (text.replace("\\", "\\\\").replace(";", "\\;")
            .replace(",", "\\,").replace("\n", "\\n"))


def _ics_raw(value) -> str:
    """A non-TEXT value (URI, UID parts): no escaping, no control characters."""
    return _ICS_RAW_CONTROL.sub("", str(value))


def _fold(line: str) -> list[str]:
    """Split a content line into lines of at most 75 octets, never inside a UTF-8 sequence."""
    lines, current, size = [], "", 0
    for char in line:
        width = len(char.encode("utf-8"))
        if size + width > 75:
            lines.append(current)
            current, size = " ", 1
        current += char
        size += width
    lines.append(current)
    return lines


def _parse_when(value):
    """"YYYY-MM-DD" → date, naive ISO → naive datetime, ISO with offset → aware; else None."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        if len(text) == 10:
            return date.fromisoformat(text)
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _ics_date(value: date) -> str:
    return f"{value.year:04d}{value.month:02d}{value.day:02d}"


def _ics_local(value: datetime) -> str:
    return f"{_ics_date(value)}T{value.hour:02d}{value.minute:02d}{value.second:02d}"


def _ics_utc(value: datetime) -> str:
    return _ics_local(value.astimezone(timezone.utc)) + "Z"


def _ics_zone(name):
    """The ZoneInfo to write a TZID for, or None (then times are written in UTC)."""
    if not name or _TZID_UNSAFE.search(name):
        return None
    return _zone(name)


def _ics_time(value, kind: str, zone_name: str | None = None):
    """(parameters, value) of a DATE / DATE-TIME property typed for kind, or None."""
    parsed = _parse_when(value)
    if parsed is None:
        return None
    if kind == "all_day":
        if isinstance(parsed, datetime):
            parsed = (parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed).date()
        return ";VALUE=DATE", _ics_date(parsed)
    if not isinstance(parsed, datetime):
        parsed = datetime(parsed.year, parsed.month, parsed.day)
    if kind == "floating":
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return "", _ics_local(parsed)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    zone = _ics_zone(zone_name)
    if zone is not None:
        return f";TZID={zone_name}", _ics_local(parsed.astimezone(zone))
    return "", _ics_utc(parsed)


def _start_kind(event: dict) -> str:
    if event.get("all_day"):
        return "all_day"
    return "floating" if event.get("floating") else "zoned"


def _ics_until(value, start_kind: str) -> str | None:
    """UNTIL typed like DTSTART: DATE for all-day, UTC for zoned, local for floating."""
    parsed = _parse_when(value)
    if parsed is None:
        return None
    if start_kind == "all_day":
        if isinstance(parsed, datetime):
            parsed = (parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed).date()
        return _ics_date(parsed)
    if not isinstance(parsed, datetime):
        parsed = datetime(parsed.year, parsed.month, parsed.day, 23, 59, 59)
    if start_kind == "floating":
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return _ics_local(parsed)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return _ics_utc(parsed)


def recurrence_rrule(recurrence: dict, start_kind: str) -> str:
    """The RRULE value (without "RRULE:") for an event's ``recurrence``.

    ``start_kind`` is how the event's start is typed: ``"all_day"``,
    ``"floating"`` or ``"zoned"`` (anything else is read as zoned). UNTIL
    follows RFC 5545: a DATE for all-day events, a local time for floating
    ones, UTC for zoned ones. Returns "" when there is no rule to write.
    """
    if not recurrence:
        return ""
    frequency = recurrence.get("frequency")
    if frequency not in _FREQUENCIES.values():
        return ""
    if start_kind == "date":
        start_kind = "all_day"
    parts = [f"FREQ={frequency.upper()}"]
    interval = _int(recurrence.get("interval"))
    if interval and interval > 1:
        parts.append(f"INTERVAL={interval}")
    count = _int(recurrence.get("count"))
    if count and count > 0:
        parts.append(f"COUNT={count}")
    else:
        until = _ics_until(recurrence.get("until"), start_kind)
        if until:
            parts.append(f"UNTIL={until}")
    by_day = [str(d).upper() for d in recurrence.get("by_day") or []
              if isinstance(d, str) and _BYDAY.fullmatch(d.upper())]
    if by_day:
        parts.append("BYDAY=" + ",".join(by_day))
    for key, name in (("by_month_day", "BYMONTHDAY"), ("by_month", "BYMONTH"),
                      ("by_year_day", "BYYEARDAY"), ("by_week_no", "BYWEEKNO"),
                      ("by_set_pos", "BYSETPOS")):
        numbers = [str(n) for n in recurrence.get(key) or []
                   if isinstance(n, int) and not isinstance(n, bool) and n != 0]
        if numbers:
            parts.append(f"{name}=" + ",".join(numbers))
    if recurrence.get("week_start") in _WEEKDAYS:
        parts.append(f"WKST={recurrence['week_start']}")
    return ";".join(parts)


def _ics_duration(seconds_before) -> str:
    """TRIGGER duration for an alarm ``seconds_before`` the start (negative = after)."""
    offset = -int(seconds_before)
    sign = "-" if offset < 0 else ""
    days, rest = divmod(abs(offset), 86400)
    hours, rest = divmod(rest, 3600)
    minutes, seconds = divmod(rest, 60)
    if hours:
        time = f"{hours}H" + (f"{minutes}M" if minutes or seconds else "") + (f"{seconds}S" if seconds else "")
    elif minutes:
        time = f"{minutes}M" + (f"{seconds}S" if seconds else "")
    else:
        time = f"{seconds}S"
    if days and time == "0S":
        return f"{sign}P{days}D"
    return f"{sign}P{days}DT{time}" if days else f"{sign}PT{time}"


def _ics_offset(offset: timedelta) -> str:
    total = int(offset.total_seconds())
    sign = "+" if total >= 0 else "-"
    hours, rest = divmod(abs(total), 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{sign}{hours:02d}{minutes:02d}" + (f"{seconds:02d}" if seconds else "")


def _zone_changes(name: str, first_year: int, last_year: int) -> list[tuple]:
    """Every UTC-offset change in a zone from Jan 1 first_year to Dec 31 last_year.

    Returns (instant UTC, offset before, offset after, dst after, name after),
    found by checking each day at 00:00 UTC and narrowing a change to the minute.
    """
    zone = ZoneInfo(name)

    def offset(instant):
        return instant.astimezone(zone).utcoffset()

    changes = []
    previous_at = datetime(first_year, 1, 1, tzinfo=timezone.utc)
    stop = datetime(last_year + 1, 1, 1, tzinfo=timezone.utc)
    previous = offset(previous_at)
    day = timedelta(days=1)
    at = previous_at + day
    while at <= stop:
        current = offset(at)
        if current != previous:
            low, high = 0, 1440  # minutes after previous_at: low has the old offset, high the new
            while high - low > 1:
                middle = (low + high) // 2
                if offset(previous_at + timedelta(minutes=middle)) == previous:
                    low = middle
                else:
                    high = middle
            instant = previous_at + timedelta(minutes=high)
            local = instant.astimezone(zone)
            changes.append((instant, previous, local.utcoffset(), local.dst(), local.tzname()))
            previous = local.utcoffset()
        previous_at = at
        at += day
    return changes


def _vtimezone(name: str, first_year: int, last_year: int) -> list[str]:
    """A VTIMEZONE for an IANA zone covering first_year..last_year (deterministic)."""
    first_year = max(_VTIMEZONE_MIN_YEAR, first_year)
    last_year = min(_VTIMEZONE_MAX_YEAR, max(first_year, last_year))
    lines = ["BEGIN:VTIMEZONE", f"TZID:{name}"]
    changes = _zone_changes(name, first_year, last_year)
    if not changes:
        local = datetime(first_year, 1, 1, tzinfo=timezone.utc).astimezone(ZoneInfo(name))
        lines += ["BEGIN:STANDARD", "DTSTART:19700101T000000",
                  f"TZOFFSETFROM:{_ics_offset(local.utcoffset())}",
                  f"TZOFFSETTO:{_ics_offset(local.utcoffset())}"]
        if local.tzname():
            lines.append(f"TZNAME:{_ics_text(local.tzname())}")
        lines.append("END:STANDARD")
    for instant, before, after, dst, tzname in changes:
        component = "STANDARD" if after < before or not dst else "DAYLIGHT"
        lines += [f"BEGIN:{component}",
                  f"DTSTART:{_ics_local((instant + before).replace(tzinfo=None))}",
                  f"TZOFFSETFROM:{_ics_offset(before)}",
                  f"TZOFFSETTO:{_ics_offset(after)}"]
        if tzname:
            lines.append(f"TZNAME:{_ics_text(tzname)}")
        lines.append(f"END:{component}")
    lines.append("END:VTIMEZONE")
    return lines


def _person_label(person: dict) -> str | None:
    name, email = person.get("name"), person.get("email")
    if name and email:
        return f"{name} <{email}>"
    return name or email or person.get("phone")


def _description(event: dict) -> str | None:
    blocks = []
    if event.get("notes"):
        blocks.append(str(event["notes"]))
    people = []
    organizer = event.get("organizer")
    label = _person_label(organizer) if organizer else None
    if label:
        people.append(f"Organizer: {label}")
    invitees = []
    for attendee in event.get("attendees") or []:
        label = _person_label(attendee)
        if not label:
            continue
        status = attendee.get("status")
        if status and status != "unknown":
            label += f" ({str(status).replace('_', ' ')})"
        invitees.append(label)
    if invitees:
        people.append("Invitees: " + ", ".join(invitees))
    if people:
        blocks.append("\n".join(people))
    return "\n\n".join(blocks) or None


def _location_text(location: dict | None) -> str | None:
    if not location:
        return None
    title, address = location.get("title"), location.get("address")
    if title and address:
        if address == title or address.startswith(title):
            return address
        return f"{title}, {address}"
    return title or address


def _geo_number(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".")


def events_to_ics(events: list[dict], calendars: list[dict] | None = None, *,
                  now: datetime | None = None, product: str = "-//OpenExtract//Calendar//EN") -> str:
    """Write events from list_events as an iCalendar (RFC 5545) file.

    Series stay series (RRULE, EXDATE); a moved or edited occurrence whose
    series is in ``events`` shares its UID and carries RECURRENCE-ID. Zoned
    times are written with TZID and a VTIMEZONE built from the zone database,
    floating times without a zone, all-day events as dates (DTEND is the day
    after the last day). Organizer and invitees go into DESCRIPTION, not
    ORGANIZER/ATTENDEE, so importing the file doesn't turn events into
    meeting invitations. Events with no start date are left out. ``now`` is
    the DTSTAMP for events with no modified date, and the year a series
    without an end is covered from: each VTIMEZONE runs to 10 years after
    the later of ``now`` and the last year the zone's events use, so clients
    that read the file's VTIMEZONE (not the zone name) keep the right offsets.
    """
    stamp_now = (now or datetime.now(timezone.utc))
    if stamp_now.tzinfo is None:
        stamp_now = stamp_now.replace(tzinfo=timezone.utc)
    events = [e for e in events or [] if isinstance(e, dict)]
    by_id = {e.get("id"): e for e in events if e.get("id") is not None}

    def start_value(event):
        kind = _start_kind(event)
        if kind == "all_day":
            return event.get("start_day")
        return event.get("wall_start") if kind == "floating" else event.get("start")

    def master_of(event):
        detached = event.get("detached_from") or {}
        master = by_id.get(detached.get("event_id")) if detached else None
        if (master is None or master is event or master.get("detached_from")
                or start_value(master) is None or detached.get("original_start") is None):
            return None
        return master

    def base_uid(event):
        uid = _ics_raw(event.get("uid") or "").strip()
        return uid or f"oe-{event.get('id')}@openextract"

    # UIDs: a series and its moved occurrences share one; anything else is unique.
    uids, used = {}, set()
    for event in events:
        if master_of(event) is not None:
            continue
        uid = base_uid(event)
        if event.get("detached_from") or uid in used:
            uid = f"{uid}-{event.get('id')}"
        while uid in used:
            uid += "-1"
        used.add(uid)
        uids[id(event)] = uid

    zone_years: dict[str, set] = {}
    open_ended: set = set()

    def note_zone(zone_name, *values):
        if _ics_zone(zone_name) is None:
            return
        years = zone_years.setdefault(zone_name, set())
        for value in values:
            parsed = _parse_when(value)
            if parsed is not None:
                years.add(parsed.year)

    body: list[str] = []
    for event in events:
        kind = _start_kind(event)
        zone_name = event.get("time_zone") if kind == "zoned" else None
        start = _ics_time(start_value(event), kind, zone_name)
        if start is None:
            continue
        master = master_of(event)
        uid = uids.get(id(event)) or uids.get(id(master)) or base_uid(event)
        stamp = _parse_when(event.get("modified_date"))
        stamp = stamp if isinstance(stamp, datetime) and stamp.tzinfo else stamp_now

        lines = ["BEGIN:VEVENT", f"UID:{_ics_text(uid)}", f"DTSTAMP:{_ics_utc(stamp)}",
                 f"DTSTART{start[0]}:{start[1]}"]
        note_zone(zone_name, event.get("start"))

        end = None
        if kind == "all_day":
            last = _parse_when(event.get("end_day")) or _parse_when(event.get("start_day"))
            if isinstance(last, date) and not isinstance(last, datetime):
                end = _ics_time((last + timedelta(days=1)).isoformat(), kind)
        elif kind == "floating":
            begin, finish = _parse_when(event.get("wall_start")), _parse_when(event.get("wall_end"))
            if isinstance(finish, datetime) and isinstance(begin, datetime) and finish > begin:
                end = _ics_time(event.get("wall_end"), kind)
        else:
            begin, finish = _parse_when(event.get("start")), _parse_when(event.get("end"))
            if (isinstance(finish, datetime) and isinstance(begin, datetime)
                    and finish.tzinfo and begin.tzinfo and finish > begin):
                end_zone = event.get("end_time_zone") or zone_name
                end = _ics_time(event.get("end"), kind, end_zone)
                note_zone(end_zone, event.get("end"))
        if end is not None:
            lines.append(f"DTEND{end[0]}:{end[1]}")

        if master is not None:
            master_kind = _start_kind(master)
            master_zone = master.get("time_zone") if master_kind == "zoned" else None
            original = _ics_time(event["detached_from"]["original_start"], master_kind, master_zone)
            if original is not None:
                lines.append(f"RECURRENCE-ID{original[0]}:{original[1]}")
                note_zone(master_zone, event["detached_from"]["original_start"])
        else:
            recurrence = event.get("recurrence")
            rule = recurrence_rrule(recurrence, kind) if recurrence else ""
            if rule:
                lines.append(f"RRULE:{rule}")
                note_zone(zone_name, recurrence.get("until"), recurrence.get("last_start"))
                if zone_name and not recurrence.get("count") and not recurrence.get("until"):
                    open_ended.add(zone_name)
                excluded = [_ics_time(x, kind, zone_name) for x in event.get("exceptions") or []]
                excluded = [x for x in excluded if x is not None]
                if excluded:
                    lines.append(f"EXDATE{excluded[0][0]}:" + ",".join(x[1] for x in excluded))
                    note_zone(zone_name, *(event.get("exceptions") or []))

        if event.get("title"):
            lines.append(f"SUMMARY:{_ics_text(event['title'])}")
        description = _description(event)
        if description:
            lines.append(f"DESCRIPTION:{_ics_text(description)}")
        location = event.get("location") or None
        place = _location_text(location)
        if place:
            lines.append(f"LOCATION:{_ics_text(place)}")
        if location:
            latitude, longitude = _float(location.get("latitude")), _float(location.get("longitude"))
            if latitude is not None and longitude is not None:
                lines.append(f"GEO:{_geo_number(latitude)};{_geo_number(longitude)}")
        link = _ics_raw(event.get("url") or event.get("conference_url") or "").strip()
        if link:
            lines.append(f"URL:{link}")
        status = _STATUS_ICS.get(event.get("status"))
        if status:
            lines.append(f"STATUS:{status}")
        lines.append("TRANSP:" + ("TRANSPARENT" if event.get("availability") == "free" else "OPAQUE"))
        for key, name in (("created_date", "CREATED"), ("modified_date", "LAST-MODIFIED")):
            value = _parse_when(event.get(key))
            if isinstance(value, datetime) and value.tzinfo:
                lines.append(f"{name}:{_ics_utc(value)}")
        for alarm in event.get("alarms") or []:
            if not isinstance(alarm, dict):
                continue
            if alarm.get("seconds_before") is not None and _int(alarm["seconds_before"]) is not None:
                trigger = f"TRIGGER:{_ics_duration(_int(alarm['seconds_before']))}"
            else:
                at = _parse_when(alarm.get("at"))
                if not isinstance(at, datetime) or at.tzinfo is None:
                    continue
                trigger = f"TRIGGER;VALUE=DATE-TIME:{_ics_utc(at)}"
            lines += ["BEGIN:VALARM", "ACTION:DISPLAY", "DESCRIPTION:Reminder", trigger, "END:VALARM"]
        lines.append("END:VEVENT")
        body.extend(lines)

    head = ["BEGIN:VCALENDAR", "VERSION:2.0", f"PRODID:{_ics_text(product)}",
            "CALSCALE:GREGORIAN", "METHOD:PUBLISH"]
    if calendars and len(calendars) == 1 and isinstance(calendars[0], dict) and calendars[0].get("title"):
        head.append(f"X-WR-CALNAME:{_ics_text(calendars[0]['title'])}")
    for zone_name in sorted(zone_years):
        years = zone_years[zone_name] or {stamp_now.year}
        first, last = min(years) - 1, max(years) + 1
        if zone_name in open_ended:
            # A series without an end goes on past the export: cover the years ahead
            # of whichever is later, the export or the zone's last event year.
            last = max(last, max(max(years), stamp_now.year) + _OPEN_ENDED_YEARS)
        head.extend(_vtimezone(zone_name, first, last))
    lines = head + body + ["END:VCALENDAR"]
    return "".join(folded + "\r\n" for line in lines for folded in _fold(line))
