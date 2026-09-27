"""Tests for HealthExtractor against a fake backup."""

import os
import sys
import time
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from backup_builder import BackupBuilder
from ios_backup_core.extractors.health import (
    HEALTH_DOMAIN,
    MAIN_DB_PATH,
    SECURE_DB_PATH,
    UNENCRYPTED_HEALTH_NOTICE,
    HealthExtractor,
)

_APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)

STEPS, DISTANCE, FLIGHTS, ENERGY, HEART_RATE, RESTING_HR, WEIGHT, SLEEP = 7, 8, 12, 10, 5, 118, 3, 63
IPHONE, WATCH = 1, 2  # source ids (and provenance rowids)

SAMPLES_SCHEMA = """
    CREATE TABLE samples (data_id INTEGER PRIMARY KEY, start_date REAL, end_date REAL,
                          data_type INTEGER);
    CREATE TABLE quantity_samples (data_id INTEGER PRIMARY KEY, quantity REAL,
                                   original_quantity REAL, original_unit INTEGER);
"""
FULL_SCHEMA = SAMPLES_SCHEMA + """
    CREATE TABLE category_samples (data_id INTEGER PRIMARY KEY, value INTEGER);
    CREATE TABLE objects (data_id INTEGER PRIMARY KEY, uuid BLOB, provenance INTEGER,
                          type INTEGER, creation_date REAL);
    CREATE TABLE data_provenances (ROWID INTEGER PRIMARY KEY, source_id INTEGER,
                                   device_id INTEGER, origin_product_type TEXT, tz_name TEXT);
    CREATE TABLE metadata_keys (ROWID INTEGER PRIMARY KEY, key TEXT);
    CREATE TABLE metadata_values (ROWID INTEGER PRIMARY KEY, key_id INTEGER, object_id INTEGER,
                                  numerical_value REAL);
    INSERT INTO data_provenances VALUES (1, 1, 1, 'iPhone15,2', 'America/Los_Angeles');
    INSERT INTO data_provenances VALUES (2, 2, 2, 'Watch6,1', 'America/Los_Angeles');
    INSERT INTO metadata_keys VALUES (1, '_HKPrivateWorkoutAverageHeartRate');
    INSERT INTO metadata_keys VALUES (2, '_HKPrivateWorkoutMaxHeartRate');
"""
WORKOUT_ACTIVITIES_SCHEMA = """
    CREATE TABLE workouts (data_id INTEGER PRIMARY KEY, total_distance REAL, goal_type INTEGER,
                           goal REAL);
    CREATE TABLE workout_activities (ROWID INTEGER PRIMARY KEY, owner_id INTEGER,
                                     activity_type INTEGER, location_type INTEGER,
                                     start_date REAL, end_date REAL, duration REAL);
    CREATE TABLE workout_statistics (ROWID INTEGER PRIMARY KEY, workout_activity_id INTEGER,
                                     data_type INTEGER, quantity REAL);
"""
LEGACY_WORKOUTS_SCHEMA = """
    CREATE TABLE workouts (data_id INTEGER PRIMARY KEY, total_distance REAL,
                           total_energy_burned REAL, total_basal_energy_burned REAL,
                           goal_type INTEGER, goal REAL, activity_type INTEGER, duration REAL);
"""


def ts(y, mo, d, h=0, mi=0) -> float:
    """Seconds since 2001-01-01 for a UTC time."""
    return (datetime(y, mo, d, h, mi, tzinfo=timezone.utc) - _APPLE_EPOCH).total_seconds()


class _Writer:
    """Inserts samples into an open healthdb_secure connection."""

    def __init__(self, conn):
        self.conn = conn
        self.next_id = conn.execute("SELECT COALESCE(MAX(data_id), 0) + 1 FROM samples").fetchone()[0]

    def _sample(self, data_type, start, end, source, obj_type=1):
        data_id = self.next_id
        self.next_id += 1
        self.conn.execute("INSERT INTO samples VALUES (?, ?, ?, ?)", (data_id, start, end, data_type))
        if source is not None:
            self.conn.execute("INSERT INTO objects VALUES (?, NULL, ?, ?, NULL)",
                              (data_id, source, obj_type))
        return data_id

    def quantity(self, data_type, start, value, source=IPHONE, minutes=5, obj_type=1):
        data_id = self._sample(data_type, start, start + minutes * 60, source, obj_type)
        self.conn.execute("INSERT INTO quantity_samples VALUES (?, ?, NULL, NULL)", (data_id, value))
        return data_id

    def category(self, data_type, start, end, value, source=WATCH):
        data_id = self._sample(data_type, start, end, source)
        self.conn.execute("INSERT INTO category_samples VALUES (?, ?)", (data_id, value))
        return data_id


