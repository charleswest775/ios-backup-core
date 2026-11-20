"""Calendar and Voice Memos read back through the library, against backupgen's answer keys.

backupgen writes Maren's calendar and recordings from her persona pack, and its answer key
says what a correct reader lists. These tests read her backup (unencrypted and encrypted)
through BackupReader and compare every calendar, event and recording with the answer key,
field by field. They also play every recording's audio file, read every transcript and write
every calendar as an .ics file. A copy of the test persona with a small calendar.json and
voice_memos.txt covers the older layouts: iOS 13.4.1 (Voice Memos in MediaDomain, no folders
or favourites), 15.3.1, 17.3 and 26.5.

All of it is made up. Photo downloads are replaced by small generated images, so nothing
needs the network.
"""

import hashlib
import io
import json
import os
import re
import shutil
import sys
from types import SimpleNamespace

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, ROOT)

from backupgen import PERSONAS_DIR, media  # noqa: E402
from backupgen.generate import EXPECTED, generate  # noqa: E402
from backupgen.persona import load_persona  # noqa: E402
from ios_backup_core import mp4  # noqa: E402
from ios_backup_core.backup import BackupReader, open_local_backup  # noqa: E402
from ios_backup_core.extractors import CalendarExtractor, VoiceMemoExtractor  # noqa: E402
from ios_backup_core.extractors.calendar_events import events_to_ics  # noqa: E402

# Keys only the answer key has: the event's calendar title and its id in the pack.
EVENT_KEY_ONLY = {"calendar", "pack_id"}
# Keys only the answer key has: the transcript text and the audio file's size.
MEMO_KEY_ONLY = {"transcript", "bytes"}


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
    return SimpleNamespace(expected=expected, accessor=accessor, reader=BackupReader(accessor))


@pytest.fixture(scope="module")
def maren_plain(tmp_path_factory):
    return _generate("maren", "ios26", tmp_path_factory.mktemp("maren-plain"))


@pytest.fixture(scope="module")
def maren_encrypted(tmp_path_factory):
    return _generate("maren", "ios26", tmp_path_factory.mktemp("maren-enc"), password="pw")


@pytest.fixture(params=["plain", "encrypted"])
def maren(request):
    return request.getfixturevalue(f"maren_{request.param}")


# ── comparing with the answer key ────────────────────────────────────────────


def _differences(label: str, got: dict, want: dict, key_only=frozenset()) -> list:
    """Every field where *got* differs from the answer key's *want*, as readable lines."""
    wanted = {k: v for k, v in want.items() if k not in key_only}
    out = [f"{label}: the reader has {k!r}, the answer key doesn't" for k in sorted(set(got) - set(wanted))]
    out += [f"{label}: the answer key has {k!r}, the reader doesn't" for k in sorted(set(wanted) - set(got))]
    out += [f"{label}: {k} is {got[k]!r}, the answer key says {v!r}"
            for k, v in wanted.items() if k in got and got[k] != v]
    return out


def _succeeded(result: dict, key: str) -> list:
    assert "error" not in result, result.get("error")
    assert "empty_reason" not in result, result
    return result[key]


def check_calendars(gen) -> list:
    want = gen.expected["calendar"]["calendars"]
    got = _succeeded(gen.reader.calendars(), "calendars")
    assert [c["id"] for c in got] == [c["id"] for c in want], "same calendars, in the same order"
    problems = []
    for g, w in zip(got, want, strict=True):
        problems += _differences(f"calendar {w['title']!r}", g, w)
    assert problems == []
    return got


def check_events(gen, calendars) -> list:
    want = gen.expected["calendar"]["events"]
    result = gen.reader.calendar_events()
    got = _succeeded(result, "events")
    assert result["hidden_count"] == 0
    by_id = {e["id"]: e for e in got}
    assert len(by_id) == len(got), "each event listed once"
    assert sorted(by_id) == sorted(e["id"] for e in want)
    titles = {c["id"]: c["title"] for c in calendars}
    problems = []
    for w in want:
        g = by_id[w["id"]]
        problems += _differences(f"event {w['pack_id']!r}", g, w, EVENT_KEY_ONLY)
        if titles.get(g["calendar_id"]) != w["calendar"]:
            problems.append(f"event {w['pack_id']!r}: in {titles.get(g['calendar_id'])!r}, not {w['calendar']!r}")
    assert problems == []
    return got


