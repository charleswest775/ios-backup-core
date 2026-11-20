"""backupgen's Calendar and Voice Memos writers.

Maren's pack is the showcase (iOS 26.5: the Simulator's Calendar schema and Apple's
VoiceMemos14 model). A copy of the test persona with a small calendar.json and
voice_memos.txt covers the older layouts, including an iOS 13.4.1 snapshot (Voice Memos in
MediaDomain, no folders or favourites). Everything is read back with plain SQL and a small
MP4 box reader written here, from the encodings the research found, so these tests don't
depend on the readers being written alongside them. Photo downloads are replaced by small
generated images, so nothing needs the network.
"""

import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import struct
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, ROOT)

from backupgen import PERSONAS_DIR, harvest, media  # noqa: E402
from backupgen.generate import EXPECTED, generate  # noqa: E402
from backupgen.persona import PersonaError, load_persona  # noqa: E402
from backupgen.report import render  # noqa: E402
from backupgen.sqlitegen import load_schema  # noqa: E402
from backupgen.writers import voice_memos as vm_writer  # noqa: E402
from ios_backup_core.backup import open_local_backup  # noqa: E402

CALENDAR = ("HomeDomain", "Library/Calendar/Calendar.sqlitedb")
SHARED = "AppDomainGroup-group.com.apple.VoiceMemos.shared"
COCOA = datetime(2001, 1, 1, tzinfo=timezone.utc)
PACIFIC = ZoneInfo("America/Los_Angeles")


def _fake_fetch(url, sha1):
    """A small JPEG coloured by the photo's hash, in place of a download."""
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (64, 48), "#" + hashlib.sha1(url.encode()).hexdigest()[:6]).save(out, "JPEG")
    return out.getvalue()


def _generate(persona, snapshot, out, password=None, pack=None):
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(media, "fetch", _fake_fetch)
        backup_dir = generate(persona, snapshot, out, password=password, persona=pack)
    expected = json.loads((out / EXPECTED).read_text(encoding="utf-8"))
    accessor = open_local_backup(str(backup_dir), password=password)
    return SimpleNamespace(backup_dir=backup_dir, expected=expected, accessor=accessor, password=password)


@pytest.fixture(scope="module")
def maren_plain(tmp_path_factory):
    return _generate("maren", "ios26", tmp_path_factory.mktemp("maren-plain"))


@pytest.fixture(scope="module")
def maren_encrypted(tmp_path_factory):
    return _generate("maren", "ios26", tmp_path_factory.mktemp("maren-enc"), password="pw")


@pytest.fixture(params=["plain", "encrypted"])
def maren(request):
    return request.getfixturevalue(f"maren_{request.param}")