def _add_sources_db(builder):
    def build(conn):
        conn.execute("CREATE TABLE sources (ROWID INTEGER PRIMARY KEY, name TEXT, bundle_id TEXT)")
        conn.execute("INSERT INTO sources VALUES (1, 'iPhone', 'com.apple.health.1')")
        conn.execute("INSERT INTO sources VALUES (2, 'Apple Watch', 'com.apple.health.2')")
    builder.add_db(HEALTH_DOMAIN, MAIN_DB_PATH, build)


class _HealthTestCase(unittest.TestCase):
    tz = "UTC"

    def setUp(self):
        self._old_tz = os.environ.get("TZ")
        os.environ["TZ"] = self.tz
        if hasattr(time, "tzset"):
            time.tzset()
        self.builder = BackupBuilder()
        self.extractor = HealthExtractor()

    def tearDown(self):
        if self._old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self._old_tz
        if hasattr(time, "tzset"):
            time.tzset()
        self.builder.cleanup()

    def summary(self, fill, schema=FULL_SCHEMA, encrypted=True, wal_fill=None):
        def build(conn):
            conn.executescript(schema)
            fill(_Writer(conn), conn)
        wal = (lambda conn: wal_fill(_Writer(conn), conn)) if wal_fill else None
        self.builder.add_db(HEALTH_DOMAIN, SECURE_DB_PATH, build, wal)
        _add_sources_db(self.builder)
        return self.extractor.get_summary(self.builder.accessor(encrypted=encrypted))

    @staticmethod
    def by_date(rows):
        return {r["date"]: r for r in rows}


class DailyMetricsTests(_HealthTestCase):
    def test_steps_use_the_larger_source_each_day(self):
        def fill(w, conn):
            # Jan 1: iPhone and Watch both counted the same walk.
            w.quantity(STEPS, ts(2024, 1, 1, 9), 3000, IPHONE)
            w.quantity(STEPS, ts(2024, 1, 1, 17), 2000, IPHONE)
            w.quantity(STEPS, ts(2024, 1, 1, 9), 6000, WATCH)
            # Jan 2: only the iPhone.
            w.quantity(STEPS, ts(2024, 1, 2, 9), 3000, IPHONE)
        result = self.summary(fill)
        self.assertTrue(result["available"])
        self.assertEqual(result["errors"], [])
        days = self.by_date(result["daily"])
        self.assertEqual(days["2024-01-01"]["steps"], 6000)   # not 11,000
        self.assertEqual(days["2024-01-02"]["steps"], 3000)
        self.assertEqual(result["range"], {"first": "2024-01-01", "last": "2024-01-02"})

    def test_units_for_distance_flights_and_energy(self):
        def fill(w, conn):
            w.quantity(DISTANCE, ts(2024, 1, 1, 9), 1500)
            w.quantity(DISTANCE, ts(2024, 1, 1, 10), 500)
            w.quantity(FLIGHTS, ts(2024, 1, 1, 11), 3)
            w.quantity(ENERGY, ts(2024, 1, 1, 12), 250.4)
        day = self.summary(fill)["daily"][0]
        self.assertEqual(day["distance_km"], 2.0)
        self.assertEqual(day["flights"], 3)
        self.assertEqual(day["active_energy_kcal"], 250)

    def test_heart_rate_is_converted_to_bpm(self):
        def fill(w, conn):
            for bps in (1.0, 1.5, 2.0):
                w.quantity(HEART_RATE, ts(2024, 1, 1, 9), bps, WATCH)
            w.quantity(HEART_RATE, ts(2024, 1, 1, 10), 3.0, WATCH, obj_type=2)  # left out
            w.quantity(RESTING_HR, ts(2024, 1, 1, 6), 58, WATCH)
            w.quantity(RESTING_HR, ts(2024, 1, 1, 7), 60, WATCH)
        day = self.summary(fill)["daily"][0]
        self.assertEqual((day["heart_rate_min"], day["heart_rate_avg"], day["heart_rate_max"]),
                         (60, 90, 120))
        self.assertEqual(day["resting_heart_rate"], 59)

    def test_weight_is_the_days_average(self):
        def fill(w, conn):
            w.quantity(WEIGHT, ts(2024, 1, 1, 7), 70.0)
            w.quantity(WEIGHT, ts(2024, 1, 1, 20), 70.5)
        self.assertEqual(self.summary(fill)["daily"][0]["weight_kg"], 70.25)

    def test_reads_uncheckpointed_wal(self):
        def fill(w, conn):
            w.quantity(STEPS, ts(2024, 1, 1, 9), 1000)

        def wal_fill(w, conn):
            w.quantity(STEPS, ts(2024, 1, 2, 9), 4321)

        days = self.by_date(self.summary(fill, wal_fill=wal_fill)["daily"])
        self.assertEqual(days["2024-01-02"]["steps"], 4321)

    def test_minimal_schema_without_sources(self):
        def fill(w, conn):
            conn.execute("INSERT INTO samples VALUES (1, ?, ?, ?)", (ts(2024, 1, 1, 9), ts(2024, 1, 1, 9, 5), STEPS))
            conn.execute("INSERT INTO quantity_samples VALUES (1, 1234, NULL, NULL)")
        result = self.summary(fill, schema=SAMPLES_SCHEMA)
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["daily"][0]["steps"], 1234)
        self.assertEqual((result["sleep"], result["workouts"]), ([], []))