def check_calendar_filters(gen, calendars) -> None:
    """Each calendar's own list holds exactly its events, as many as event_count says."""
    want = gen.expected["calendar"]["events"]
    for calendar in calendars:
        events = gen.reader.calendar_events(calendar_id=calendar["id"])["events"]
        mine = sorted(e["id"] for e in want if e["calendar_id"] == calendar["id"])
        assert sorted(e["id"] for e in events) == mine, calendar["title"]
        assert len(events) == calendar["event_count"], calendar["title"]


def check_voice_memos(gen) -> list:
    want = gen.expected["voice_memos"]
    result = gen.reader.voice_memos()
    got = _succeeded(result, "voice_memos")
    assert [m["id"] for m in got] == [m["id"] for m in want], "same recordings, newest first"
    problems = []
    for g, w in zip(got, want, strict=True):
        problems += _differences(f"recording {w['title']!r}", g, w, MEMO_KEY_ONLY)
    assert problems == []

    # The folders, each counting the recordings in it that aren't deleted.
    named = {}
    for m in want:
        if m["folder_id"]:
            named.setdefault(m["folder_id"], (m["folder"], []))[1].append(m)
    folders = result["folders"]
    assert {f["id"]: f["name"] for f in folders} == {i: name for i, (name, _) in named.items()}
    assert {f["id"]: f["count"] for f in folders} == {
        i: sum(not m["is_deleted"] for m in memos) for i, (_, memos) in named.items()
    }
    return got


def check_audio(gen) -> int:
    """Every recording in the backup plays: its file is an MP4 holding this recording.
    Recordings that aren't in the backup give no path. Returns how many played."""
    extractor = VoiceMemoExtractor()
    played = 0
    for memo in gen.expected["voice_memos"]:
        path = extractor.get_audio_path(gen.accessor, memo["id"])
        if not memo["audio_available"]:
            assert path is None, memo["title"]
            continue
        assert path and os.path.isfile(path), memo["title"]
        assert os.path.getsize(path) == memo["bytes"], memo["title"]
        with open(path, "rb") as f:
            assert f.read(8)[4:] == b"ftyp", memo["title"]
        assert mp4.audio_codec(path) == "mp4a", memo["title"]
        assert mp4.read_tags(path).get("unique_id") == memo["unique_id"], memo["title"]
        assert extractor.get_audio_path(gen.accessor, str(memo["id"])) == path, "the id as text finds it too"
        played += 1
    return played


def check_transcripts(gen) -> int:
    """Every transcript reads as the answer key has it; recordings without one give None."""
    extractor = VoiceMemoExtractor()
    read = 0
    for memo in gen.expected["voice_memos"]:
        transcript = extractor.get_transcript(gen.accessor, memo["id"])
        if not memo["has_transcript"]:
            assert transcript is None, memo["title"]
            continue
        assert transcript is not None, memo["title"]
        assert transcript["text"] == memo["transcript"], memo["title"]
        assert transcript["segments"], memo["title"]
        assert " ".join(s["text"] for s in transcript["segments"]).split() == memo["transcript"].split()
        for segment in transcript["segments"]:
            assert 0 <= segment["start"] <= segment["end"] <= memo["duration_seconds"] + 0.5, segment
        read += 1
    return read


def _unfold(ics: str) -> list:
    return re.sub(r"\r\n[ \t]", "", ics).split("\r\n")[:-1]


def _blocks(lines: list, name: str) -> list:
    """The content lines of each BEGIN:name ... END:name block."""
    out, current = [], None
    for line in lines:
        if line == f"BEGIN:{name}":
            current = []
        elif line == f"END:{name}":
            out.append(current)
            current = None
        elif current is not None:
            current.append(line)
    return out


def _unescape(value: str) -> str:
    return re.sub(r"\\([\\;,nN])", lambda m: "\n" if m.group(1) in "nN" else m.group(1), value)