def _open(gen, domain, path):
    local = gen.accessor.get_file(path, domain)
    assert local, f"{domain} {path} isn't in the backup"
    db = sqlite3.connect(Path(local).resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    db.row_factory = sqlite3.Row
    return db


def _paths(gen, domain=None):
    return {(f["domain"], f["path"]) for f in gen.accessor.list_files(domain=domain)}


# ── a reader of our own, from the research's encodings ───────────────────────

_STATUS = {0: "none", 1: "confirmed", 2: "tentative", 3: "cancelled"}
_AVAILABILITY = {0: "busy", 1: "free", 2: "tentative", 3: "unavailable"}
_PARTSTAT = {0: "needs_action", 1: "accepted", 2: "declined", 3: "tentative"}
_ROLE = {0: "unknown", 1: "required", 2: "optional", 3: "chair", 4: "non_participant"}
_FREQUENCY = {1: "daily", 2: "weekly", 3: "monthly", 4: "yearly"}
_WEEKDAY = {1: "MO", 2: "TU", 3: "WE", 4: "TH", 5: "FR", 6: "SA", 7: "SU"}


def _instant(value):
    return COCOA + timedelta(seconds=value)


def _typed(value, all_day, floating):
    if value is None:
        return None
    moment = _instant(value)
    if all_day:
        return moment.date().isoformat()
    return moment.replace(tzinfo=None).isoformat() if floating else moment.isoformat()


def _specifier(text):
    """Apple's BY* rules: D=+4TH,0TU,-1MO (0: every such day) / M= month days / O= months / S= / Y= / W=."""
    parts = dict(p.split("=", 1) for p in (text or "").split(";") if p)
    days = [t[1:] if t.startswith("0") else t.lstrip("+") for t in parts.get("D", "").split(",") if t]
    numbers = {key: [int(x) for x in parts.get(key, "").split(",") if x] for key in "MOSYW"}
    return days, numbers


def read_events(db):
    """Calendar events as a reader should produce them, keyed like the answer key."""
    owner_email = {r["ROWID"]: r["self_identity_email"] for r in db.execute("SELECT ROWID, self_identity_email FROM Calendar")}
    titles = {r["ROWID"]: r["title"] for r in db.execute("SELECT ROWID, title FROM Calendar")}
    events = []
    for row in db.execute("SELECT * FROM CalendarItem WHERE entity_type = 2 ORDER BY ROWID"):
        all_day, floating = bool(row["all_day"]), row["start_tz"] == "_float"
        record = {"id": row["ROWID"], "uid": row["unique_identifier"], "calendar": titles[row["calendar_id"]],
                  "calendar_id": row["calendar_id"], "title": row["summary"], "notes": row["description"],
                  "url": row["url"], "conference_url": row["conference_url"], "all_day": all_day,
                  "floating": floating, "start": None, "end": None,
                  "start_day": None, "end_day": None, "days": None, "wall_start": None, "wall_end": None,
                  "time_zone": None, "end_time_zone": None, "status": _STATUS[row["status"]],
                  "availability": _AVAILABILITY[row["availability"]], "location": None, "recurrence": None,
                  "exceptions": [], "detached_from": None, "organizer": None, "attendees": [], "alarms": [],
                  "year_unknown": False,
                  "created_date": _instant(row["creation_date"]).isoformat() if row["creation_date"] is not None else None,
                  "modified_date": (_instant(row["last_modified"]).isoformat()
                                    if row["last_modified"] is not None else None)}
        if all_day:
            days = max(1, round((row["end_date"] - row["start_date"] + 1) / 86400))
            first = _instant(row["start_date"]).date()
            record.update(start_day=first.isoformat(), days=days, end_day=(first + timedelta(days=days - 1)).isoformat())
        elif floating:
            record.update(wall_start=_typed(row["start_date"], False, True), wall_end=_typed(row["end_date"], False, True))
        else:
            record.update(start=_typed(row["start_date"], False, False), end=_typed(row["end_date"], False, False),
                          time_zone=row["start_tz"], end_time_zone=row["end_tz"] if row["end_tz"] != row["start_tz"] else None)
        if row["location_id"]:
            loc = db.execute("SELECT * FROM Location WHERE ROWID = ?", (row["location_id"],)).fetchone()
            lat, lon = loc["latitude"], loc["longitude"]
            if lat == 0 and lon == 0:
                lat = lon = None
            record["location"] = {"title": loc["title"], "address": loc["address"], "latitude": lat, "longitude": lon}
        me = owner_email[row["calendar_id"]]
        if row["organizer_id"]:
            org = db.execute("SELECT p.*, i.display_name FROM Participant p LEFT JOIN Identity i ON i.rowid = p.identity_id "
                             "WHERE p.ROWID = ?", (row["organizer_id"],)).fetchone()
            assert org["entity_type"] == 8
            record["organizer"] = {"name": org["display_name"], "email": org["email"],
                                   "is_me": bool(me) and org["email"].lower() == me.lower()}
        for p in db.execute("SELECT p.*, i.display_name, i.address FROM Participant p LEFT JOIN Identity i "
                            "ON i.rowid = p.identity_id WHERE p.owner_id = ? AND p.entity_type = 7 ORDER BY p.ROWID",
                            (row["ROWID"],)):
            assert p["address"] == f"mailto:{p['email']}"
            record["attendees"].append({"name": p["display_name"], "email": p["email"], "phone": p["phone_number"],
                                        "status": _PARTSTAT[p["status"]], "role": _ROLE[p["role"]],
                                        "type": "person" if p["type"] == 1 else "unknown",
                                        "is_me": p["ROWID"] == row["self_attendee_id"]})
        for a in db.execute("SELECT * FROM Alarm WHERE calendaritem_owner_id = ? ORDER BY ROWID", (row["ROWID"],)):
            if a["default_alarm"] != 1 and a["trigger_interval"] is not None:
                record["alarms"].append({"seconds_before": -a["trigger_interval"]})
        if row["has_recurrences"]:
            r = db.execute("SELECT * FROM Recurrence WHERE owner_id = ? ORDER BY ROWID", (row["ROWID"],)).fetchone()
            days, numbers = _specifier(r["specifier"])
            record["recurrence"] = {
                "frequency": _FREQUENCY[r["frequency"]], "interval": r["interval"], "count": r["count"] or None,
                "until": _typed(r["end_date"], all_day, floating), "by_day": days, "by_month_day": numbers["M"],
                "by_month": numbers["O"], "by_set_pos": numbers["S"], "by_year_day": numbers["Y"],
                "by_week_no": numbers["W"], "week_start": _WEEKDAY.get(r["week_start"]),
                "last_start": _typed(r["cached_end_date"], all_day, floating)}
            record["exceptions"] = sorted(_typed(e["date"], all_day, floating) for e in
                                          db.execute("SELECT date FROM ExceptionDate WHERE owner_id = ?", (row["ROWID"],)))
        if row["orig_item_id"]:
            record["detached_from"] = {"event_id": row["orig_item_id"],
                                       "original_start": _typed(row["orig_date"], all_day, floating)}
        events.append(record)
    return events


def read_memos(gen, domain, folder):
    """Voice memos as a reader should list them, keyed like the answer key."""
    db = _open(gen, domain, f"{folder}/CloudRecordings.db")
    columns = {r["name"] for r in db.execute("PRAGMA table_info(ZCLOUDRECORDING)")}
    folders = bool(db.execute("SELECT 1 FROM sqlite_master WHERE name = 'ZFOLDER'").fetchone())
    join = " LEFT JOIN ZFOLDER f ON f.Z_PK = r.ZFOLDER" if folders else ""
    extra = ", f.ZUUID AS folder_id, f.ZENCRYPTEDNAME AS folder" if folders else ""
    listed = {Path(p).name: p for d, p in _paths(gen, domain) if p.startswith(folder + "/") and p.endswith(".m4a")}
    memos = []
    for r in db.execute(f"SELECT r.*{extra} FROM ZCLOUDRECORDING r{join} ORDER BY r.ZDATE DESC, r.Z_PK DESC"):
        title = next((r[c] for c in ("ZCUSTOMLABELFORSORTING", "ZENCRYPTEDTITLE", "ZCUSTOMLABEL") if c in columns and r[c]), None)
        name = Path(r["ZPATH"]).name if r["ZPATH"] else None
        if name not in listed:
            name = next((n for n in listed if re.match(rf"^\d{{8}} \d{{6}}-{r['ZUNIQUEID'][:8]}\.m4a$", n, re.IGNORECASE)), None)
        local = r["ZLOCALDURATION"] if "ZLOCALDURATION" in columns else None
        flags, shared = r["ZFLAGS"], r["ZSHAREDFLAGS"] if "ZSHAREDFLAGS" in columns else None
        memos.append({
            "id": r["Z_PK"], "unique_id": r["ZUNIQUEID"], "title": title, "date": _instant(r["ZDATE"]).isoformat(),
            "duration_seconds": local if local and local > 0 else r["ZDURATION"],
            "folder": r["folder"] if folders else None, "folder_id": r["folder_id"] if folders else None,
            "is_favorite": bool(shared & 1) if shared is not None else None,
            "is_deleted": r["ZEVICTIONDATE"] is not None,
            "deleted_date": _instant(r["ZEVICTIONDATE"]).isoformat() if r["ZEVICTIONDATE"] is not None else None,
            "in_phone_list": (flags & 6) != 0, "recorded_on_watch": bool(flags & 0x40 or (shared or 0) & 0x40),
            "is_enhanced": bool(shared & 2) if shared is not None else None,
            "is_layered": bool(flags & 0x800), "is_spatial": bool(flags & 0x1000),
            "has_transcript": bool(flags & 0x400), "file_name": name or (Path(r["ZPATH"]).name if r["ZPATH"] else None),
            "format": "m4a" if name else None, "audio_available": name is not None,
            "audio_missing_reason": None if name else ("in_icloud_only" if flags & 2 else "not_in_backup"),
            "bytes": len(Path(gen.accessor.get_file(listed[name], domain)).read_bytes()) if name else None,
            "source": "CloudRecordings.db",
        })
    return memos, db


# ── MP4 boxes ────────────────────────────────────────────────────────────────

_CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"udta", b"edts", b"dinf", b"ilst", b"----"}


def boxes(data, start=0, end=None):
    """{path: payload} for every box, e.g. 'moov/trak/mdia/mdhd' (the first of each path)."""
    out = {}

    def walk(start, end, prefix):
        pos = start
        while pos + 8 <= end:
            size, kind = struct.unpack(">I4s", data[pos:pos + 8])
            assert 8 <= size <= end - pos, "box runs past its parent"
            path = prefix + kind.decode("latin-1")
            out.setdefault(path, data[pos + 8:pos + size])
            if kind in _CONTAINERS:
                walk(pos + 8, pos + size, path + "/")
            elif kind == b"meta":
                walk(pos + 12, pos + size, path + "/")  # ISO meta: version and flags first
            pos += size
        assert pos == end, "boxes don't fill their parent"

    walk(start, len(data) if end is None else end, "")
    return out


def _data(payload):
    assert payload[4:8] == b"data"
    assert struct.unpack(">I", payload[8:12])[0] == 1, "UTF-8 text"
    return payload[16:].decode("utf-8")


# ── Maren ────────────────────────────────────────────────────────────────────


def test_maren_has_both_in_her_answer_key(maren):
    expected = maren.expected
    assert "calendar" not in expected["absent"] and "voice_memos" not in expected["absent"]
    calendars = {c["title"]: c for c in expected["calendar"]["calendars"]}
    assert set(calendars) == {"Default", "Birthdays", "Home", "Work", "US Holidays", "Found in Mail"}
    assert {t: c["kind"] for t, c in calendars.items()} == {"Default": "normal", "Birthdays": "birthdays", "Home": "normal",
                                                            "Work": "normal", "US Holidays": "holidays",
                                                            "Found in Mail": "suggestions"}
    assert calendars["Default"]["event_count"] == calendars["Birthdays"]["event_count"] == 0
    events = expected["calendar"]["events"]
    assert len(events) == 43  # 42 in the pack plus the moved climbing night
    assert sum(c["event_count"] for c in calendars.values()) == len(events)
    for title, calendar in calendars.items():
        assert calendar["event_count"] == sum(e["calendar"] == title for e in events), title
    assert len(expected["voice_memos"]) == 12


