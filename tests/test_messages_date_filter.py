"""Tests for date_from/date_to paging in MessageExtractor.get_messages."""

import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ios_backup_core.extractors.messages import MessageExtractor
from ios_backup_core.timestamps import iso_to_apple


CHAT = 1
OTHER_CHAT = 2


class _FakeBackup:
    def __init__(self, sms_path: str):
        self._sms_path = sms_path

    def get_file(self, relative_path: str, domain: str = None):
        if relative_path == "Library/SMS/sms.db":
            return self._sms_path
        return None


def _day(n: int) -> str:
    return f"2023-01-{n:02d}T12:00:00+00:00"


def _build_sms_db(path: str) -> None:
    """Chat 1 has one message a day, 1-20 January, with ROWID == day.

    Chat 2 has a message on each of those days too, so a filter that loses the
    chat condition shows up. Its ROWIDs (101-120) are all newer than chat 1's.
    """
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE handle (
            ROWID INTEGER PRIMARY KEY,
            id TEXT,
            uncanonicalized_id TEXT
        );
        CREATE TABLE chat_handle_join (chat_id INTEGER, handle_id INTEGER);
        CREATE TABLE message (
            ROWID INTEGER PRIMARY KEY,
            text TEXT,
            date INTEGER,
            is_from_me INTEGER,
            handle_id INTEGER,
            cache_has_attachments INTEGER,
            associated_message_type INTEGER
        );
        CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
        INSERT INTO handle (ROWID, id) VALUES (1, '+15551234567');
        INSERT INTO chat_handle_join (chat_id, handle_id) VALUES (1, 1);
        """
    )
    for chat_id, base in ((CHAT, 0), (OTHER_CHAT, 100)):
        for n in range(1, 21):
            conn.execute(
                "INSERT INTO message VALUES (?, ?, ?, 0, 1, 0, 0)",
                (base + n, f"chat {chat_id} day {n}", int(iso_to_apple(_day(n), nanoseconds=True))),
            )
            conn.execute("INSERT INTO chat_message_join VALUES (?, ?)", (chat_id, base + n))
    conn.commit()
    conn.close()


class TestGetMessagesDateFilter:
    def setup_method(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        sms_path = os.path.join(self._tmpdir.name, "sms.db")
        _build_sms_db(sms_path)
        self.extractor = MessageExtractor()
        self.backup = _FakeBackup(sms_path)

    def teardown_method(self):
        self.extractor._connections.clear()
        self._tmpdir.cleanup()

    def _page(self, offset=0, limit=5, chat_id=CHAT, **dates):
        return self.extractor.get_messages(
            self.backup, chat_id, {}, offset=offset, limit=limit, **dates
        )

    def _ids(self, result):
        return [m["message_id"] for m in result["messages"]]

    def _walk(self, limit=5, **dates):
        """Every page in the order a caller loads them, following next_offset."""
        pages = []
        offset = 0
        while True:
            result = self._page(offset=offset, limit=limit, **dates)
            if not result["messages"]:
                return pages
            pages.append(self._ids(result))
            offset = result["next_offset"]

    def test_range_older_than_newest_page_returns_its_messages(self):
        """The range holds days 3-6; the newest 5 messages of the chat are days 16-20."""
        result = self._page(date_from=_day(3), date_to=_day(6))
        assert result["total"] == 4
        assert self._ids(result) == [3, 4, 5, 6]
        assert result["next_offset"] == 4

    def test_page_is_the_newest_in_range_not_the_newest_in_chat(self):
        result = self._page(limit=2, date_from=_day(3), date_to=_day(10))
        assert result["total"] == 8
        assert self._ids(result) == [9, 10]

    def test_offset_counts_within_the_range(self):
        result = self._page(offset=2, limit=2, date_from=_day(3), date_to=_day(10))
        assert self._ids(result) == [7, 8]
        assert result["next_offset"] == 4

    def test_offset_past_the_range_is_empty(self):
        result = self._page(offset=8, limit=2, date_from=_day(3), date_to=_day(10))
        assert result["total"] == 8
        assert self._ids(result) == []

    def test_paging_a_range_matches_paging_the_chat_with_the_same_order(self):
        """Newest page first, oldest-first inside a page, nothing lost or repeated."""
        assert self._walk(limit=3) == [
            [18, 19, 20], [15, 16, 17], [12, 13, 14], [9, 10, 11], [6, 7, 8], [3, 4, 5], [1, 2],
        ]
        assert self._walk(limit=3, date_from=_day(3), date_to=_day(10)) == [
            [8, 9, 10], [5, 6, 7], [3, 4],
        ]

    def test_date_from_alone(self):
        result = self._page(limit=3, date_from=_day(10))
        assert result["total"] == 11
        assert self._ids(result) == [18, 19, 20]

    def test_date_to_alone(self):
        """Only date_to set: the old days are the whole range, and none are in the newest page."""
        result = self._page(limit=3, date_to=_day(5))
        assert result["total"] == 5
        assert self._ids(result) == [3, 4, 5]

    def test_range_stays_inside_the_chat(self):
        result = self._page(chat_id=OTHER_CHAT, date_from=_day(3), date_to=_day(6))
        assert result["total"] == 4
        assert self._ids(result) == [103, 104, 105, 106]

    def test_range_with_no_messages_is_empty(self):
        result = self._page(date_from="2024-01-01", date_to="2024-02-01")
        assert result["total"] == 0
        assert self._ids(result) == []
        assert result["next_offset"] == 0

    def test_unfiltered_page_is_unchanged(self):
        result = self._page(limit=3)
        assert result["total"] == 20
        assert self._ids(result) == [18, 19, 20]
