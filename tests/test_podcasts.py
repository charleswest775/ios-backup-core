"""Tests for PodcastExtractor and podcasts_to_opml.

The fixtures copy the column sets of real MTLibrary.sqlite files: iOS 13
(49 columns in ZMTPODCAST, no implicit follows) and iOS 16+ (implicit
follows, bookmarks). Only the columns the extractor reads are created, plus
one unrelated column, since the extractor must not care about the rest.
"""

import os
import re
import sqlite3
import sys
import xml.etree.ElementTree as ET

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ios_backup_core.extractors.podcasts import (
    PODCASTS_DB_PATH,
    PODCASTS_DOMAIN,
    PodcastExtractor,
    podcasts_to_opml,
)

# 2020-03-25T19:17:58+00:00 and later, in Apple epoch seconds.
ADDED = 606856678.0
PLAYED = 608063886.5


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


IOS13_PODCAST = (
    "Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, ZSUBSCRIBED INTEGER, ZHIDDEN INTEGER,"
    " ZSORTORDER INTEGER, ZSTORECOLLECTIONID INTEGER, ZADDEDDATE TIMESTAMP,"
    " ZLASTDATEPLAYED TIMESTAMP, ZAUTHOR VARCHAR, ZCATEGORY VARCHAR, ZFEEDURL VARCHAR,"
    " ZIMAGEURL VARCHAR, ZITEMDESCRIPTION VARCHAR, ZPROVIDER VARCHAR,"
    " ZSHOWTYPEINFEED VARCHAR, ZSTORECLEANURL VARCHAR, ZTITLE VARCHAR,"
    " ZUPDATEDFEEDURL VARCHAR, ZUUID VARCHAR, ZWEBPAGEURL VARCHAR"
)
IOS16_PODCAST = IOS13_PODCAST + (
    ", ZISIMPLICITLYFOLLOWED INTEGER, ZISHIDDENORIMPLICITLYFOLLOWED INTEGER,"
    " ZLASTUNFOLLOWEDDATE TIMESTAMP"
)
IOS13_EPISODE = (
    "Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, ZPODCAST INTEGER, ZPLAYCOUNT INTEGER,"
    " ZPLAYSTATE INTEGER, ZSAVED INTEGER, ZVIDEO INTEGER, ZFEEDDELETED INTEGER,"
    " ZUSERDELETED INTEGER, ZSEASONNUMBER INTEGER, ZEPISODENUMBER INTEGER,"
    " ZDURATION FLOAT, ZLASTDATEPLAYED TIMESTAMP, ZPLAYHEAD FLOAT, ZPUBDATE TIMESTAMP,"
    " ZASSETURL VARCHAR, ZAUTHOR VARCHAR, ZENCLOSUREURL VARCHAR, ZEPISODETYPE VARCHAR,"
    " ZGUID VARCHAR, ZITEMDESCRIPTIONWITHOUTHTML VARCHAR, ZPODCASTUUID VARCHAR,"
    " ZTITLE VARCHAR, ZUUID VARCHAR, ZWEBPAGEURL VARCHAR"
)
IOS16_EPISODE = IOS13_EPISODE + ", ZISBOOKMARKED INTEGER, ZISHIDDEN INTEGER"


def _library(path, podcast_cols=IOS16_PODCAST, episode_cols=IOS16_EPISODE,
             podcasts=(), episodes=()):
    conn = sqlite3.connect(path)
    conn.execute(f"CREATE TABLE ZMTPODCAST ({podcast_cols})")
    conn.execute(f"CREATE TABLE ZMTEPISODE ({episode_cols})")
    for row in podcasts:
        names = ", ".join(row)
        conn.execute(f"INSERT INTO ZMTPODCAST ({names}) VALUES ({', '.join('?' * len(row))})",
                     list(row.values()))
    for row in episodes:
        names = ", ".join(row)
        conn.execute(f"INSERT INTO ZMTEPISODE ({names}) VALUES ({', '.join('?' * len(row))})",
                     list(row.values()))
    conn.commit()
    conn.close()
    return str(path)