def test_both_answer_keys_are_the_same_plain_and_encrypted(maren_plain, maren_encrypted):
    for key in ("calendar", "voice_memos"):
        assert maren_plain.expected[key] == maren_encrypted.expected[key]


def test_calendar_rows_read_back_as_the_answer_key(maren):
    db = _open(maren, *CALENDAR)
    events = maren.expected["calendar"]["events"]
    assert read_events(db) == [{k: v for k, v in e.items() if k != "pack_id"} for e in events]


def test_calendar_accounts_and_calendars(maren):
    db = _open(maren, *CALENDAR)
    stores = {r["ROWID"]: (r["name"], r["type"]) for r in db.execute("SELECT * FROM Store")}
    assert stores == {1: ("Local", 0), 2: ("Other", 5), 3: ("iCloud", 2), 4: ("Subscribed Calendars", 4)}
    assert db.execute("SELECT owner_name FROM Store WHERE type = 2").fetchone()[0] == "Maren Lindqvist"
    props = dict(db.execute("SELECT key, value FROM _SqliteDatabaseProperties").fetchall())
    calendars = {r["title"]: r for r in db.execute("SELECT * FROM Calendar")}
    assert props["BirthdayCalendarID"] == str(calendars["Birthdays"]["ROWID"])
    assert props["SuggestionsCalendarID"] == str(calendars["Found in Mail"]["ROWID"])
    assert props["NativeStoreID"] == "2" and props["OccurrenceCacheTimeZone"] == "America/Los_Angeles"
    holidays = calendars["US Holidays"]
    assert holidays["external_id"] == "__HolidayCalendar__" and holidays["subcal_url"].startswith("https://")
    assert holidays["flags"] & 1 and calendars["Birthdays"]["flags"] & 1 and not calendars["Home"]["flags"] & 1
    assert calendars["Home"]["color"] == "#34AADCFF"  # iCloud stores the alpha too
    assert calendars["Home"]["self_identity_email"] == "maren.lindqvist@example.com"
    assert calendars["Default"]["supported_entity_types"] is None
    by_id = {c["id"]: c for c in maren.expected["calendar"]["calendars"]}
    assert by_id[calendars["Home"]["ROWID"]]["color"] == "#34AADC"
    counts = dict(db.execute("SELECT calendar_id, COUNT(*) FROM CalendarItem GROUP BY calendar_id").fetchall())
    assert {c["id"]: c["event_count"] for c in by_id.values() if c["event_count"]} == counts
    assert db.execute("SELECT COUNT(*) FROM OccurrenceCache").fetchone()[0] == 0  # not written (the reader doesn't use it)


def test_calendar_encodings(maren):
    db = _open(maren, *CALENDAR)
    rows = {r["summary"]: r for r in db.execute("SELECT * FROM CalendarItem WHERE orig_item_id = 0")}
    for r in db.execute("SELECT * FROM CalendarItem"):
        assert (r["entity_type"], r["birthday_id"], r["created_by_id"], r["can_forward"], r["hidden"]) == (2, -1, -1, 1, 0)
        assert r["UUID"] != r["unique_identifier"]
        assert None not in (r["location_id"], r["organizer_id"], r["self_attendee_id"])  # 0 when unused, as on a device
        if r["all_day"]:
            assert r["start_tz"] == r["end_tz"] == "_float" and r["start_date"] % 86400 == 0  # midnight UTC
            assert (r["end_date"] - r["start_date"] + 1) % 86400 == 0  # 23:59:59 of the last day
    uuids = [r[0] for r in db.execute("SELECT UUID FROM CalendarItem")]
    assert len(set(uuids)) == len(uuids)

    trip = rows["Yellowstone road trip"]
    assert trip["end_date"] - trip["start_date"] == 8 * 86400 - 1
    assert _instant(trip["start_date"]) == datetime(2025, 8, 9, tzinfo=timezone.utc)
    sunrise = rows["Sunrise at Reflection Lakes"]  # floating: the wall clock as if it were UTC
    assert (sunrise["all_day"], sunrise["start_tz"]) == (0, "_float")
    assert _instant(sunrise["start_date"]) == datetime(2024, 8, 18, 5, 15, tzinfo=timezone.utc)
    lamar = rows["Lamar Valley at dawn"]  # 04:30 in Yellowstone is 10:30 UTC in August
    assert lamar["start_tz"] == "America/Denver" and _instant(lamar["start_date"]).hour == 10
    flight = rows["Fly to Missoula"]
    assert (flight["start_tz"], flight["end_tz"]) == ("America/Los_Angeles", "America/Denver")
    assert flight["end_date"] - flight["start_date"] == 2 * 3600 + 20 * 60
    assert rows["Cottonwood site visit with Natalie"]["status"] == 3
    assert rows["Thanksgiving"]["availability"] == 1 and rows["Thanksgiving"]["creation_date"] is None
    assert rows["Climbing at Basalt"]["creation_date"] is not None
    leah = rows["Leah's birthday"]
    alarm = db.execute("SELECT trigger_interval FROM Alarm WHERE calendaritem_owner_id = ? AND default_alarm = 0",
                       (leah["ROWID"],)).fetchone()[0]
    assert alarm == 9 * 3600  # 9 AM on the day: after the all-day start
    dentist = rows["Dentist cleaning"]
    assert [r[0] for r in db.execute("SELECT trigger_interval FROM Alarm WHERE calendaritem_owner_id = ? AND default_alarm = 0 "
                                     "ORDER BY ROWID", (dentist["ROWID"],))] == [-86400, -3600]
    # iOS 16 on: one default-alert placeholder per event, with no interval.
    assert db.execute("SELECT COUNT(*) FROM Alarm WHERE default_alarm = 1 AND trigger_interval IS NULL").fetchone()[0] == \
        db.execute("SELECT COUNT(*) FROM CalendarItem").fetchone()[0]
    # Events made before the backup but starting after it are there, as on a phone.
    backup = datetime.fromisoformat(maren.expected["backup_date"])
    future = {t for t, r in rows.items() if _instant(r["start_date"]) > backup}
    assert {"Erik & Jess's wedding", "Dentist cleaning", "Goat Rocks?"} <= future


