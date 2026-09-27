"""
Apple Health extraction from HealthDomain ``Health/healthdb_secure.sqlite``
(samples and workouts) and ``Health/healthdb.sqlite`` (source names).

Health data is only in encrypted backups. Unencrypted ones leave it out —
sometimes as a placeholder file that isn't a database.

The database stores sample types as numbers. The codes and units used here
are the ones iLEAPP (scripts/artifacts/health.py) and APOLLO (modules/health_*)
document and test against real devices:

    7    steps                       count
    8    walking + running distance  meters
    12   flights climbed             count
    10   active energy               kilocalories
    5    heart rate                  beats per *second*
    118  resting heart rate          beats per minute
    3    body mass                   kilograms
    63   sleep analysis              category (HKCategoryValueSleepAnalysis)

Workouts come from ``workout_activities`` + ``workout_statistics`` (iOS 16+)
or the older ``workouts`` table, where ``total_distance`` is in kilometers.

Days are local days on this computer (SQLite's 'localtime').

iPhone and Apple Watch both record steps, distance, flights and energy for the
same minutes, so adding every sample would count that time twice. Each day's
total is therefore taken from the single source that recorded the most that
day. Sleep works the same way per night.
"""

import sqlite3
import sys
from typing import Optional

from ios_backup_core.backup import open_database
from ios_backup_core.schema import column_names, table_names
from ios_backup_core.timestamps import apple_to_iso

HEALTH_DOMAIN = "HealthDomain"
SECURE_DB_PATH = "Health/healthdb_secure.sqlite"
MAIN_DB_PATH = "Health/healthdb.sqlite"

UNENCRYPTED_HEALTH_NOTICE = (
    "This backup isn't encrypted, and iOS leaves Health data out of "
    "unencrypted backups. To include it, turn on \"Encrypt local backup\" "
    "and back up the iPhone again."
)

# Daily totals: field → (data_type, factor to the reported unit, decimals).
DAILY_TOTALS = {
    "steps": (7, 1.0, 0),
    "distance_km": (8, 0.001, 2),
    "flights": (12, 1.0, 0),
    "active_energy_kcal": (10, 1.0, 0),
}
HEART_RATE = 5              # beats per second
RESTING_HEART_RATE = 118    # beats per minute
BODY_MASS = 3               # kilograms
SLEEP_ANALYSIS = 63

# workout_statistics.data_type values.
_STAT_ACTIVE_ENERGY = 10    # kilocalories
_STAT_WALK_RUN_DISTANCE = 8  # meters

# HKCategoryValueSleepAnalysis.
SLEEP_IN_BED, SLEEP_ASLEEP, SLEEP_AWAKE, SLEEP_CORE, SLEEP_DEEP, SLEEP_REM = range(6)
_ASLEEP_VALUES = (SLEEP_ASLEEP, SLEEP_CORE, SLEEP_DEEP, SLEEP_REM)

# workout_activities.location_type.
_INDOOR, _OUTDOOR = 2, 3

_APPLE_EPOCH_OFFSET = 978307200

# HKWorkoutActivityType raw values (Apple's public enum).
WORKOUT_TYPES = {
    1: "American Football", 2: "Archery", 3: "Australian Football", 4: "Badminton",
    5: "Baseball", 6: "Basketball", 7: "Bowling", 8: "Boxing", 9: "Climbing",
    10: "Cricket", 11: "Cross Training", 12: "Curling", 13: "Cycling", 14: "Dance",
    15: "Dance Training", 16: "Elliptical", 17: "Equestrian Sports", 18: "Fencing",
    19: "Fishing", 20: "Functional Strength Training", 21: "Golf", 22: "Gymnastics",
    23: "Handball", 24: "Hiking", 25: "Hockey", 26: "Hunting", 27: "Lacrosse",
    28: "Martial Arts", 29: "Mind and Body", 30: "Mixed Cardio", 31: "Paddle Sports",
    32: "Play", 33: "Preparation and Recovery", 34: "Racquetball", 35: "Rowing",
    36: "Rugby", 37: "Running", 38: "Sailing", 39: "Skating", 40: "Snow Sports",
    41: "Soccer", 42: "Softball", 43: "Squash", 44: "Stair Climbing",
    45: "Surfing", 46: "Swimming", 47: "Table Tennis", 48: "Tennis",
    49: "Track and Field", 50: "Traditional Strength Training", 51: "Volleyball",
    52: "Walking", 53: "Water Fitness", 54: "Water Polo", 55: "Water Sports",
    56: "Wrestling", 57: "Yoga", 58: "Barre", 59: "Core Training",
    60: "Cross Country Skiing", 61: "Downhill Skiing", 62: "Flexibility",
    63: "High Intensity Interval Training", 64: "Jump Rope", 65: "Kickboxing",
    66: "Pilates", 67: "Snowboarding", 68: "Stairs", 69: "Step Training",
    70: "Wheelchair Walk Pace", 71: "Wheelchair Run Pace", 72: "Tai Chi",
    73: "Mixed Cardio", 74: "Hand Cycling", 75: "Disc Sports", 76: "Fitness Gaming",
    77: "Cardio Dance", 78: "Social Dance", 79: "Pickleball", 80: "Cooldown",
    82: "Swim Bike Run", 83: "Transition", 84: "Underwater Diving", 3000: "Other",
}


