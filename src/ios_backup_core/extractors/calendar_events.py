"""
Calendar extraction from Calendar.sqlitedb (HomeDomain Library/Calendar/).

Returns events with their calendar, account, location, organizer and
attendees, plus the list of calendars.

Times: Calendar stores seconds since 2001-01-01. Timed events are absolute
instants and are returned as UTC ISO strings. Floating events (start_tz
"_float", which includes all-day events) are wall-clock times with no zone,
so they're returned without an offset — converting them to UTC and back
would move an all-day event to the previous day anywhere west of Greenwich.
All-day events come back as dates: ``start`` is the first day and ``end``
the last day, both inclusive.

Left out: birthday events generated from Contacts (calendar_scale
"gregorian") and items with no start date.

Recurring events are returned once, as their stored master row, with
``recurring: True``. Expanding them into occurrences isn't implemented yet:
the Recurrence table's frequency encoding hasn't been verified against real
backups.
"""

import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from typing import Optional

from ios_backup_core.backup import open_database
from ios_backup_core.schema import column_names, table_names

CALENDAR_DB_PATH = "Library/Calendar/Calendar.sqlitedb"
CALENDAR_DOMAIN = "HomeDomain"

FLOATING_TZ = "_float"

# Participant.entity_type for event attendees (organizers use other values).
_ATTENDEE_ENTITY_TYPE = 7

_APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)