def test_calendar_repeats_skips_and_moves(maren):
    db = _open(maren, *CALENDAR)
    rows = {r["summary"]: r for r in db.execute("SELECT * FROM CalendarItem WHERE orig_item_id = 0")}

    def rule(title):
        return db.execute("SELECT * FROM Recurrence WHERE owner_id = ?", (rows[title]["ROWID"],)).fetchone()

    thanksgiving = rule("Thanksgiving")
    assert (thanksgiving["frequency"], thanksgiving["count"], thanksgiving["specifier"], thanksgiving["week_start"]) == \
        (4, 10, "D=+4TH;O=11", 7)
    assert _instant(thanksgiving["cached_end_date"]).date() == date(2031, 11, 27)  # the last one's start
    assert rule("Memorial Day")["specifier"] == "D=-1MO;O=5"
    assert rule("Juneteenth")["specifier"] is None  # a fixed date needs no BY rules
    birthday = rule("Theo's birthday")
    assert (birthday["count"], birthday["end_date"], birthday["cached_end_date"]) == (0, None, None)  # never ends
    climbing = rule("Climbing at Basalt")
    assert (climbing["frequency"], climbing["specifier"], climbing["cached_end_date"]) == (2, "D=0TU", None)
    checkin = rule("Cottonwood check-in")
    assert (checkin["frequency"], checkin["interval"], checkin["count"]) == (2, 2, 20)
    assert _instant(checkin["cached_end_date"]).astimezone(PACIFIC) == datetime(2025, 11, 27, 15, tzinfo=PACIFIC)
    sale = rule("Thimbleberry plant sale")
    assert (sale["frequency"], sale["specifier"], sale["count"]) == (3, "D=+1SA", 0)
    assert _instant(sale["end_date"]).date() == date(2025, 10, 31) and _instant(sale["cached_end_date"]).date() == date(2025, 10, 4)
    drops = rule("Juniper: ear drops")
    assert (drops["frequency"], drops["count"]) == (1, 10)

    # A skipped Tuesday (Yellowstone) and one moved to Wednesday (the DD presentation).
    skips = [r[0] for r in db.execute("SELECT date FROM ExceptionDate WHERE owner_id = ?", (rows["Climbing at Basalt"]["ROWID"],))]
    assert [_instant(s).astimezone(PACIFIC) for s in skips] == [datetime(2025, 8, 12, 19, tzinfo=PACIFIC)]
    moved = db.execute("SELECT * FROM CalendarItem WHERE orig_item_id != 0").fetchall()
    assert len(moved) == 1
    moved = moved[0]
    master = rows["Climbing at Basalt"]
    assert moved["orig_item_id"] == master["ROWID"] and moved["unique_identifier"] == master["unique_identifier"]
    assert _instant(moved["orig_date"]).astimezone(PACIFIC) == datetime(2025, 11, 18, 19, tzinfo=PACIFIC)
    assert _instant(moved["start_date"]).astimezone(PACIFIC) == datetime(2025, 11, 19, 19, tzinfo=PACIFIC)
    assert moved["has_recurrences"] == 0


def test_calendar_people(maren):
    db = _open(maren, *CALENDAR)
    rows = {r["summary"]: r for r in db.execute("SELECT * FROM CalendarItem WHERE orig_item_id = 0")}
    events = {e["title"]: e for e in maren.expected["calendar"]["events"]}
    mine = rows["Linden kids' design workshop"]
    organizer = db.execute("SELECT * FROM Participant WHERE ROWID = ?", (mine["organizer_id"],)).fetchone()
    assert (organizer["entity_type"], organizer["email"], organizer["is_self"]) == (8, "maren.lindqvist@example.com", 1)
    chair = db.execute("SELECT * FROM Participant WHERE ROWID = ?", (mine["self_attendee_id"],)).fetchone()
    assert (chair["entity_type"], chair["role"], chair["status"]) == (7, 3, 1)
    invited = rows["Cottonwood Landing design development presentation"]
    me = db.execute("SELECT * FROM Participant WHERE ROWID = ?", (invited["self_attendee_id"],)).fetchone()
    assert (me["role"], me["status"], me["email"]) == (1, 1, "maren.lindqvist@example.com")
    assert events["Cottonwood Landing design development presentation"]["organizer"] == \
        {"name": "Natalie Cho", "email": "ncho@example.org", "is_me": False}
    answers = {a["name"]: a["status"] for a in events["DD dry run"]["attendees"]}
    assert answers == {"Maren Lindqvist": "accepted", "Owen Castellanos": "accepted", "Kenji Mori": "declined"}
    assert {a["status"] for a in events["Wizard Island boat tour"]["attendees"]} == {"accepted", "tentative"}
    # One Identity per person, however many events they're in.
    addresses = [r[0] for r in db.execute("SELECT address FROM Identity")]
    assert len(addresses) == len(set(addresses)) and all(a.startswith("mailto:") for a in addresses)
    assert rows["Linden kids' design workshop"]["has_attendees"] == 1 and rows["Dentist cleaning"]["has_attendees"] == 0


def test_calendar_database_is_stored_like_a_device_copy(maren):
    local = maren.accessor.get_file(CALENDAR[1], CALENDAR[0])
    assert Path(local).read_bytes()[18] == 2, "WAL mode"
    assert not any(p.endswith(("-wal", "-shm")) for _, p in _paths(maren, "HomeDomain"))


def test_voice_memos_read_back_as_the_answer_key(maren):
    paths = _paths(maren)
    assert (SHARED, "Recordings/CloudRecordings.db") in paths
    assert not any(d == "MediaDomain" and "Recordings" in p for d, p in paths)
    memos, _ = read_memos(maren, SHARED, "Recordings")
    assert memos == [{k: v for k, v in m.items() if k != "transcript"} for m in maren.expected["voice_memos"]]


def test_voice_memo_rows(maren):
    _, db = read_memos(maren, SHARED, "Recordings")
    rows = {r["ZCUSTOMLABEL"]: r for r in db.execute("SELECT * FROM ZCLOUDRECORDING")}
    for r in rows.values():
        assert r["ZCUSTOMLABEL"] == r["ZENCRYPTEDTITLE"] == r["ZCUSTOMLABELFORSORTING"]
        assert re.fullmatch(r"[0-9A-F]{8}-[0-9A-F]{4}-4[0-9A-F]{3}-[89AB][0-9A-F]{3}-[0-9A-F]{12}", r["ZUNIQUEID"])
        if r["ZPATH"]:
            local = _instant(r["ZDATE"]).astimezone(PACIFIC)
            assert r["ZPATH"] == f"{local:%Y%m%d %H%M%S}-{r['ZUNIQUEID'][:8]}.m4a"
    assert rows["Marmot whistle"]["ZFLAGS"] == 0x4 and rows["Marmot whistle"]["ZSHAREDFLAGS"] == 1
    assert rows["DD presentation run-through"]["ZFLAGS"] == 0x4 | 0x200 | 0x400
    cloud = rows["Permit comments call"]
    assert (cloud["ZFLAGS"], cloud["ZPATH"], cloud["ZLOCALDURATION"], cloud["ZDURATION"]) == (0x2, None, 0.0, 1440.0)
    deleted = rows["New Recording 14"]
    assert deleted["ZEVICTIONDATE"] - deleted["ZDATE"] == 86400
    assert deleted["ZEVICTIONDATE"] > (datetime.fromisoformat(maren.expected["backup_date"]) - timedelta(days=30)
                                       - COCOA).total_seconds()
    folders = db.execute("SELECT * FROM ZFOLDER").fetchall()
    assert [(f["ZENCRYPTEDNAME"], f["ZCOUNTOFRECORDINGS"], f["ZRANK"]) for f in folders] == [("Site visits", 3, 0)]
    assert {r["ZCUSTOMLABEL"] for r in rows.values() if r["ZFOLDER"] == folders[0]["Z_PK"]} == \
        {"Linden site walk", "Cottonwood site walk", "Linden grading check with Ray"}
    entities = {r["Z_NAME"]: (r["Z_ENT"], r["Z_MAX"]) for r in db.execute("SELECT * FROM Z_PRIMARYKEY")}
    assert entities["CloudRecording"] == (1, 12) and entities["Folder"] == (4, 1)
    assert {r["Z_ENT"] for r in rows.values()} == {1}
    assert db.execute("SELECT COUNT(*) FROM ZRECORDING").fetchone()[0] == 0
    by_title = {m["title"]: m for m in maren.expected["voice_memos"]}
    assert by_title["Permit comments call"]["audio_missing_reason"] == "in_icloud_only"
    assert by_title["Cottonwood site walk"]["is_favorite"] and by_title["Cottonwood site walk"]["folder"] == "Site visits"
    assert [m["title"] for m in maren.expected["voice_memos"]][:2] == ["New Recording 14", "Frog pond tour"]  # newest first