class LocalDayTests(_HealthTestCase):
    tz = "America/Los_Angeles"

    @unittest.skipUnless(hasattr(time, "tzset"), "needs time.tzset")
    def test_days_follow_the_local_time_zone(self):
        def fill(w, conn):
            # 03:00 UTC on Jan 2 is 7 PM on Jan 1 in Los Angeles.
            w.quantity(STEPS, ts(2024, 1, 2, 3), 500)
        self.assertEqual(self.summary(fill)["daily"][0]["date"], "2024-01-01")


class SleepTests(_HealthTestCase):
    def test_sleep_uses_the_most_complete_source(self):
        night_start = ts(2024, 1, 1, 23)

        def fill(w, conn):
            hour = 3600
            # Apple Watch: staged sleep, 5.5 hours asleep plus 30 minutes awake.
            t = night_start
            for value, hours in ((3, 3), (2, 0.5), (4, 1), (5, 1.5)):  # core, awake, deep, REM
                w.category(SLEEP, t, t + hours * hour, value, WATCH)
                t += hours * hour
            # iPhone: 8 hours in bed and 5 hours "asleep" for the same night.
            w.category(SLEEP, night_start, night_start + 8 * hour, 0, IPHONE)
            w.category(SLEEP, night_start, night_start + 5 * hour, 1, IPHONE)
        result = self.summary(fill)
        self.assertEqual(result["sleep"], [{
            "date": "2024-01-02",
            "asleep_minutes": 330,
            "in_bed_minutes": 480,
            "awake_minutes": 30,
            "core_minutes": 180,
            "deep_minutes": 60,
            "rem_minutes": 90,
        }])


