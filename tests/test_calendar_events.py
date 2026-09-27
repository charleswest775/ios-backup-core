"""Tests for CalendarExtractor against a fake unencrypted backup."""

import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from backup_builder import BackupBuilder
from ios_backup_core.extractors.calendar_events import (
    CALENDAR_DB_PATH,
    CALENDAR_DOMAIN,
    CalendarExtractor,
    event_times,
)

_APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)


def apple(y, mo, d, h=0, mi=0, s=0) -> float:
    """Seconds since 2001-01-01 for a UTC (or floating wall-clock) time."""
    return (datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc) - _APPLE_EPOCH).total_seconds()


FULL_SCHEMA = """
    CREATE TABLE Store (ROWID INTEGER PRIMARY KEY, name TEXT);
    CREATE TABLE Calendar (ROWID INTEGER PRIMARY KEY, title TEXT, color TEXT, store_id INTEGER);
    CREATE TABLE Location (ROWID INTEGER PRIMARY KEY, title TEXT, address TEXT,
                           latitude REAL, longitude REAL);
    CREATE TABLE Identity (ROWID INTEGER PRIMARY KEY, display_name TEXT, address TEXT);
    CREATE TABLE Participant (ROWID INTEGER PRIMARY KEY, entity_type INTEGER, owner_id INTEGER,
                              identity_id INTEGER, email TEXT, status INTEGER);
    CREATE TABLE CalendarItem (
        ROWID INTEGER PRIMARY KEY, summary TEXT, start_date REAL, end_date REAL, start_tz TEXT,
        all_day INTEGER, calendar_id INTEGER, description TEXT, url TEXT,
        conference_url_detected TEXT, unique_identifier TEXT, UUID TEXT, creation_date REAL,
        last_modified REAL, location_id INTEGER, organizer_id INTEGER, has_recurrences INTEGER,
        calendar_scale TEXT
    );
"""


def _item(conn, rowid, summary, start, end, tz="America/New_York", all_day=0, calendar_id=1,
          notes=None, location_id=None, organizer_id=None, recurring=0, scale=None, uid=None):
    conn.execute(
        "INSERT INTO CalendarItem VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, NULL, NULL, NULL,"
        " ?, ?, ?, ?)",
        (rowid, summary, start, end, tz, all_day, calendar_id, notes, uid,
         location_id, organizer_id, recurring, scale),
    )


def _full_calendar(conn):
    conn.executescript(FULL_SCHEMA)
    conn.execute("INSERT INTO Store VALUES (1, 'iCloud')")
    conn.execute("INSERT INTO Calendar VALUES (1, 'Work', '#1BADF8FF', 1)")
    conn.execute("INSERT INTO Calendar VALUES (2, 'Birthdays', '#8E8E93', 1)")
    conn.execute("INSERT INTO Location VALUES (1, 'Office', '1 Main St', 40.7, -74.0)")
    conn.execute("INSERT INTO Identity VALUES (1, 'Alex Chen', 'mailto:alex@example.com')")
    conn.execute("INSERT INTO Identity VALUES (2, 'Sam Lee', 'mailto:sam@example.com')")
    conn.execute("INSERT INTO Participant VALUES (1, 8, 1, 1, 'alex@example.com', 2)")   # organizer
    conn.execute("INSERT INTO Participant VALUES (2, 7, 1, 2, NULL, 2)")                  # attendee
    conn.execute("INSERT INTO Participant VALUES (3, 7, 1, NULL, 'pat@example.com', 1)")  # attendee

    _item(conn, 1, "Planning", apple(2024, 3, 10, 15), apple(2024, 3, 10, 16),
          notes="Agenda", location_id=1, organizer_id=1, uid="E1-UID")
    _item(conn, 2, "Holiday", apple(2024, 3, 12), apple(2024, 3, 12, 23, 59, 59),
          tz="_float", all_day=1)
    _item(conn, 3, "Conference", apple(2024, 3, 20), apple(2024, 3, 23),
          tz="_float", all_day=1)
    _item(conn, 4, "Wake-up call", apple(2024, 3, 15, 9, 30), apple(2024, 3, 15, 9, 45),
          tz="_float")
    _item(conn, 5, "Sam's birthday", apple(2024, 3, 1), apple(2024, 3, 2),
          tz="_float", all_day=1, calendar_id=2, scale="gregorian")
    _item(conn, 6, "Weekly sync", apple(2024, 3, 4, 14), apple(2024, 3, 4, 15), recurring=1)
    _item(conn, 7, "Undated", None, None)