def test_recordings_are_tagged_silent_aac(maren):
    rows = {r["ZPATH"]: r for r in read_memos(maren, SHARED, "Recordings")[1].execute("SELECT * FROM ZCLOUDRECORDING")}
    for memo in maren.expected["voice_memos"]:
        if not memo["file_name"]:
            continue
        data = Path(maren.accessor.get_file(f"Recordings/{memo['file_name']}", SHARED)).read_bytes()
        tree = boxes(data)
        assert tree["ftyp"][:4] == b"M4A "
        timescale, duration = struct.unpack(">II", tree["moov/mvhd"][12:20])
        assert (timescale, duration) == (1000, round(memo["duration_seconds"] * 1000))
        rate, samples = struct.unpack(">II", tree["moov/trak/mdia/mdhd"][12:20])
        assert rate == 48000 and tree["moov/trak/mdia/hdlr"][8:12] == b"soun"
        assert tree["moov/trak/mdia/minf/stbl/stsd"][12:16] == b"mp4a"
        sample_size, frames = struct.unpack(">II", tree["moov/trak/mdia/minf/stbl/stsz"][4:12])
        assert sample_size == 4 and samples == frames * 1024 and len(tree["mdat"]) == 4 * frames
        # The edit list skips the encoder's 2112 samples of delay, so it plays for exactly the recorded length.
        count, segment, media_time = struct.unpack(">IIi", tree["moov/trak/edts/elst"][4:16])
        assert (count, segment, media_time) == (1, duration, 2112)
        assert samples >= 2112 + memo["duration_seconds"] * 48000
        assert _data(tree["moov/udta/meta/ilst/\xa9nam"]) == memo["title"]
        assert _data(tree["moov/udta/meta/ilst/\xa9too"]) == "com.apple.VoiceMemos (iPhone Version 26.5 (Build 23F77))"
        assert tree["moov/udta/meta/ilst/----/mean"][4:] == b"com.apple.iTunes"
        assert tree["moov/udta/meta/ilst/----/name"][4:] == b"voice-memo-uuid"
        assert tree["moov/udta/meta/ilst/----/data"][8:].decode() == memo["unique_id"] == rows[memo["file_name"]]["ZUNIQUEID"]
        assert ("moov/trak/udta/tsrp" in tree) == memo["has_transcript"]
        assert memo["bytes"] == len(data)


def test_transcript_box_holds_apples_json(maren_plain):
    memo = next(m for m in maren_plain.expected["voice_memos"] if m["has_transcript"])
    data = Path(maren_plain.accessor.get_file(f"Recordings/{memo['file_name']}", SHARED)).read_bytes()
    doc = json.loads(boxes(data)["moov/trak/udta/tsrp"])
    runs, table = doc["attributedString"]["runs"], doc["attributedString"]["attributeTable"]
    assert "".join(runs[0::2]) == memo["transcript"] and runs[1::2] == list(range(len(table)))
    starts = [t["timeRange"][0] for t in table]
    assert starts == sorted(starts) and table[-1]["timeRange"][1] <= memo["duration_seconds"]
    assert doc["locale"] == {"identifier": "en_US", "current": 0}


def test_transcript_round_trips_through_the_reader(maren_plain):
    mp4 = pytest.importorskip("ios_backup_core.mp4")
    for memo in maren_plain.expected["voice_memos"]:
        if not memo["file_name"]:
            continue
        path = maren_plain.accessor.get_file(f"Recordings/{memo['file_name']}", SHARED)
        transcript = mp4.read_transcript(path)
        if memo["has_transcript"]:
            assert transcript["text"] == memo["transcript"] and transcript["locale"] == "en_US"
            assert "".join(s["text"] for s in transcript["segments"]) == memo["transcript"]
        else:
            assert transcript is None
        if hasattr(mp4, "read_tags"):
            assert mp4.read_tags(path).get("unique_id") == memo["unique_id"]
        if hasattr(mp4, "audio_codec"):
            assert mp4.audio_codec(path) == "mp4a"


@pytest.mark.skipif(not (shutil.which("ffprobe") or shutil.which("afinfo")), reason="needs ffprobe or afinfo")
def test_media_tools_agree_on_the_length(tmp_path):
    path = tmp_path / "memo.m4a"
    path.write_bytes(media.silent_m4a(62, 1_780_000_000, title="Test"))
    if shutil.which("ffprobe"):
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_name,sample_rate",
                              "-of", "default=nw=1", str(path)], capture_output=True, text=True, check=True).stdout
        assert "codec_name=aac" in out and "sample_rate=48000" in out and "duration=62.000000" in out
    if shutil.which("afinfo"):
        out = subprocess.run(["afinfo", str(path)], capture_output=True, text=True, check=True).stdout
        assert "1 ch,  48000 Hz, aac" in out  # afinfo ignores the edit list, so its length runs 15 ms long


def test_silent_m4a_lengths_are_exact():
    for seconds in (0.5, 1, 21, 62, 1440, 6720):
        tree = boxes(media.silent_m4a(seconds))
        assert struct.unpack(">I", tree["moov/mvhd"][16:20])[0] == round(seconds * 1000)
        assert media.m4a_duration(seconds) == seconds
    assert "moov/udta" not in boxes(media.silent_m4a(3))  # no tags asked for, none written


def test_file_names_use_the_week_year():
    assert vm_writer.week_year(date(2024, 12, 28)) == 2024  # a Saturday
    assert vm_writer.week_year(date(2024, 12, 29)) == 2025  # its week holds 1 January
    assert vm_writer.week_year(date(2021, 1, 1)) == 2021
    assert vm_writer.file_name(datetime(2024, 12, 30, 9, 5, 7), "ABCDEF12-0000") == "20251230 090507-ABCDEF12.m4a"


def test_generation_is_deterministic(maren_plain, tmp_path):
    again = _generate("maren", "ios26", tmp_path)
    assert again.expected["calendar"] == maren_plain.expected["calendar"]
    assert again.expected["voice_memos"] == maren_plain.expected["voice_memos"]
    def files(gen):
        return {p.relative_to(gen.backup_dir): p.read_bytes() for p in gen.backup_dir.rglob("*") if p.is_file()}

    assert files(again) == files(maren_plain)


def test_adding_calendar_and_voice_memos_leaves_the_rest_alone(maren_encrypted, tmp_path):
    pack = tmp_path / "packs"
    shutil.copytree(PERSONAS_DIR / "maren", pack / "maren")
    (pack / "maren" / "calendar.json").unlink()
    (pack / "maren" / "voice_memos.txt").unlink()
    without = _generate("maren", "ios26", tmp_path / "out", password="pw", pack=load_persona("maren", root=pack))
    with_both = maren_encrypted
    for key, value in without.expected.items():
        if key in ("calendar", "voice_memos", "absent"):
            continue
        assert with_both.expected[key] == value, key
    assert without.expected["calendar"] is None and without.expected["voice_memos"] is None
    assert without.expected["absent"] == {**with_both.expected["absent"], "calendar": "none", "voice_memos": "none"}
    before, after = _paths(without), _paths(with_both)
    assert before < after
    added = after - before
    assert CALENDAR in added and (SHARED, "Recordings/CloudRecordings.db") in added
    assert {d for d, _ in added} == {"HomeDomain", SHARED}
    for domain, path in sorted(before):
        a = Path(without.accessor.get_file(path, domain)).read_bytes()
        b = Path(with_both.accessor.get_file(path, domain)).read_bytes()
        assert a == b, f"{domain} {path}"