def _backup(db_path, domain=PODCASTS_DOMAIN, path=PODCASTS_DB_PATH):
    return _FakeBackup(files={(domain, path): db_path})


SHOW_A = {
    "Z_PK": 1, "ZTITLE": "Tech & <Talk>", "ZAUTHOR": "Ada", "ZSUBSCRIBED": 1,
    "ZISIMPLICITLYFOLLOWED": 0, "ZHIDDEN": 0, "ZSORTORDER": 1,
    "ZFEEDURL": "http://feeds.example.com/tech?a=1&b=2",
    "ZUPDATEDFEEDURL": "https://feeds.example.com/tech?a=1&b=2",
    "ZWEBPAGEURL": "https://example.com/tech", "ZADDEDDATE": ADDED,
    "ZLASTDATEPLAYED": PLAYED, "ZSTORECOLLECTIONID": 12345, "ZUUID": "uuid-a",
}
SHOW_IMPLICIT = {
    "Z_PK": 2, "ZTITLE": "Played Once", "ZSUBSCRIBED": 1, "ZISIMPLICITLYFOLLOWED": 1,
    "ZSORTORDER": 2, "ZFEEDURL": "https://example.com/once.xml",
}
SHOW_UNFOLLOWED_NO_FEED = {
    "Z_PK": 3, "ZTITLE": "Apple Original", "ZSUBSCRIBED": 0, "ZSORTORDER": 3,
    "ZFEEDURL": None, "ZPROVIDER": "Apple",
}

EPISODES = [
    # Listened: play count and last played date.
    {"Z_PK": 10, "ZPODCAST": 1, "ZTITLE": "Ep 3", "ZPUBDATE": PLAYED - 100,
     "ZPLAYCOUNT": 2, "ZLASTDATEPLAYED": PLAYED, "ZPLAYSTATE": 0, "ZDURATION": 1800.0,
     "ZENCLOSUREURL": "https://cdn.example.com/3.mp3", "ZEPISODENUMBER": 3,
     "ZASSETURL": "file:///private/var/mobile/Media/Podcasts/x.mp3"},
    # In progress: playhead only.
    {"Z_PK": 11, "ZPODCAST": 1, "ZTITLE": "Ep 2", "ZPUBDATE": PLAYED - 200,
     "ZPLAYCOUNT": 0, "ZPLAYHEAD": 312.5, "ZPLAYSTATE": 1, "ZISBOOKMARKED": 1},
    # Back catalogue the app marked "played" when the show was followed.
    {"Z_PK": 12, "ZPODCAST": 1, "ZTITLE": "Ep 1", "ZPUBDATE": PLAYED - 300,
     "ZPLAYCOUNT": 0, "ZPLAYHEAD": 0.0, "ZPLAYSTATE": 0},
    {"Z_PK": 13, "ZPODCAST": 2, "ZTITLE": "Only one", "ZPUBDATE": PLAYED - 50,
     "ZPLAYSTATE": 2, "ZASSETURL": "ipod-library://item/item.mp3?id=1"},
]


@pytest.fixture
def library(tmp_path):
    return _library(tmp_path / "MTLibrary.sqlite",
                    podcasts=[SHOW_A, SHOW_IMPLICIT, SHOW_UNFOLLOWED_NO_FEED],
                    episodes=EPISODES)


# ---------------------------------------------------------------------------
# Shows
# ---------------------------------------------------------------------------

def test_lists_shows_with_follow_state_and_counts(library):
    result = PodcastExtractor().list_podcasts(_backup(library))
    assert "error" not in result and "empty_reason" not in result
    shows = {p["id"]: p for p in result["podcasts"]}
    assert list(shows) == [1, 2, 3]  # ZSORTORDER

    a = shows[1]
    assert a["title"] == "Tech & <Talk>"
    assert a["author"] == "Ada"
    assert a["followed"] is True and a["implicitly_followed"] is False
    # The updated (https) address wins; the old one is kept for reference.
    assert a["feed_url"] == "https://feeds.example.com/tech?a=1&b=2"
    assert a["original_feed_url"] == "http://feeds.example.com/tech?a=1&b=2"
    assert a["added_date"] == "2020-03-25T19:17:58+00:00"
    assert a["last_played_date"].startswith("2020-04-08T")
    assert a["store_id"] == 12345
    assert a["episode_count"] == 3
    assert a["listened_count"] == 2  # not the back-catalogue episode

    implicit = shows[2]
    assert implicit["subscribed"] is True
    assert implicit["implicitly_followed"] is True
    assert implicit["followed"] is False
    assert implicit["original_feed_url"] is None

    apple = shows[3]
    assert apple["followed"] is False
    assert apple["feed_url"] is None
    assert apple["author"] == "Apple"  # ZPROVIDER when there is no ZAUTHOR
    assert apple["episode_count"] == 0