def check_ics(ics: str, events: list, calendars: list) -> None:
    """The file's structure is sound and it holds every event, its rule, skips and moves."""
    assert ics.startswith("BEGIN:VCALENDAR\r\n") and ics.endswith("END:VCALENDAR\r\n")
    assert "\n" not in ics.replace("\r\n", ""), "CRLF line endings only"
    assert all(len(line.encode("utf-8")) <= 75 for line in ics.split("\r\n")), "folded at 75 octets"
    lines = _unfold(ics)
    stack = []
    for line in lines:
        if line.startswith("BEGIN:"):
            stack.append(line[6:])
        elif line.startswith("END:"):
            assert stack and stack.pop() == line[4:], line
    assert stack == [], "every BEGIN has its END"
    assert "VERSION:2.0" in lines and any(line.startswith("PRODID:") for line in lines)
    if len(calendars) == 1:
        assert f"X-WR-CALNAME:{calendars[0]['title']}" in [_unescape(line) for line in lines]

    vevents = _blocks(lines, "VEVENT")
    dated = [e for e in events if e["start"] or e["wall_start"] or e["start_day"]]
    assert len(vevents) == len(dated)

    def value(block, name):
        return next((line.split(":", 1)[1] for line in block if re.match(rf"{name}[;:]", line)), None)

    assert sorted(_unescape(value(v, "SUMMARY")) for v in vevents) == sorted(e["title"] for e in dated)
    present = {e["id"] for e in dated}
    moved = [e for e in dated if e["detached_from"] and e["detached_from"]["event_id"] in present]
    assert sum(value(v, "RRULE") is not None for v in vevents) == sum(e["recurrence"] is not None for e in dated)
    assert sum(value(v, "EXDATE") is not None for v in vevents) == sum(
        bool(e["exceptions"]) and e["recurrence"] is not None for e in dated)
    assert sum(value(v, "RECURRENCE-ID") is not None for v in vevents) == len(moved)
    uids = [value(v, "UID") for v in vevents]
    assert all(uids)
    assert len(set(uids)) == len(uids) - len(moved), "a moved occurrence shares its series' UID"
    assert sum(len(_blocks(v, "VALARM")) for v in vevents) == sum(len(e["alarms"]) for e in dated)

    zones_used = {m.group(1) for line in lines for m in [re.search(r";TZID=([^;:]+)", line)] if m}
    zones_defined = {line[5:] for block in _blocks(lines, "VTIMEZONE") for line in block if line.startswith("TZID:")}
    assert zones_used <= zones_defined, "every TZID has a VTIMEZONE"


def check_every_calendar_as_ics(gen, calendars) -> None:
    for calendar in calendars:
        events = gen.reader.calendar_events(calendar_id=calendar["id"])["events"]
        check_ics(events_to_ics(events, [calendar]), events, [calendar])
    everything = gen.reader.calendar_events()["events"]
    check_ics(events_to_ics(everything, calendars), everything, calendars)


# ── Maren ────────────────────────────────────────────────────────────────────


def test_maren_calendars_match_the_answer_key(maren):
    calendars = check_calendars(maren)
    assert {c["kind"] for c in calendars} == {"normal", "birthdays", "holidays", "suggestions"}


def test_maren_events_match_the_answer_key(maren):
    calendars = check_calendars(maren)
    events = check_events(maren, calendars)
    assert len(events) == 43
    # The showcase cases are all there: a series with a skipped and a moved night, other zones,
    # a floating event, multi-day trips, meetings and alerts.
    assert any(e["recurrence"] and e["exceptions"] for e in events)
    assert any(e["detached_from"] for e in events)
    assert any(e["end_time_zone"] for e in events)
    assert any(e["floating"] and not e["all_day"] for e in events)
    assert any(e["all_day"] and e["days"] > 1 for e in events)
    assert any(e["attendees"] for e in events) and any(e["alarms"] for e in events)


def test_maren_each_calendar_lists_its_own_events(maren):
    check_calendar_filters(maren, check_calendars(maren))


def test_maren_voice_memos_match_the_answer_key(maren):
    memos = check_voice_memos(maren)
    assert len(memos) == 12
    assert any(m["is_deleted"] for m in memos)
    assert any(m["audio_missing_reason"] == "in_icloud_only" for m in memos)