def workout_type_name(activity_type) -> str:
    return WORKOUT_TYPES.get(activity_type, f"Workout type {activity_type}")


def _local_day(column: str) -> str:
    return f"date({column} + {_APPLE_EPOCH_OFFSET}, 'unixepoch', 'localtime')"


def _round(value, digits: int = 0):
    if value is None:
        return None
    return int(round(value)) if digits == 0 else round(value, digits)


def _bpm(beats_per_second):
    return None if beats_per_second is None else _round(beats_per_second * 60)


class HealthExtractor:
    """Summarizes Apple Health data: daily metrics, sleep and workouts."""

    def get_summary(self, backup) -> dict:
        """Return the Health summary for a backup.

        ``{"available", "notice", "errors", "range", "daily", "sleep", "workouts"}``:
        ``daily`` has one row per local day with any of steps, distance_km,
        flights, active_energy_kcal, resting_heart_rate, heart_rate_min/avg/max
        and weight_kg; ``sleep`` one row per night (keyed by the date it ended);
        ``workouts`` newest first. ``notice`` explains missing data in
        unencrypted backups.
        """
        result = {"available": False, "notice": None, "errors": [], "range": None,
                  "daily": [], "sleep": [], "workouts": []}
        encrypted = getattr(backup, "encrypted", True)

        db_path = open_database(backup, SECURE_DB_PATH, HEALTH_DOMAIN)
        conn = self._connect(db_path) if db_path else None
        if conn is None:
            if not encrypted:
                result["notice"] = UNENCRYPTED_HEALTH_NOTICE
            elif db_path:
                result["errors"].append({"section": "health",
                                         "message": "The Health database in this backup couldn't be read."})
            return result

        result["available"] = True
        try:
            tables = table_names(conn)
            sources = self._source_names(backup)
            sections = (
                ("daily", "daily activity", lambda: self._daily(conn, tables)),
                ("sleep", "sleep", lambda: self._sleep(conn, tables)),
                ("workouts", "workouts", lambda: self._workouts(conn, tables, sources)),
            )
            for key, label, read in sections:
                try:
                    result[key] = read()
                except sqlite3.Error as e:
                    print(f"[health] {key} error: {e}", file=sys.stderr, flush=True)
                    result["errors"].append({"section": key, "message": f"Couldn't read {label} ({e})."})
        finally:
            conn.close()

        dates = [d["date"] for d in result["daily"]] + [s["date"] for s in result["sleep"]]
        if dates:
            result["range"] = {"first": min(dates), "last": max(dates)}
        return result

    # ── Plumbing ─────────────────────────────────────────────────────────────

    @staticmethod
    def _connect(db_path: str) -> Optional[sqlite3.Connection]:
        """Open the samples database, or None if it isn't one (e.g. a placeholder)."""
        try:
            conn = sqlite3.connect(db_path)
            conn.execute("PRAGMA query_only = TRUE")
            if "samples" not in table_names(conn):
                conn.close()
                return None
            return conn
        except sqlite3.DatabaseError:
            return None

    @staticmethod
    def _source_names(backup) -> dict:
        """Map data_provenances.source_id → the app or device name in healthdb.sqlite."""
        path = open_database(backup, MAIN_DB_PATH, HEALTH_DOMAIN)
        if not path:
            return {}
        try:
            conn = sqlite3.connect(path)
            try:
                if "name" not in column_names(conn, "sources"):
                    return {}
                return {rowid: name for rowid, name in conn.execute("SELECT ROWID, name FROM sources")}
            finally:
                conn.close()
        except sqlite3.Error:
            return {}

    @staticmethod
    def _provenance(conn, tables: set, owner: str) -> tuple:
        """Return (JOIN clause, source expression) linking *owner* to its source."""
        if ("objects" in tables and "data_provenances" in tables
                and {"data_id", "provenance"} <= column_names(conn, "objects")
                and "source_id" in column_names(conn, "data_provenances")):
            return (f"LEFT JOIN objects o ON o.data_id = {owner} "
                    "LEFT JOIN data_provenances p ON p.ROWID = o.provenance", "p.source_id")
        return "", "NULL"

    # ── Daily metrics ────────────────────────────────────────────────────────

    def _daily(self, conn, tables: set) -> list:
        if "quantity_samples" not in tables:
            return []
        days: dict = {}

        def day_row(day: str) -> dict:
            return days.setdefault(day, {"date": day})

        join, source = self._provenance(conn, tables, "s.data_id")
        for field, (data_type, factor, digits) in DAILY_TOTALS.items():
            best: dict = {}  # day → the largest single-source total
            for day, _src, total in conn.execute(f"""
                SELECT {_local_day('s.start_date')} AS day, {source} AS src, SUM(q.quantity)
                FROM samples s
                JOIN quantity_samples q ON q.data_id = s.data_id
                {join}
                WHERE s.data_type = ?
                GROUP BY day, src
            """, (data_type,)):
                if day and total is not None and total > best.get(day, -1):
                    best[day] = total
            for day, total in best.items():
                day_row(day)[field] = _round(total * factor, digits)

        # Heart rate: min/avg/max across sources (no double-counting issue).
        # Like iLEAPP, leave out objects of type 2 (meaning undocumented).
        has_object_type = "objects" in tables and "type" in column_names(conn, "objects")
        hr_join = "LEFT JOIN objects o ON o.data_id = s.data_id" if has_object_type else ""
        hr_where = "AND (o.type IS NULL OR o.type != 2)" if has_object_type else ""
        for day, lo, avg, hi in conn.execute(f"""
            SELECT {_local_day('s.start_date')} AS day, MIN(q.quantity), AVG(q.quantity), MAX(q.quantity)
            FROM samples s
            JOIN quantity_samples q ON q.data_id = s.data_id
            {hr_join}
            WHERE s.data_type = ? {hr_where}
            GROUP BY day
        """, (HEART_RATE,)):
            if day:
                row = day_row(day)
                row["heart_rate_min"] = _bpm(lo)
                row["heart_rate_avg"] = _bpm(avg)
                row["heart_rate_max"] = _bpm(hi)

        for field, data_type, digits in (("resting_heart_rate", RESTING_HEART_RATE, 0),
                                         ("weight_kg", BODY_MASS, 2)):
            for day, avg in conn.execute(f"""
                SELECT {_local_day('s.start_date')} AS day, AVG(q.quantity)
                FROM samples s
                JOIN quantity_samples q ON q.data_id = s.data_id
                WHERE s.data_type = ?
                GROUP BY day
            """, (data_type,)):
                if day and avg is not None:
                    day_row(day)[field] = _round(avg, digits)

        return [days[d] for d in sorted(days)]

    # ── Sleep ────────────────────────────────────────────────────────────────

    def _sleep(self, conn, tables: set) -> list:
        """One row per night, keyed by the local date the night ended.

        Asleep time and stages come from the source that recorded the most
        sleep that night (usually Apple Watch); time in bed is the most any
        source recorded (the iPhone's Sleep Focus often writes only that).
        """
        if "category_samples" not in tables:
            return []
        join, source = self._provenance(conn, tables, "s.data_id")
        nights: dict = {}
        for night, src, value, seconds in conn.execute(f"""
            SELECT {_local_day('s.end_date')} AS night, {source} AS src, c.value,
                   SUM(s.end_date - s.start_date)
            FROM samples s
            JOIN category_samples c ON c.data_id = s.data_id
            {join}
            WHERE s.data_type = ?
            GROUP BY night, src, c.value
        """, (SLEEP_ANALYSIS,)):
            if night and seconds:
                by_value = nights.setdefault(night, {}).setdefault(src, {})
                by_value[value] = by_value.get(value, 0) + seconds

        rows = []
        for night in sorted(nights):
            per_source = nights[night]
            best = max(per_source.values(), key=lambda v: sum(v.get(x, 0) for x in _ASLEEP_VALUES))
            asleep = sum(best.get(x, 0) for x in _ASLEEP_VALUES)
            in_bed = max(v.get(SLEEP_IN_BED, 0) for v in per_source.values())
            if not asleep and not in_bed:
                continue
            rows.append({
                "date": night,
                "asleep_minutes": _round(asleep / 60),
                "in_bed_minutes": _round(in_bed / 60),
                "awake_minutes": _round(best.get(SLEEP_AWAKE, 0) / 60),
                "core_minutes": _round(best.get(SLEEP_CORE, 0) / 60),
                "deep_minutes": _round(best.get(SLEEP_DEEP, 0) / 60),
                "rem_minutes": _round(best.get(SLEEP_REM, 0) / 60),
            })
        return rows

    # ── Workouts ─────────────────────────────────────────────────────────────

    def _workouts(self, conn, tables: set, sources: dict) -> list:
        if "workout_activities" in tables:
            rows = self._read_workout_activities(conn, tables)
        elif "workouts" in tables:
            rows = self._read_legacy_workouts(conn, tables)
        else:
            return []

        workouts = []
        for r in rows:
            start, end = r["start_date"], r["end_date"]
            duration = r["duration"] or ((end - start) if start and end else None)
            if r["distance_m"]:
                distance_km = r["distance_m"] / 1000
            elif r["total_distance_km"] and (r["activity_count"] or 1) <= 1:
                # The workout-level total; only safe to use for single-activity workouts.
                distance_km = r["total_distance_km"]
            else:
                distance_km = None
            location = r["location_type"]
            workouts.append({
                "id": r["id"],
                "activity_type": r["activity_type"],
                "type": workout_type_name(r["activity_type"]),
                "start": apple_to_iso(start),
                "end": apple_to_iso(end),
                "duration_minutes": _round(duration / 60, 1) if duration else None,
                "distance_km": _round(distance_km, 2),
                "energy_kcal": _round(r["energy_kcal"]) if r["energy_kcal"] else None,
                "avg_heart_rate": _bpm(r["avg_hr"]),
                "max_heart_rate": _bpm(r["max_hr"]),
                "indoor": True if location == _INDOOR else False if location == _OUTDOOR else None,
                "source": sources.get(r["source"]),
            })
        return workouts

    @staticmethod
    def _metadata_value(conn, tables: set, owner: str, key: str) -> str:
        if not {"metadata_values", "metadata_keys"} <= tables:
            return "NULL"
        return (f"(SELECT MAX(mv.numerical_value) FROM metadata_values mv "
                f"JOIN metadata_keys mk ON mk.ROWID = mv.key_id "
                f"WHERE mv.object_id = {owner} AND mk.key = '{key}')")

    def _read_workout_activities(self, conn, tables: set) -> list:
        """iOS 16+: one row per activity (a multisport workout has several)."""
        wa = column_names(conn, "workout_activities")
        w = column_names(conn, "workouts") if "workouts" in tables else set()

        def col(alias, cols, name):
            return f"{alias}.{name}" if name in cols else "NULL"

        def stat(data_type):
            if "workout_statistics" not in tables:
                return "NULL"
            return (f"(SELECT MAX(ws.quantity) FROM workout_statistics ws "
                    f"WHERE ws.workout_activity_id = wa.ROWID AND ws.data_type = {data_type})")

        join, source = self._provenance(conn, tables, "wa.owner_id")
        workouts_join = "LEFT JOIN workouts w ON w.data_id = wa.owner_id" if w else ""
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(f"""
                SELECT wa.ROWID AS id,
                       {col('wa', wa, 'activity_type')} AS activity_type,
                       {col('wa', wa, 'start_date')} AS start_date,
                       {col('wa', wa, 'end_date')} AS end_date,
                       {col('wa', wa, 'duration')} AS duration,
                       {col('wa', wa, 'location_type')} AS location_type,
                       {col('w', w, 'total_distance')} AS total_distance_km,
                       (SELECT COUNT(*) FROM workout_activities x WHERE x.owner_id = wa.owner_id)
                           AS activity_count,
                       {stat(_STAT_ACTIVE_ENERGY)} AS energy_kcal,
                       {stat(_STAT_WALK_RUN_DISTANCE)} AS distance_m,
                       {self._metadata_value(conn, tables, 'wa.owner_id', '_HKPrivateWorkoutAverageHeartRate')} AS avg_hr,
                       {self._metadata_value(conn, tables, 'wa.owner_id', '_HKPrivateWorkoutMaxHeartRate')} AS max_hr,
                       {source} AS source
                FROM workout_activities wa
                {workouts_join}
                {join}
                ORDER BY start_date DESC
            """).fetchall()
        finally:
            conn.row_factory = None

    def _read_legacy_workouts(self, conn, tables: set) -> list:
        """iOS 15 and earlier: one row per workout in the workouts table."""
        w = column_names(conn, "workouts")

        def col(name):
            return f"w.{name}" if name in w else "NULL"

        join, source = self._provenance(conn, tables, "w.data_id")
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(f"""
                SELECT w.data_id AS id,
                       {col('activity_type')} AS activity_type,
                       s.start_date AS start_date,
                       s.end_date AS end_date,
                       {col('duration')} AS duration,
                       NULL AS location_type,
                       {col('total_distance')} AS total_distance_km,
                       1 AS activity_count,
                       {col('total_energy_burned')} AS energy_kcal,
                       NULL AS distance_m,
                       {self._metadata_value(conn, tables, 'w.data_id', '_HKPrivateWorkoutAverageHeartRate')} AS avg_hr,
                       {self._metadata_value(conn, tables, 'w.data_id', '_HKPrivateWorkoutMaxHeartRate')} AS max_hr,
                       {source} AS source
                FROM workouts w
                LEFT JOIN samples s ON s.data_id = w.data_id
                {join}
                ORDER BY s.start_date DESC
            """).fetchall()
        finally:
            conn.row_factory = None