def test_ios13_schema_without_implicit_follow_columns(tmp_path):
    db = _library(tmp_path / "MTLibrary.sqlite", podcast_cols=IOS13_PODCAST,
                  episode_cols=IOS13_EPISODE,
                  podcasts=[{k: v for k, v in SHOW_A.items() if k != "ZISIMPLICITLYFOLLOWED"}],
                  episodes=[{k: v for k, v in e.items() if k != "ZISBOOKMARKED"}
                            for e in EPISODES if e["ZPODCAST"] == 1])
    shows = PodcastExtractor().list_podcasts(_backup(db))["podcasts"]
    assert shows[0]["followed"] is True
    assert shows[0]["unfollowed_date"] is None
    episodes = PodcastExtractor().list_episodes(_backup(db))["episodes"]
    assert len(episodes) == 3
    assert all(e["hidden"] is False for e in episodes)


def test_minimal_schema_reads_missing_columns_as_none(tmp_path):
    db = _library(tmp_path / "MTLibrary.sqlite",
                  podcast_cols="Z_PK INTEGER PRIMARY KEY, ZTITLE VARCHAR, ZFEEDURL VARCHAR",
                  episode_cols="Z_PK INTEGER PRIMARY KEY, ZTITLE VARCHAR",
                  podcasts=[{"Z_PK": 1, "ZTITLE": "Bare", "ZFEEDURL": "https://x/feed"}],
                  episodes=[{"Z_PK": 1, "ZTITLE": "Bare ep"}])
    show = PodcastExtractor().list_podcasts(_backup(db))["podcasts"][0]
    assert show["title"] == "Bare" and show["feed_url"] == "https://x/feed"
    assert show["followed"] is False and show["author"] is None
    assert show["episode_count"] == 0  # no ZPODCAST column to count by
    ep = PodcastExtractor().list_episodes(_backup(db))["episodes"][0]
    assert ep["title"] == "Bare ep" and ep["listened"] is False and ep["play_state"] is None
    assert PodcastExtractor().list_episodes(_backup(db), podcast_id=1) == {
        "episodes": [], "empty_reason": "no_records"}


def test_text_stored_as_bytes_is_decoded(tmp_path):
    db = _library(tmp_path / "MTLibrary.sqlite", podcasts=[{
        "Z_PK": 1, "ZTITLE": "Café Radio".encode(), "ZSUBSCRIBED": 1,
        "ZFEEDURL": b"https://example.com/cafe.xml", "ZAUTHOR": b"\xff broken",
    }], episodes=[{"Z_PK": 1, "ZPODCAST": 1, "ZTITLE": "Épisode".encode()}])
    show = PodcastExtractor().list_podcasts(_backup(db))["podcasts"][0]
    assert show["title"] == "Café Radio"
    assert show["feed_url"] == "https://example.com/cafe.xml"
    assert show["author"] == "� broken"
    ep = PodcastExtractor().list_episodes(_backup(db))["episodes"][0]
    assert ep["title"] == "Épisode" and ep["podcast_title"] == "Café Radio"