class CalendarTests(unittest.TestCase):
    def setUp(self):
        self.builder = BackupBuilder()
        self.extractor = CalendarExtractor()

    def tearDown(self):
        self.builder.cleanup()

    def _list(self, build=_full_calendar, **kwargs):
        self.builder.add_db(CALENDAR_DOMAIN, CALENDAR_DB_PATH, build)
        return self.extractor.list_events(self.builder.accessor(), **kwargs)

    def _by_title(self, result):
        return {e["title"]: e for e in result["events"]}

    def test_timed_event(self):
        result = self._list()
        self.assertEqual(result["errors"], [])
        ev = self._by_title(result)["Planning"]
        self.assertEqual(ev["start"], "2024-03-10T15:00:00+00:00")
        self.assertEqual(ev["end"], "2024-03-10T16:00:00+00:00")
        self.assertFalse(ev["all_day"])
        self.assertFalse(ev["floating"])
        self.assertEqual(ev["timezone"], "America/New_York")
        self.assertEqual(ev["calendar"], "Work")
        self.assertEqual(ev["calendar_color"], "#1BADF8")
        self.assertEqual(ev["account"], "iCloud")
        self.assertEqual((ev["location"], ev["address"]), ("Office", "1 Main St"))
        self.assertEqual((ev["latitude"], ev["longitude"]), (40.7, -74.0))
        self.assertEqual(ev["notes"], "Agenda")
        self.assertEqual(ev["uid"], "E1-UID")
        self.assertEqual(ev["organizer"], {"name": "Alex Chen", "email": "alex@example.com"})
        self.assertEqual(
            [(a["name"], a["email"]) for a in ev["attendees"]],
            [("Sam Lee", "sam@example.com"), (None, "pat@example.com")],
        )

    def test_all_day_events_are_inclusive_dates(self):
        events = self._by_title(self._list())
        self.assertEqual((events["Holiday"]["start"], events["Holiday"]["end"]),
                         ("2024-03-12", "2024-03-12"))
        # Stored with the end at midnight after the last day.
        self.assertEqual((events["Conference"]["start"], events["Conference"]["end"]),
                         ("2024-03-20", "2024-03-22"))
        self.assertTrue(events["Conference"]["all_day"])
        self.assertIsNone(events["Conference"]["timezone"])

    def test_floating_timed_event_has_no_offset(self):
        ev = self._by_title(self._list())["Wake-up call"]
        self.assertEqual(ev["start"], "2024-03-15T09:30:00")
        self.assertFalse(ev["all_day"])
        self.assertTrue(ev["floating"])
        self.assertIsNone(ev["timezone"])

    def test_birthdays_and_undated_items_are_left_out(self):
        titles = set(self._by_title(self._list()))
        self.assertNotIn("Sam's birthday", titles)
        self.assertNotIn("Undated", titles)
        self.assertEqual(len(titles), 5)

    def test_newest_first_and_recurring_flag(self):
        result = self._list()
        starts = [e["start"][:10] for e in result["events"]]
        self.assertEqual(starts, sorted(starts, reverse=True))
        self.assertTrue(self._by_title(result)["Weekly sync"]["recurring"])
        self.assertFalse(self._by_title(result)["Planning"]["recurring"])

    def test_calendars_with_counts_and_filter(self):
        result = self._list()
        counts = {c["title"]: c["event_count"] for c in result["calendars"]}
        self.assertEqual(counts, {"Work": 5, "Birthdays": 0})
        work = next(c for c in result["calendars"] if c["title"] == "Work")
        self.assertEqual((work["color"], work["account"]), ("#1BADF8", "iCloud"))

        filtered = self.extractor.list_events(self.builder.accessor(), calendar_id=2)
        self.assertEqual(filtered["events"], [])

    def test_minimal_schema(self):
        def build(conn):
            conn.executescript("""
                CREATE TABLE Calendar (ROWID INTEGER PRIMARY KEY, title TEXT);
                CREATE TABLE CalendarItem (ROWID INTEGER PRIMARY KEY, summary TEXT,
                    start_date REAL, end_date REAL, start_tz TEXT, calendar_id INTEGER);
            """)
            conn.execute("INSERT INTO Calendar VALUES (1, 'Home')")
            conn.execute("INSERT INTO CalendarItem VALUES (1, 'Day off', ?, ?, '_float', 1)",
                         (apple(2024, 5, 1), apple(2024, 5, 2)))
        result = self._list(build)
        self.assertEqual(result["errors"], [])
        ev = result["events"][0]
        self.assertEqual((ev["title"], ev["calendar"]), ("Day off", "Home"))
        # No all_day column: inferred from a floating midnight-to-midnight event.
        self.assertTrue(ev["all_day"])
        self.assertEqual((ev["start"], ev["end"]), ("2024-05-01", "2024-05-01"))
        self.assertEqual(ev["attendees"], [])
        self.assertIsNone(ev["organizer"])

    def test_reads_uncheckpointed_wal(self):
        def build(conn):
            conn.executescript(FULL_SCHEMA)
            _item(conn, 1, "Old", apple(2024, 1, 1, 9), apple(2024, 1, 1, 10))

        def wal(conn):
            _item(conn, 2, "Added just before backup", apple(2024, 1, 2, 9), apple(2024, 1, 2, 10))

        self.builder.add_db(CALENDAR_DOMAIN, CALENDAR_DB_PATH, build, wal)
        result = self.extractor.list_events(self.builder.accessor())
        self.assertEqual([e["title"] for e in result["events"]], ["Added just before backup", "Old"])

    def test_missing_database(self):
        result = self.extractor.list_events(self.builder.accessor())
        self.assertEqual(result, {"events": [], "calendars": [], "errors": []})


class EventTimesTests(unittest.TestCase):
    def test_not_all_day_when_floating_but_short(self):
        times = event_times(apple(2024, 1, 1), apple(2024, 1, 1, 1), "_float", None)
        self.assertFalse(times["all_day"])
        self.assertEqual(times["start"], "2024-01-01T00:00:00")

    def test_all_day_without_end(self):
        times = event_times(apple(2024, 1, 1), None, "_float", True)
        self.assertEqual((times["start"], times["end"]), ("2024-01-01", "2024-01-01"))

    def test_no_start(self):
        self.assertIsNone(event_times(None, None, None, None))


if __name__ == "__main__":
    unittest.main()