def test_einstein_still_has_neither(tmp_path):
    gen = _generate("einstein", "ios26", tmp_path)
    assert gen.expected["absent"]["calendar"] == "none" and gen.expected["absent"]["voice_memos"] == "none"
    assert gen.expected["calendar"] is None and gen.expected["voice_memos"] is None
    paths = {p for _, p in _paths(gen)}
    assert not any(p.endswith(("Calendar.sqlitedb", "CloudRecordings.db")) for p in paths)


def test_report_shows_both(tmp_path):
    text = render(load_persona("maren"))
    assert "42 calendar events, 12 voice memos |" in text
    assert "## Calendar\n" in text and "## Voice Memos\n" in text
    assert "| Climbing at Basalt |" in text and "skips 2025-08-12 19:00" in text
    assert "| DD presentation run-through |" in text


# ── the older layouts, with a copy of the test persona ───────────────────────

_CALENDAR_PACK = {
    "calendars": [{"id": "cal", "title": "Calendar", "color": "#1BADF8", "account": "icloud"},
                  {"id": "phone", "title": "On my phone", "color": "#63DA38", "account": "local"}],
    "events": [
        {"id": "standup", "calendar": "cal", "title": "Release standup", "start": "2021-06-01T09:30", "end": "2021-06-01T09:45",
         "created": "2021-05-28T16:00", "organizer": "me", "attendees": ["riley"], "statuses": {"riley": "accepted"},
         "alerts": [5], "repeat": {"frequency": "weekly", "until": "2021-07-27", "by_day": ["TU"]}, "skip": ["2021-06-15"]},
        {"id": "launch", "calendar": "phone", "title": "Launch day", "start": "2023-01-10", "created": "2022-01-05T10:00",
         "notes": "Starts after the 2022 backup, made before it."},
        {"id": "trip", "calendar": "phone", "title": "Lisbon", "start": "2024-01-15", "end": "2024-01-19",
         "created": "2023-12-01T19:00"},
        {"id": "late", "calendar": "cal", "title": "Made in 2025", "start": "2025-03-01T10:00", "end": "2025-03-01T11:00",
         "created": "2025-02-01T09:00", "time_zone": "Europe/Lisbon"},
    ],
}
_MEMOS = """== 2021-06-01
09:00 2m Release notes (favorite) (folder: Work)
== 2023-11-20
19:30 45s Song idea
  > La la la
== 2025-05-01
08:00 30s Deleted long ago (deleted)
"""


@pytest.fixture
def pack(tmp_path):
    folder = tmp_path / "packs" / "testpersona"
    shutil.copytree(PERSONAS_DIR / "testpersona", folder)
    raw = json.loads((folder / "persona.json").read_text(encoding="utf-8"))
    raw["snapshots"].append({"id": "ios13", "backup_date": "2021-12-01T20:00:00", "ios": "13.4.1", "build": "17E262",
                             "product_type": "iPhone11,8", "product_name": "iPhone XR"})
    raw["snapshots"].append({"id": "ios16", "backup_date": "2023-01-15T20:00:00", "ios": "16.1.2", "build": "20B110",
                             "product_type": "iPhone14,5", "product_name": "iPhone 13"})
    (folder / "persona.json").write_text(json.dumps(raw), encoding="utf-8")
    (folder / "calendar.json").write_text(json.dumps(_CALENDAR_PACK), encoding="utf-8")
    (folder / "voice_memos.txt").write_text(_MEMOS, encoding="utf-8")
    return folder.parent


@pytest.mark.parametrize("snapshot, calendar_schema, memo_schema, domain, folder, store_name, defaults", [
    ("ios13", "13.4.1", "13.4.1", "MediaDomain", "Media/Recordings", "Default", False),
    ("ios15", "15.3.1", "13.4.1", "MediaDomain", "Media/Recordings", "Default", False),
    ("ios16", "16.1.2", "13.4.1", "MediaDomain", "Media/Recordings", "Local", True),  # "Local" as on a 16.1.2 phone
    ("ios17", "17.3", "13.4.1", SHARED, "Recordings", "Local", True),
    ("ios26", "26.5", "26.5", SHARED, "Recordings", "Local", True),
])
def test_each_layout_reads_back(tmp_path, pack, snapshot, calendar_schema, memo_schema, domain, folder, store_name, defaults):
    persona = load_persona("testpersona", root=pack)
    gen = _generate("testpersona", snapshot, tmp_path / "out", password="pw" if snapshot == "ios17" else None, pack=persona)
    assert load_schema(persona.snapshots[snapshot].schema, "Calendar.sqlitedb")["ios"] == calendar_schema
    db = _open(gen, *CALENDAR)
    events = gen.expected["calendar"]["events"]
    assert read_events(db) == [{k: v for k, v in e.items() if k != "pack_id"} for e in events]
    assert db.execute("SELECT name FROM Store WHERE type = 0").fetchone()[0] == store_name
    assert bool(db.execute("SELECT COUNT(*) FROM Alarm WHERE default_alarm = 1").fetchone()[0]) == defaults
    backup = gen.expected["backup_date"]
    titles = {e["title"] for e in events}
    assert ("Made in 2025" in titles) == (snapshot == "ios26")  # made after the other backups
    if snapshot == "ios15":
        assert "Launch day" in titles and "Lisbon" not in titles  # starts after the backup, made before it
    assert all(e["created_date"] <= backup for e in events)
    standup = next(e for e in events if e["title"] == "Release standup")
    assert standup["recurrence"]["until"] == "2021-07-27T22:59:59+00:00"  # the end of that day in London (BST)
    assert standup["recurrence"]["last_start"] == "2021-07-27T08:30:00+00:00"
    assert standup["exceptions"] == ["2021-06-15T08:30:00+00:00"]

    paths = _paths(gen)
    assert (domain, f"{folder}/CloudRecordings.db") in paths
    assert gen.expected["voice_memos"] is not None
    memos, mdb = read_memos(gen, domain, folder)
    assert memos == [{k: v for k, v in m.items() if k != "transcript"} for m in gen.expected["voice_memos"]]
    columns = {r["name"] for r in mdb.execute("PRAGMA table_info(ZCLOUDRECORDING)")}
    assert ("ZSHAREDFLAGS" in columns) == (memo_schema == "26.5")
    notes = next(m for m in gen.expected["voice_memos"] if m["title"] == "Release notes")
    if memo_schema == "26.5":
        assert (notes["is_favorite"], notes["folder"]) == (True, "Work")
    else:
        assert (notes["is_favorite"], notes["folder"], notes["folder_id"]) == (None, None, None)
    song = [m for m in gen.expected["voice_memos"] if m["title"] == "Song idea"]
    if song:  # transcripts arrived with iOS 18
        assert song[0]["has_transcript"] == (snapshot == "ios26")
    assert all(m["title"] != "Deleted long ago" for m in gen.expected["voice_memos"]), "past 30 days in Recently Deleted"


def test_events_made_after_the_backup_are_left_out(tmp_path, pack):
    persona = load_persona("testpersona", root=pack)
    gen = _generate("testpersona", "ios17", tmp_path / "out", pack=persona)
    titles = {e["title"] for e in gen.expected["calendar"]["events"]}
    assert titles == {"Release standup", "Launch day", "Lisbon"}  # "Made in 2025" was made after this 2024 backup