def test_invalid_utf8_text_does_not_fail_the_library(library):
    conn = sqlite3.connect(library)
    # A 4-byte sequence cut in half, and a UTF-8-encoded lone surrogate.
    conn.execute("UPDATE ZMTPODCAST SET ZITEMDESCRIPTION = CAST(X'4869F09F' AS TEXT) WHERE Z_PK = 1")
    conn.execute("UPDATE ZMTPODCAST SET ZTITLE = CAST(X'4170EDA0BD' AS TEXT) WHERE Z_PK = 2")
    conn.execute("UPDATE ZMTEPISODE SET ZTITLE = CAST(X'45702033EDA0BD' AS TEXT) WHERE Z_PK = 10")
    conn.commit()
    conn.close()
    shows = PodcastExtractor().list_podcasts(_backup(library))
    assert "error" not in shows and len(shows["podcasts"]) == 3
    assert shows["podcasts"][0]["description"].startswith("Hi")
    episodes = PodcastExtractor().list_episodes(_backup(library))
    assert "error" not in episodes and len(episodes["episodes"]) == 4
    assert {e["id"]: e for e in episodes["episodes"]}[10]["title"].startswith("Ep 3")


# ---------------------------------------------------------------------------
# Episodes
# ---------------------------------------------------------------------------

def test_episodes_newest_first_with_listening_history(library):
    episodes = PodcastExtractor().list_episodes(_backup(library))["episodes"]
    assert [e["id"] for e in episodes] == [13, 10, 11, 12]
    by_id = {e["id"]: e for e in episodes}

    played = by_id[10]
    assert played["listened"] is True and played["play_count"] == 2
    assert played["play_state"] == "played"
    assert played["last_played_date"].startswith("2020-04-08T")
    assert played["duration_seconds"] == 1800.0
    assert played["downloaded_on_phone"] is True
    assert played["enclosure_url"] == "https://cdn.example.com/3.mp3"
    assert played["podcast_title"] == "Tech & <Talk>"
    assert played["episode_number"] == 3

    in_progress = by_id[11]
    assert in_progress["listened"] is True and in_progress["play_state"] == "in_progress"
    assert in_progress["playhead_seconds"] == 312.5
    assert in_progress["saved"] is True

    back_catalogue = by_id[12]
    assert back_catalogue["play_state"] == "played"
    assert back_catalogue["listened"] is False

    synced = by_id[13]
    assert synced["downloaded_on_phone"] is True  # ipod-library:// counts
    assert synced["play_state"] == "unplayed"
    assert "description" not in synced


def test_episode_filters(library):
    ex = PodcastExtractor()
    assert [e["id"] for e in ex.list_episodes(_backup(library), listened_only=True)["episodes"]] == [10, 11]
    assert [e["id"] for e in ex.list_episodes(_backup(library), podcast_id=2)["episodes"]] == [13]
    assert ex.list_episodes(_backup(library), podcast_id=3) == {
        "episodes": [], "empty_reason": "no_records"}
    assert ex.list_episodes(_backup(library), podcast_id=2, listened_only=True) == {
        "episodes": [], "empty_reason": "no_records"}


def test_descriptions_only_when_asked(tmp_path):
    db = _library(tmp_path / "MTLibrary.sqlite", podcasts=[SHOW_A], episodes=[
        {"Z_PK": 1, "ZPODCAST": 1, "ZTITLE": "x", "ZITEMDESCRIPTIONWITHOUTHTML": "About x"}])
    ep = PodcastExtractor().list_episodes(_backup(db), include_descriptions=True)["episodes"][0]
    assert ep["description"] == "About x"


# ---------------------------------------------------------------------------
# Finding the library, and empty states
# ---------------------------------------------------------------------------

def test_found_through_the_manifest_in_another_container(library):
    backup = _backup(library, domain="AppDomainGroup-OTHER.podcasts", path="Library/MTLibrary.sqlite")
    assert len(PodcastExtractor().list_podcasts(backup)["podcasts"]) == 3


def test_prefers_the_podcasts_container(tmp_path, library):
    decoy = _library(tmp_path / "decoy.sqlite", podcasts=[{"Z_PK": 9, "ZTITLE": "Decoy"}])
    backup = _FakeBackup(files={
        ("AppDomain-com.example.other", "Documents/MTLibrary.sqlite"): decoy,
        (PODCASTS_DOMAIN, PODCASTS_DB_PATH): library,
    })
    assert [p["id"] for p in PodcastExtractor().list_podcasts(backup)["podcasts"]] == [1, 2, 3]