def _apple_datetime(ts) -> Optional[datetime]:
    if ts is None or ts == "":
        return None
    try:
        return _APPLE_EPOCH + timedelta(seconds=float(ts))
    except (TypeError, ValueError, OverflowError):
        return None


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _color(value) -> Optional[str]:
    """Normalize Calendar's "#RRGGBB" / "#RRGGBBAA" colors to "#RRGGBB"."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if re.fullmatch(r"#[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?", value):
        return value[:7].upper()
    return None


def _email(value) -> Optional[str]:
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    if value.lower().startswith("mailto:"):
        value = value[7:]
    return value or None


def _text(value) -> Optional[str]:
    return value.strip() if isinstance(value, str) and value.strip() else None


def event_times(start_ts, end_ts, tz: Optional[str], all_day: Optional[bool]) -> Optional[dict]:
    """Return ``{start, end, all_day, floating}`` in the shapes described above."""
    start = _apple_datetime(start_ts)
    if start is None:
        return None
    end = _apple_datetime(end_ts)
    floating = tz == FLOATING_TZ
    if all_day is None:
        # Older schemas without an all_day column: a floating event that starts
        # at midnight and lasts at least a day is an all-day event.
        all_day = (
            floating
            and start.time() == datetime.min.time()
            and end is not None
            and end - start >= timedelta(hours=23, minutes=59)
        )

    if all_day:
        # iOS has stored the end as either 23:59:59 on the last day or 00:00 on
        # the day after; stepping back one second covers both.
        last_day = (end - timedelta(seconds=1)).date() if end and end > start else start.date()
        return {
            "start": start.date().isoformat(),
            "end": last_day.isoformat(),
            "all_day": True,
            "floating": True,
        }

    def fmt(dt):
        if dt is None:
            return None
        return dt.replace(tzinfo=None).isoformat() if floating else dt.isoformat()

    return {"start": fmt(start), "end": fmt(end), "all_day": False, "floating": floating}


class CalendarExtractor:
    """Extracts calendars and events from Calendar.sqlitedb."""

    def list_events(self, backup, calendar_id: Optional[int] = None) -> dict:
        """List events (newest first) and calendars.

        Returns ``{"events": [...], "calendars": [...], "errors": [...]}``.
        ``calendars`` includes an ``event_count`` per calendar, counted after
        birthday events are left out.
        """
        db_path = open_database(backup, CALENDAR_DB_PATH, CALENDAR_DOMAIN)
        if not db_path:
            return {"events": [], "calendars": [], "errors": []}
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA query_only = TRUE")
                calendars = self._read_calendars(conn)
                events = self._read_events(conn)
            finally:
                conn.close()
        except Exception as e:
            print(f"[calendar] parse error: {e}", file=sys.stderr, flush=True)
            return {
                "events": [],
                "calendars": [],
                "errors": [{"message": f"Couldn't read the Calendar database ({e})."}],
            }

        counts: dict = {}
        for ev in events:
            counts[ev["calendar_id"]] = counts.get(ev["calendar_id"], 0) + 1
        for cal in calendars:
            cal["event_count"] = counts.get(cal["id"], 0)

        if calendar_id is not None:
            events = [ev for ev in events if ev["calendar_id"] == calendar_id]
        return {"events": events, "calendars": calendars, "errors": []}

    # ── Queries ──────────────────────────────────────────────────────────────

    @staticmethod
    def _read_calendars(conn: sqlite3.Connection) -> list:
        tables = table_names(conn)
        if "Calendar" not in tables:
            return []
        cal_cols = column_names(conn, "Calendar")
        has_store = "Store" in tables and "store_id" in cal_cols
        rows = conn.execute(f"""
            SELECT c.ROWID AS id,
                   {"c.title" if "title" in cal_cols else "NULL"} AS title,
                   {"c.color" if "color" in cal_cols else "NULL"} AS color,
                   {"s.name" if has_store else "NULL"} AS account
            FROM Calendar c
            {"LEFT JOIN Store s ON c.store_id = s.ROWID" if has_store else ""}
        """).fetchall()
        return [{
            "id": r["id"],
            "title": _text(r["title"]) or "Untitled calendar",
            "color": _color(r["color"]),
            "account": _text(r["account"]),
        } for r in rows]

    def _read_events(self, conn: sqlite3.Connection) -> list:
        tables = table_names(conn)
        if "CalendarItem" not in tables:
            return []
        # SQLite identifiers are case-insensitive, so compare lowercased names.
        cols = {t: {c.lower() for c in column_names(conn, t)} for t in
                ("CalendarItem", "Calendar", "Store", "Location", "Participant", "Identity")}
        ci = cols["CalendarItem"]

        joins: list = []
        joined: set = {"ci"}

        def join(alias: str, table: str, on: str) -> None:
            joins.append(f"LEFT JOIN {table} {alias} ON {on}")
            joined.add(alias)

        if "calendar_id" in ci and cols["Calendar"]:
            join("c", "Calendar", "ci.calendar_id = c.ROWID")
            if "store_id" in cols["Calendar"] and cols["Store"]:
                join("s", "Store", "c.store_id = s.ROWID")
        if "location_id" in ci and cols["Location"]:
            join("l", "Location", "ci.location_id = l.ROWID")
        if "organizer_id" in ci and cols["Participant"]:
            join("op", "Participant", "ci.organizer_id = op.ROWID")
            if "identity_id" in cols["Participant"] and cols["Identity"]:
                join("oi", "Identity", "op.identity_id = oi.ROWID")

        def pick(alias: str, table: str, column: str, as_name: str) -> str:
            if alias in joined and column.lower() in cols[table]:
                return f"{alias}.{column} AS {as_name}"
            return f"NULL AS {as_name}"

        conference = "conference_url_detected" if "conference_url_detected" in ci else "conference_url"
        if "has_recurrences" in ci:
            recurring = "ci.has_recurrences AS recurring"
        elif "Recurrence" in tables and "owner_id" in {c.lower() for c in column_names(conn, "Recurrence")}:
            recurring = "EXISTS(SELECT 1 FROM Recurrence r WHERE r.owner_id = ci.ROWID) AS recurring"
        else:
            recurring = "0 AS recurring"

        select = ",\n".join([
            "ci.ROWID AS id",
            pick("ci", "CalendarItem", "summary", "title"),
            "ci.start_date AS start_date",
            pick("ci", "CalendarItem", "end_date", "end_date"),
            pick("ci", "CalendarItem", "start_tz", "start_tz"),
            pick("ci", "CalendarItem", "all_day", "all_day"),
            pick("ci", "CalendarItem", "calendar_id", "calendar_id"),
            pick("ci", "CalendarItem", "description", "notes"),
            pick("ci", "CalendarItem", "url", "url"),
            pick("ci", "CalendarItem", conference, "conference_url"),
            pick("ci", "CalendarItem", "unique_identifier", "unique_identifier"),
            pick("ci", "CalendarItem", "UUID", "uuid"),
            pick("ci", "CalendarItem", "creation_date", "creation_date"),
            pick("ci", "CalendarItem", "last_modified", "last_modified"),
            recurring,
            pick("c", "Calendar", "title", "calendar_title"),
            pick("c", "Calendar", "color", "calendar_color"),
            pick("s", "Store", "name", "account"),
            pick("l", "Location", "title", "location"),
            pick("l", "Location", "address", "address"),
            pick("l", "Location", "latitude", "latitude"),
            pick("l", "Location", "longitude", "longitude"),
            pick("oi", "Identity", "display_name", "organizer_name"),
            pick("op", "Participant", "email", "organizer_email"),
        ])
        where = ["ci.start_date IS NOT NULL"]
        if "calendar_scale" in ci:
            where.append("(ci.calendar_scale IS NULL OR ci.calendar_scale != 'gregorian')")

        rows = conn.execute(f"""
            SELECT {select}
            FROM CalendarItem ci
            {" ".join(joins)}
            WHERE {" AND ".join(where)}
            ORDER BY ci.start_date DESC
        """).fetchall()

        attendees = self._read_attendees(conn, cols)
        events = []
        for r in rows:
            times = event_times(
                r["start_date"], r["end_date"], r["start_tz"],
                None if r["all_day"] is None else bool(r["all_day"]),
            )
            if times is None:
                continue
            tz = r["start_tz"]
            organizer_email = _email(r["organizer_email"])
            organizer_name = _text(r["organizer_name"])
            lat, lon = r["latitude"], r["longitude"]
            has_coords = isinstance(lat, (int, float)) and isinstance(lon, (int, float)) and (lat or lon)
            events.append({
                "id": r["id"],
                "title": _text(r["title"]) or "",
                **times,
                "timezone": tz if tz and tz != FLOATING_TZ else None,
                "calendar_id": r["calendar_id"],
                "calendar": _text(r["calendar_title"]),
                "calendar_color": _color(r["calendar_color"]),
                "account": _text(r["account"]),
                "location": _text(r["location"]),
                "address": _text(r["address"]),
                "latitude": float(lat) if has_coords else None,
                "longitude": float(lon) if has_coords else None,
                "notes": _text(r["notes"]) or "",
                "url": _text(r["url"]),
                "conference_url": _text(r["conference_url"]),
                "organizer": (
                    {"name": organizer_name, "email": organizer_email}
                    if organizer_name or organizer_email else None
                ),
                "attendees": attendees.get(r["id"], []),
                "recurring": bool(r["recurring"]),
                "uid": _text(r["unique_identifier"]) or _text(r["uuid"]),
                "created": _iso(_apple_datetime(r["creation_date"])),
                "modified": _iso(_apple_datetime(r["last_modified"])),
            })
        return events

    @staticmethod
    def _read_attendees(conn: sqlite3.Connection, cols: dict) -> dict:
        """Map event ROWID → [{"name", "email", "status"}]."""
        pcols = cols["Participant"]
        if not {"owner_id", "entity_type"} <= pcols:
            return {}
        has_identity = "identity_id" in pcols and {"display_name", "address"} <= cols["Identity"]
        rows = conn.execute(f"""
            SELECT p.owner_id AS owner_id,
                   {"p.email" if "email" in pcols else "NULL"} AS email,
                   {"p.status" if "status" in pcols else "NULL"} AS status,
                   {"i.display_name" if has_identity else "NULL"} AS name,
                   {"i.address" if has_identity else "NULL"} AS address
            FROM Participant p
            {"LEFT JOIN Identity i ON p.identity_id = i.ROWID" if has_identity else ""}
            WHERE p.entity_type = ?
            ORDER BY p.ROWID
        """, (_ATTENDEE_ENTITY_TYPE,)).fetchall()
        by_event: dict = {}
        for r in rows:
            email = _email(r["email"]) or _email(r["address"])
            name = _text(r["name"])
            if not (email or name):
                continue
            by_event.setdefault(r["owner_id"], []).append(
                {"name": name, "email": email, "status": r["status"]}
            )
        return by_event