def test_maren_every_recording_plays(maren):
    assert check_audio(maren) == sum(m["audio_available"] for m in maren.expected["voice_memos"]) == 11


def test_maren_every_transcript_reads(maren):
    assert check_transcripts(maren) == 1


def test_maren_every_calendar_writes_as_ics(maren):
    check_every_calendar_as_ics(maren, check_calendars(maren))


def test_maren_lists_match_the_extractors_used_directly(maren_plain):
    """BackupReader adds nothing of its own: the same lists as the extractors give."""
    accessor = maren_plain.accessor
    assert maren_plain.reader.calendars() == CalendarExtractor().list_calendars(accessor)
    assert maren_plain.reader.calendar_events() == CalendarExtractor().list_events(
        accessor, contacts=maren_plain.reader.contacts())
    assert maren_plain.reader.voice_memos() == VoiceMemoExtractor().list_voice_memos(accessor)


def test_einstein_has_neither(tmp_path):
    gen = _generate("einstein", "ios26", tmp_path / "einstein")
    assert gen.expected["calendar"] is None and gen.expected["voice_memos"] is None
    assert gen.reader.calendars() == {"calendars": [], "empty_reason": "not_in_backup"}
    assert gen.reader.calendar_events() == {"events": [], "empty_reason": "not_in_backup"}
    assert gen.reader.voice_memos() == {"voice_memos": [], "empty_reason": "not_in_backup"}


# ── the older layouts, with a copy of the test persona ───────────────────────

_CALENDAR_PACK = {
    "calendars": [
        {"id": "personal", "title": "Personal", "color": "#1BADF8", "account": "icloud"},
        {"id": "phone", "title": "On my phone", "color": "#63DA38", "account": "local"},
        {"id": "holidays", "title": "Holidays", "color": "#FF9500", "account": "holidays"},
    ],
    "events": [
        {"id": "new-year", "calendar": "holidays", "title": "New Year's Day", "start": "2022-01-01",
         "created": "2021-02-01T08:00", "repeat": {"frequency": "yearly", "count": 5}},
        {"id": "rent", "calendar": "phone", "title": "Pay rent", "start": "2021-02-01", "created": "2021-01-20T10:00",
         "repeat": {"frequency": "monthly", "until": "2021-12-31"}, "alerts": [-540]},
        {"id": "birthday", "calendar": "personal", "title": "Riley's birthday", "start": "2021-11-03",
         "created": "2021-03-01T10:00", "repeat": {"frequency": "yearly"}},
        {"id": "swim", "calendar": "personal", "title": "Swimming", "start": "2021-05-04T07:00",
         "end": "2021-05-04T08:00", "created": "2021-04-30T12:00", "location": "Lido", "address": "1 Pool Lane",
         "lat": 51.5, "lon": -0.12, "alerts": [10],
         "repeat": {"frequency": "weekly", "by_day": ["TU"], "count": 12}, "skip": ["2021-05-18T07:00"],
         "moved": [{"from": "2021-06-01T07:00", "start": "2021-06-02T07:30", "end": "2021-06-02T08:30"}]},
        {"id": "stretch", "calendar": "phone", "title": "Morning stretch", "start": "2021-06-05T06:30",
         "end": "2021-06-05T06:45", "floating": True, "created": "2021-06-04T22:00",
         "repeat": {"frequency": "daily", "interval": 2, "count": 5}},
        {"id": "book-club", "calendar": "personal", "title": "Book club", "start": "2021-07-08T19:00",
         "end": "2021-07-08T20:30", "created": "2021-06-20T09:00", "organizer": "me", "attendees": ["riley"],
         "statuses": {"riley": "tentative"}, "alerts": [60, 5], "notes": "Bring the second book."},
        {"id": "picnic", "calendar": "personal", "title": "Riley's picnic", "start": "2021-08-14T12:00",
         "end": "2021-08-14T15:00", "created": "2021-08-01T18:00", "organizer": "riley", "attendees": ["me"],
         "statuses": {"me": "accepted"}},
        {"id": "lakes", "calendar": "phone", "title": "Lake trip", "start": "2021-09-20", "end": "2021-09-24",
         "created": "2021-07-01T20:00", "availability": "free"},
        {"id": "flight", "calendar": "personal", "title": "Flight to New York", "start": "2022-02-10T10:00",
         "end": "2022-02-10T13:30", "end_time_zone": "America/New_York", "created": "2022-01-15T10:00"},
        {"id": "dentist", "calendar": "personal", "title": "Dentist", "start": "2023-03-14T15:00",
         "end": "2023-03-14T15:30", "created": "2023-02-01T10:00", "status": "cancelled"},
        {"id": "concert", "calendar": "phone", "title": "Concert", "start": "2027-05-01T20:00",
         "end": "2027-05-01T22:30", "created": "2025-11-01T10:00", "time_zone": "Europe/Paris"},
    ],
}
_MEMOS = """== 2021-03-10
08:15 1m30s Shopping list
== 2021-06-01
09:00 2m Release notes (favorite) (folder: Work)
== 2021-09-21
18:40 45s Waterfall (folder: Trips)
== 2021-11-20
12:00 30s Kept in iCloud (icloud)
== 2021-11-25
20:00 1m Old idea (deleted)
== 2023-11-20
19:30 50s Song idea
  > A song about the sea, in two parts.
  > The second part is slower.
"""