def test_not_in_backup():
    ex = PodcastExtractor()
    assert ex.list_podcasts(_FakeBackup()) == {"podcasts": [], "empty_reason": "not_in_backup"}
    assert ex.list_episodes(_FakeBackup()) == {"episodes": [], "empty_reason": "not_in_backup"}


def test_empty_library(tmp_path):
    db = _library(tmp_path / "MTLibrary.sqlite")
    ex = PodcastExtractor()
    assert ex.list_podcasts(_backup(db)) == {"podcasts": [], "empty_reason": "no_records"}
    assert ex.list_episodes(_backup(db)) == {"episodes": [], "empty_reason": "no_records"}


def test_library_without_podcast_tables(tmp_path):
    path = tmp_path / "MTLibrary.sqlite"
    sqlite3.connect(path).execute("CREATE TABLE Z_METADATA (Z_VERSION INTEGER)").connection.close()
    ex = PodcastExtractor()
    assert ex.list_podcasts(_backup(str(path))) == {"podcasts": [], "empty_reason": "no_records"}
    assert ex.list_episodes(_backup(str(path))) == {"episodes": [], "empty_reason": "no_records"}


def test_listed_but_unreadable_is_an_error():
    backup = _FakeBackup(unreadable={(PODCASTS_DOMAIN, PODCASTS_DB_PATH)})
    result = PodcastExtractor().list_podcasts(backup)
    assert result["podcasts"] == [] and "empty_reason" not in result
    assert result["error"].startswith("Podcasts are in this backup but could not be read.")


def test_corrupt_file_is_an_error(tmp_path):
    path = tmp_path / "MTLibrary.sqlite"
    path.write_bytes(b"not a database" * 100)
    for result in (PodcastExtractor().list_podcasts(_backup(str(path))),
                   PodcastExtractor().list_episodes(_backup(str(path)))):
        assert "empty_reason" not in result
        assert result["error"].startswith("Podcasts are in this backup but could not be read.")


# ---------------------------------------------------------------------------
# OPML
# ---------------------------------------------------------------------------

def test_opml_keeps_followed_shows_with_a_feed(library):
    shows = PodcastExtractor().list_podcasts(_backup(library))["podcasts"]
    opml = podcasts_to_opml(shows, title="Podcasts from Ada's iPhone",
                            date_created="Thu, 12 Nov 2026 10:00:00 GMT")
    assert opml.startswith('<?xml version="1.0" encoding="UTF-8"?>\n<opml version="2.0">')
    root = ET.fromstring(opml.encode("utf-8"))
    assert root.find("head/title").text == "Podcasts from Ada's iPhone"
    assert root.find("head/dateCreated").text == "Thu, 12 Nov 2026 10:00:00 GMT"
    outlines = root.findall("body/outline")
    assert len(outlines) == 1  # not the implicit follow, not the feedless show
    o = outlines[0].attrib
    assert o == {
        "type": "rss",
        "text": "Tech & <Talk>",
        "title": "Tech & <Talk>",
        "xmlUrl": "https://feeds.example.com/tech?a=1&b=2",
        "htmlUrl": "https://example.com/tech",
    }
    assert "&amp;" in opml and "&lt;Talk&gt;" in opml


def test_opml_all_shows_and_odd_characters():
    shows = [
        {"title": "Quote \" and \x01control", "feed_url": "https://a/x?q=\"1\"", "followed": False},
        {"title": None, "feed_url": "https://b/feed", "followed": True},
        {"title": "No feed", "feed_url": None, "followed": True},
    ]
    root = ET.fromstring(podcasts_to_opml(shows, followed_only=False).encode("utf-8"))
    outlines = [o.attrib for o in root.findall("body/outline")]
    assert [o["text"] for o in outlines] == ['Quote " and control', "https://b/feed"]
    assert outlines[0]["xmlUrl"] == 'https://a/x?q="1"'
    assert "htmlUrl" not in outlines[1]


def test_opml_with_no_shows_is_still_valid():
    root = ET.fromstring(podcasts_to_opml([]).encode("utf-8"))
    assert root.find("head/title").text == "Podcasts"
    assert root.findall("body/outline") == []