def test_databases_with_nothing_in_them_yet_are_present(tmp_path):
    """A snapshot older than everything in the pack still has both databases, as a phone does."""
    from ios_backup_core.extractors import CalendarExtractor, VoiceMemoExtractor

    folder = tmp_path / "packs" / "testpersona"
    shutil.copytree(PERSONAS_DIR / "testpersona", folder)
    late = {**_CALENDAR_PACK, "events": [e for e in _CALENDAR_PACK["events"] if e["id"] == "late"]}
    (folder / "calendar.json").write_text(json.dumps(late), encoding="utf-8")
    (folder / "voice_memos.txt").write_text("== 2023-11-20\n19:30 45s Song idea\n", encoding="utf-8")
    persona = load_persona("testpersona", root=folder.parent)
    gen = _generate("testpersona", "ios15", tmp_path / "out", pack=persona)  # a 2022-03 backup
    expected = gen.expected
    assert "calendar" not in expected["absent"] and "voice_memos" not in expected["absent"]
    assert expected["calendar"]["events"] == [] and expected["voice_memos"] == []
    assert [c["title"] for c in expected["calendar"]["calendars"]] == ["Default", "On my phone", "Calendar", "Birthdays"]
    # The readers agree: the calendars are listed, with no events and no recordings in them.
    calendars = CalendarExtractor().list_calendars(gen.accessor)
    assert "empty_reason" not in calendars and "error" not in calendars
    assert [c["id"] for c in calendars["calendars"]] == [c["id"] for c in expected["calendar"]["calendars"]]
    assert CalendarExtractor().list_events(gen.accessor) == {"events": [], "empty_reason": "no_records", "hidden_count": 0}
    assert VoiceMemoExtractor().list_voice_memos(gen.accessor) == {"voice_memos": [], "empty_reason": "no_records"}


def test_long_series_end_on_their_real_last_occurrence(tmp_path, pack):
    """More than 5000 occurrences: the last start is still the series' own last one."""
    long = {**_CALENDAR_PACK, "events": [
        {"id": "pills", "calendar": "phone", "title": "Pills", "start": "2021-06-01", "created": "2021-05-01T09:00",
         "repeat": {"frequency": "daily", "count": 6000}},
        {"id": "bins", "calendar": "phone", "title": "Bins", "start": "2021-06-01", "created": "2021-05-01T09:00",
         "repeat": {"frequency": "weekly", "until": "2130-01-01"}},
    ]}
    (pack / "testpersona" / "calendar.json").write_text(json.dumps(long), encoding="utf-8")
    gen = _generate("testpersona", "ios26", tmp_path / "out", pack=load_persona("testpersona", root=pack))
    rules = {e["title"]: e["recurrence"] for e in gen.expected["calendar"]["events"]}
    assert rules["Pills"]["last_start"] == (date(2021, 6, 1) + timedelta(days=5999)).isoformat()
    assert rules["Bins"]["last_start"] == "2129-12-27"  # the last Tuesday before 1 January 2130
    db = _open(gen, *CALENDAR)
    cached = {r[0]: _instant(r[1]).date().isoformat() for r in db.execute(
        "SELECT i.summary, r.cached_end_date FROM Recurrence r JOIN CalendarItem i ON i.ROWID = r.owner_id")}
    assert cached == {"Pills": rules["Pills"]["last_start"], "Bins": rules["Bins"]["last_start"]}


def test_harvest_finds_both_voice_memo_locations(tmp_path, pack, monkeypatch):
    assert harvest.locations("CloudRecordings.db") == [(SHARED, "Recordings/CloudRecordings.db"),
                                                       ("MediaDomain", "Media/Recordings/CloudRecordings.db")]
    assert harvest.locations("sms.db") == [("HomeDomain", "Library/SMS/sms.db")]
    monkeypatch.setattr(harvest, "SCHEMAS_DIR", tmp_path / "schemas")
    persona = load_persona("testpersona", root=pack)
    for snapshot, version in (("ios13", "13.4.1"), ("ios26", "26.5")):
        gen = _generate("testpersona", snapshot, tmp_path / snapshot, pack=persona)
        written = harvest.harvest_backup(gen.backup_dir, None, f"9.{snapshot}", "X", "test",
                                         only={"CloudRecordings.db"})
        assert len(written) == 1, snapshot
        harvested = json.loads(written[0].read_text(encoding="utf-8"))
        assert sorted(o["sql"] for o in harvested["objects"]) == \
            sorted(o["sql"] for o in load_schema(version, "CloudRecordings.db")["objects"])


def test_harvested_voice_memos_schema_matches_the_others():
    new = load_schema("26.5", "CloudRecordings.db")
    old = load_schema("13.4.1", "CloudRecordings.db")
    calendar = load_schema("26.5", "Calendar.sqlitedb")
    assert set(new) == set(old) and set(new) - {"entities"} == set(calendar)
    assert new["pragmas"] == old["pragmas"] == {**calendar["pragmas"]}
    assert new["pragmas"]["journal_mode"] == "wal"
    assert "VoiceMemos14" in new["source"] and "23F77" in new["source"] and new["build"] == "23F77"
    assert {o["name"] for o in old["objects"]} < {o["name"] for o in new["objects"]}
    assert {e["Z_NAME"]: e["Z_ENT"] for e in new["entities"]} == {
        "CloudRecording": 1, "DatabaseProperty": 2, "EntityRevision": 3, "Folder": 4, "Migration": 5, "Recording": 6}
    columns = {c["name"] for c in new["columns"]["ZCLOUDRECORDING"]}
    assert {"ZENCRYPTEDTITLE", "ZCUSTOMLABELFORSORTING", "ZLOCALDURATION", "ZSHAREDFLAGS", "ZFOLDER"} <= columns
    assert {c["name"] for c in new["columns"]["ZFOLDER"]} >= {"ZUUID", "ZENCRYPTEDNAME", "ZRANK", "ZCOUNTOFRECORDINGS"}
    assert (Path(ROOT) / "backupgen" / "tools" / "coredata_store.swift").is_file()


# ── pack validation ──────────────────────────────────────────────────────────

def _event(**fields):
    return {"id": "e", "calendar": "cal", "title": "Thing", "start": "2022-03-01T10:00", "end": "2022-03-01T11:00",
            "created": "2022-02-01T09:00", **fields}