@pytest.fixture(scope="module")
def pack(tmp_path_factory):
    folder = tmp_path_factory.mktemp("packs") / "testpersona"
    shutil.copytree(PERSONAS_DIR / "testpersona", folder)
    raw = json.loads((folder / "persona.json").read_text(encoding="utf-8"))
    raw["snapshots"].append({"id": "ios13", "backup_date": "2021-12-01T20:00:00", "ios": "13.4.1", "build": "17E262",
                             "product_type": "iPhone11,8", "product_name": "iPhone XR"})
    (folder / "persona.json").write_text(json.dumps(raw), encoding="utf-8")
    (folder / "calendar.json").write_text(json.dumps(_CALENDAR_PACK), encoding="utf-8")
    (folder / "voice_memos.txt").write_text(_MEMOS, encoding="utf-8")
    return load_persona("testpersona", root=folder.parent)


@pytest.mark.parametrize("snapshot, password, source_domain", [
    ("ios13", None, "MediaDomain"),
    ("ios15", None, "MediaDomain"),
    ("ios17", "pw", "AppDomainGroup-group.com.apple.VoiceMemos.shared"),
    ("ios26", None, "AppDomainGroup-group.com.apple.VoiceMemos.shared"),
])
def test_each_layout_reads_back_as_the_answer_key(tmp_path, pack, snapshot, password, source_domain):
    gen = _generate("testpersona", snapshot, tmp_path / snapshot, password=password, pack=pack)
    calendars = check_calendars(gen)
    events = check_events(gen, calendars)
    check_calendar_filters(gen, calendars)
    check_every_calendar_as_ics(gen, calendars)
    memos = check_voice_memos(gen)
    assert check_audio(gen) == sum(m["audio_available"] for m in memos)
    transcripts = check_transcripts(gen)

    titles = {e["title"] for e in events}
    assert ("Flight to New York" in titles) == (snapshot != "ios13")  # made after the iOS 13 backup
    assert ("Concert" in titles) == (snapshot == "ios26")  # made after the others
    databases = gen.accessor.list_files(path_like="%CloudRecordings.db")
    assert [d["domain"] for d in databases] == [source_domain]
    by_title = {m["title"]: m for m in memos}
    if snapshot == "ios13":
        # The iOS 13 layout has no favourites or folders; a recording deleted days ago is still listed.
        assert (by_title["Release notes"]["is_favorite"], by_title["Release notes"]["folder"]) == (None, None)
        assert by_title["Old idea"]["is_deleted"]
        assert by_title["Kept in iCloud"]["audio_missing_reason"] == "in_icloud_only"
    if snapshot == "ios26":
        assert (by_title["Release notes"]["is_favorite"], by_title["Release notes"]["folder"]) == (True, "Work")
        assert transcripts == 1  # transcripts arrived with iOS 18
    else:
        assert transcripts == 0