class WorkoutTests(_HealthTestCase):
    def test_workout_activities(self):
        def fill(w, conn):
            conn.executescript(WORKOUT_ACTIVITIES_SCHEMA)
            conn.execute("INSERT INTO objects VALUES (100, NULL, 2, 1, NULL)")  # recorded by the Watch
            conn.execute("INSERT INTO workouts VALUES (100, 5.1, 0, 0)")
            conn.execute("INSERT INTO workout_activities VALUES (1, 100, 37, 3, ?, ?, 1800)",
                         (ts(2024, 1, 1, 7), ts(2024, 1, 1, 7, 31)))
            conn.execute("INSERT INTO workout_statistics VALUES (1, 1, 10, 320.4)")
            conn.execute("INSERT INTO workout_statistics VALUES (2, 1, 8, 5012)")
            conn.execute("INSERT INTO metadata_values VALUES (1, 1, 100, 2.5)")
            conn.execute("INSERT INTO metadata_values VALUES (2, 2, 100, 3.0)")
        workout = self.summary(fill)["workouts"][0]
        self.assertEqual(workout["type"], "Running")
        self.assertEqual(workout["activity_type"], 37)
        self.assertTrue(workout["start"].startswith("2024-01-01T07:00:00"))
        self.assertEqual(workout["duration_minutes"], 30.0)
        self.assertEqual(workout["distance_km"], 5.01)  # from the activity's own statistics
        self.assertEqual(workout["energy_kcal"], 320)
        self.assertEqual((workout["avg_heart_rate"], workout["max_heart_rate"]), (150, 180))
        self.assertIs(workout["indoor"], False)
        self.assertEqual(workout["source"], "Apple Watch")

    def test_multisport_workout_total_distance_is_not_repeated(self):
        def fill(w, conn):
            conn.executescript(WORKOUT_ACTIVITIES_SCHEMA)
            conn.execute("INSERT INTO workouts VALUES (200, 40.0, 0, 0)")
            conn.execute("INSERT INTO workout_activities VALUES (1, 200, 46, 3, ?, ?, 1200)",
                         (ts(2024, 2, 1, 8), ts(2024, 2, 1, 8, 20)))
            conn.execute("INSERT INTO workout_activities VALUES (2, 200, 13, 3, ?, ?, 3600)",
                         (ts(2024, 2, 1, 8, 25), ts(2024, 2, 1, 9, 25)))
            conn.execute("INSERT INTO workouts VALUES (300, 20.5, 0, 0)")
            conn.execute("INSERT INTO workout_activities VALUES (3, 300, 13, 2, ?, ?, 2400)",
                         (ts(2024, 2, 2, 8), ts(2024, 2, 2, 8, 40)))
        workouts = self.summary(fill)["workouts"]
        self.assertEqual([w["type"] for w in workouts], ["Cycling", "Cycling", "Swimming"])
        self.assertEqual([w["distance_km"] for w in workouts], [20.5, None, None])
        self.assertIs(workouts[0]["indoor"], True)
        self.assertIsNone(workouts[0]["source"])  # no objects row

    def test_legacy_workouts_table(self):
        def fill(w, conn):
            conn.executescript(LEGACY_WORKOUTS_SCHEMA)
            conn.execute("INSERT INTO samples VALUES (500, ?, ?, 79)", (ts(2020, 6, 1, 18), ts(2020, 6, 1, 19)))
            conn.execute("INSERT INTO objects VALUES (500, NULL, 1, 1, NULL)")
            conn.execute("INSERT INTO workouts VALUES (500, 4.2, 210.2, 60, 0, 0, 52, 3600)")
        workout = self.summary(fill)["workouts"][0]
        self.assertEqual((workout["type"], workout["duration_minutes"], workout["distance_km"],
                          workout["energy_kcal"]), ("Walking", 60.0, 4.2, 210))
        self.assertIsNone(workout["indoor"])
        self.assertEqual(workout["source"], "iPhone")


class AvailabilityTests(_HealthTestCase):
    def test_unencrypted_backup_gets_a_notice(self):
        result = self.extractor.get_summary(self.builder.accessor(encrypted=False))
        self.assertFalse(result["available"])
        self.assertEqual(result["notice"], UNENCRYPTED_HEALTH_NOTICE)
        self.assertEqual(result["errors"], [])

    def test_placeholder_file_in_unencrypted_backup(self):
        self.builder.add_bytes(HEALTH_DOMAIN, SECURE_DB_PATH, b"\x00" * 8)
        result = self.extractor.get_summary(self.builder.accessor(encrypted=False))
        self.assertFalse(result["available"])
        self.assertEqual(result["notice"], UNENCRYPTED_HEALTH_NOTICE)
        self.assertEqual(result["errors"], [])

    def test_unreadable_database_in_encrypted_backup_is_an_error(self):
        self.builder.add_bytes(HEALTH_DOMAIN, SECURE_DB_PATH, b"\x00" * 8)
        result = self.extractor.get_summary(self.builder.accessor(encrypted=True))
        self.assertFalse(result["available"])
        self.assertIsNone(result["notice"])
        self.assertEqual(len(result["errors"]), 1)

    def test_encrypted_backup_without_health(self):
        result = self.extractor.get_summary(self.builder.accessor(encrypted=True))
        self.assertEqual((result["available"], result["notice"], result["errors"]), (False, None, []))


if __name__ == "__main__":
    unittest.main()