@pytest.mark.parametrize("calendar, message", [
    ({"calendars": [{"id": "c", "title": "C", "color": "blue", "account": "icloud"}]}, "color looks like"),
    ({"calendars": [{"id": "c", "title": "C", "color": "#123456", "account": "exchange"}]}, "account should be one of"),
    ({"calendars": [{"id": "a", "title": "A", "color": "#123456", "account": "holidays"},
                    {"id": "b", "title": "B", "color": "#123456", "account": "holidays"}]}, "one holidays calendar"),
    ({"events": [_event(calendar="nope")]}, "no calendar 'nope'"),
    ({"events": [_event(colour="red")]}, "unknown field 'colour'"),
    ({"events": [_event(end="2022-03-01")]}, "both be dates"),
    ({"events": [{k: v for k, v in _event().items() if k != "end"}]}, "a timed event needs an end"),
    ({"events": [_event(end="2022-03-01T09:00")]}, "ends before it starts"),
    ({"events": [_event(start="2022-03-01T10:00", end="2022-03-01T10:30", time_zone="America/New_York",
                        end_time_zone="Europe/London")]}, "ends before it starts"),
    ({"events": [_event(time_zone="Mars/Olympus")]}, "unknown time zone"),
    ({"events": [_event(start="2022-03-01", end="2022-03-01", floating=True)]}, "all-day events float already"),
    ({"events": [_event(organizer="me", attendees=["jordan"])]}, "jordan has no email"),
    ({"events": [_event(attendees=["riley"])]}, "needs an organizer"),
    ({"events": [_event(organizer="riley", attendees=["riley", "me"])]}, "added as the chair"),
    ({"events": [_event(organizer="riley", attendees=[])]}, "'me' should be among the attendees"),
    ({"events": [_event(organizer="me", attendees=["riley"], statuses={"riley": "maybe"})]}, "answer should be one of"),
    ({"events": [_event(organizer="me", attendees=["riley"], statuses={"casey": "accepted"})]}, "isn't an attendee"),
    ({"events": [_event(calendar="phone", organizer="me", attendees=["riley"])]}, "icloud calendar"),
    ({"events": [_event(skip=["2022-03-08"])]}, "skip and moved are for repeating events"),
    ({"events": [_event(repeat={"frequency": "weekly"}, skip=["2022-03-09"])]}, "isn't an occurrence"),
    ({"events": [_event(repeat={"frequency": "weekly", "by_day": ["WE"]})]}, "isn't one of the series' own days"),
    ({"events": [_event(repeat={"frequency": "weekly", "count": 3, "until": "2022-04-01"})]}, "not both"),
    ({"events": [_event(repeat={"frequency": "weekly", "until": "2022-02-28"})]}, r"ends \(until 2022-02-28\) before"),
    ({"events": [_event(repeat={"frequency": "weekly", "until": "2022-03-01T09:00"})]}, "before the event starts"),
    ({"events": [_event(start="2022-03-01", end="2022-03-01", repeat={"frequency": "weekly", "until": "2022-02-01"})]},
     "before the event starts"),
    ({"events": [_event(repeat={"frequency": "weekly", "by_day": "TU"})]}, r"by_day is a list, e\.g\. \[\"TU\"\]"),
    ({"events": [_event(repeat={"frequency": "yearly", "by_month": 3})]}, "by_month is a list"),
    ({"events": [_event(repeat={"frequency": "weekly"}, skip="2022-03-08")]}, "skip is a list of occurrences"),
    ({"events": [_event(repeat={"frequency": "weekly"}, moved={"from": "2022-03-08"})]}, "moved is a list"),
    ({"events": [_event(repeat={"frequency": "weekly", "by_day": ["1TU"]})]}, "needs a monthly or yearly"),
    ({"events": [_event(repeat={"frequency": "yearly", "by_day": ["1TU"]})]}, "needs by_month"),
    ({"events": [_event(repeat={"frequency": "hourly"})]}, "frequency should be one of"),
    ({"events": [_event(repeat={"frequency": "weekly"},
                        moved=[{"from": "2022-03-08", "start": "2022-03-09", "end": "2022-03-09"}])]}, "keeps the series' kind"),
    ({"events": [_event(created="2030-01-01T09:00")]}, "outside the persona's period"),
    ({"events": [_event(alerts=["15"])]}, "alerts are minutes"),
    ({"events": [_event(lat=91, lon=0)]}, "isn't a place on Earth"),
    ({"events": [_event(), _event()]}, "duplicate event ids"),
])
def test_bad_calendar_files_are_rejected(tmp_path, pack, calendar, message):
    raw = {"calendars": [{"id": "cal", "title": "Cal", "color": "#1BADF8", "account": "icloud"},
                         {"id": "phone", "title": "Phone", "color": "#1BADF8", "account": "local"}], "events": [], **calendar}
    (pack / "testpersona" / "calendar.json").write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(PersonaError, match=message):
        load_persona("testpersona", root=pack)


@pytest.mark.parametrize("shift", [-2, 116])
def test_repeating_events_need_an_unshifted_persona(pack, shift):
    """A whole-year shift moves weekdays: the 2021 Tuesdays of a weekly series aren't Tuesdays two years earlier."""
    persona_json = pack / "testpersona" / "persona.json"
    raw = json.loads(persona_json.read_text(encoding="utf-8"))
    persona_json.write_text(json.dumps({**raw, "shift_years": shift}), encoding="utf-8")
    with pytest.raises(PersonaError, match=r"event 1 \(standup\): a repeating event needs shift_years 0"):
        load_persona("testpersona", root=pack)
    single = {**_CALENDAR_PACK, "events": [e for e in _CALENDAR_PACK["events"] if "repeat" not in e]}
    (pack / "testpersona" / "calendar.json").write_text(json.dumps(single), encoding="utf-8")
    assert len(load_persona("testpersona", root=pack).events) == 3
    persona_json.write_text(json.dumps({**raw, "shift_years": 400}), encoding="utf-8")  # the Gregorian cycle
    (pack / "testpersona" / "calendar.json").write_text(json.dumps(_CALENDAR_PACK), encoding="utf-8")
    assert load_persona("testpersona", root=pack).events[0].repeat is not None


@pytest.mark.parametrize("text, message", [
    ("09:00 1m Before any date\n", "needs a == date heading"),
    ("== 2022-01-01\n09:00 forever Title\n", "can't parse|can't read duration"),
    ("== 2022-01-01\n09:00 0s Title\n", "at least a second"),
    ("== 2022-01-01\n10:00 1m Later\n09:00 1m Earlier\n", "time order"),
    ("== 2022-01-01\n  > orphan transcript\n", "belongs under a recording"),
    ("== 2022-01-01\n09:00 1m Title (favorite) (favorite)\n", r"\(favorite\) twice"),
    ("== 2022-01-01\n09:00 1m Title (folder: )\n", "needs the folder's name"),
    ("== 2022-01-01\n09:00 1m (deleted)\n", "needs a title"),
    ("== 2030-01-01\n09:00 1m Title\n", "outside the persona's period"),
    ("== 2022-01-01\n09:00 1m Call (icloud)\n  > hello there\n", "no audio in the backup"),
])
def test_bad_voice_memo_files_are_rejected(tmp_path, pack, text, message):
    (pack / "testpersona" / "voice_memos.txt").write_text(text, encoding="utf-8")
    with pytest.raises(PersonaError, match=message):
        load_persona("testpersona", root=pack)


def test_transcript_spacing_is_what_the_audio_file_holds(tmp_path, pack):
    (pack / "testpersona" / "voice_memos.txt").write_text(
        "== 2025-05-01\n08:00 30s Spaced\n  > Okay,  from\tthe top.  \n  >   And   again.\n", encoding="utf-8")
    persona = load_persona("testpersona", root=pack)
    assert persona.voice_memos[0].transcript == "Okay, from the top. And again."
    gen = _generate("testpersona", "ios26", tmp_path / "out", pack=persona)
    [memo] = gen.expected["voice_memos"]
    data = Path(gen.accessor.get_file(f"Recordings/{memo['file_name']}", SHARED)).read_bytes()
    runs = json.loads(boxes(data)["moov/trak/udta/tsrp"])["attributedString"]["runs"]
    assert memo["transcript"] == "".join(runs[0::2]) == "Okay, from the top. And again."


def test_voice_memo_lines_parse():
    persona = load_persona("maren")
    memos = {m.title: m for m in persona.voice_memos}
    assert memos["Cottonwood site walk"].favorite and memos["Cottonwood site walk"].folder == "Site visits"
    assert memos["Permit comments call"].icloud and memos["Permit comments call"].duration == 1440
    assert memos["New Recording 14"].deleted
    assert memos["DD presentation run-through"].transcript.startswith("Okay, from the top.")
    assert memos["Linden kids' workshop"].duration == 6720
