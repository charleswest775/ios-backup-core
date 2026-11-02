"""Tests for attachment classification in MessageExtractor.get_messages."""

import sqlite3
import sys
import os
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ios_backup_core.extractors.messages import MessageExtractor
from ios_backup_core.text import _type_from_bundle_id


class _FakeBackup:
    def __init__(self, sms_path: str):
        self._sms_path = sms_path

    def get_file(self, relative_path: str, domain: str = None):
        if relative_path == "Library/SMS/sms.db":
            return self._sms_path
        return None


def _typedstream_nsstring(message: bytes) -> bytes:
    """Minimal pytypedstream-compatible TypedStream NSString blob."""
    return (
        b"\x04\x0bstreamtyped\x81\xe8\x03\x84\x01@\x84\x84\x84\x08NSString"
        b"\x01\x84\x84\x08NSObject\x00\x85\x84\x01+"
        + bytes([len(message)])
        + message
        + b"\x86"
    )


def _build_sms_db(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE handle (
            ROWID INTEGER PRIMARY KEY,
            id TEXT,
            uncanonicalized_id TEXT
        );
        CREATE TABLE chat_handle_join (
            chat_id INTEGER,
            handle_id INTEGER
        );
        CREATE TABLE message (
            ROWID INTEGER PRIMARY KEY,
            text TEXT,
            date INTEGER,
            is_from_me INTEGER,
            handle_id INTEGER,
            cache_has_attachments INTEGER,
            associated_message_type INTEGER,
            attributedBody BLOB,
            payload_data BLOB,
            balloon_bundle_id TEXT,
            is_audio_message INTEGER,
            item_type INTEGER,
            share_status INTEGER,
            share_direction INTEGER
        );
        CREATE TABLE chat_message_join (
            chat_id INTEGER,
            message_id INTEGER
        );
        CREATE TABLE attachment (
            ROWID INTEGER PRIMARY KEY,
            filename TEXT,
            mime_type TEXT,
            transfer_name TEXT,
            total_bytes INTEGER
        );
        CREATE TABLE message_attachment_join (
            message_id INTEGER,
            attachment_id INTEGER
        );

        INSERT INTO handle (ROWID, id) VALUES (1, '+15551234567');
        INSERT INTO chat_handle_join (chat_id, handle_id) VALUES (1, 1);

        -- Real image attachment (flag=1, real join)
        INSERT INTO message (
            ROWID, text, date, is_from_me, handle_id,
            cache_has_attachments, associated_message_type, item_type
        ) VALUES (10, NULL, 1000, 0, 1, 1, 0, 0);
        INSERT INTO chat_message_join (chat_id, message_id) VALUES (1, 10);
        INSERT INTO attachment (ROWID, filename, mime_type, transfer_name, total_bytes)
        VALUES (100, '~/Library/SMS/Attachments/img.jpg', 'image/jpeg', 'IMG_1.jpg', 1234);
        INSERT INTO message_attachment_join (message_id, attachment_id) VALUES (10, 100);

        -- Plugin-only URL balloon (flag=1, only .pluginPayloadAttachment)
        INSERT INTO message (
            ROWID, text, date, is_from_me, handle_id,
            cache_has_attachments, associated_message_type, item_type,
            balloon_bundle_id, payload_data
        ) VALUES (
            20, NULL, 2000, 0, 1, 1, 0, 0,
            'com.apple.messages.URLBalloonProvider', NULL
        );
        INSERT INTO chat_message_join (chat_id, message_id) VALUES (1, 20);
        INSERT INTO attachment (ROWID, filename, mime_type, transfer_name, total_bytes)
        VALUES (200, 'foo.pluginPayloadAttachment', NULL, 'bar.pluginPayloadAttachment', 10);
        INSERT INTO message_attachment_join (message_id, attachment_id) VALUES (20, 200);

        -- Stale flag, no joins (flag=1, empty text) → system, not attachment
        INSERT INTO message (
            ROWID, text, date, is_from_me, handle_id,
            cache_has_attachments, associated_message_type, item_type
        ) VALUES (30, NULL, 3000, 0, 1, 1, 0, 0);
        INSERT INTO chat_message_join (chat_id, message_id) VALUES (1, 30);

        -- Empty text, flag=0, no joins → system (UFFC-cleaned case)
        INSERT INTO message (
            ROWID, text, date, is_from_me, handle_id,
            cache_has_attachments, associated_message_type, item_type
        ) VALUES (40, '', 4000, 0, 1, 0, 0, 0);
        INSERT INTO chat_message_join (chat_id, message_id) VALUES (1, 40);
        """
    )
    # Link balloon with URL only in attributedBody (no SQL text, no payload_data).
    url = b"https://example.com/article"
    conn.execute(
        """
        INSERT INTO message (
            ROWID, text, date, is_from_me, handle_id,
            cache_has_attachments, associated_message_type, item_type,
            balloon_bundle_id, payload_data, attributedBody
        ) VALUES (?, NULL, 5000, 0, 1, 0, 0, 0,
                  'com.apple.messages.URLBalloonProvider', NULL, ?)
        """,
        (50, _typedstream_nsstring(url)),
    )
    conn.execute(
        "INSERT INTO chat_message_join (chat_id, message_id) VALUES (1, 50)"
    )
    conn.commit()
    conn.close()


class TestTypeFromBundleId:
    def test_url_balloon_is_link(self):
        assert _type_from_bundle_id("com.apple.messages.URLBalloonProvider") == "link"


class TestAttachmentClassification:
    def setup_method(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.sms_path = os.path.join(self._tmpdir.name, "sms.db")
        _build_sms_db(self.sms_path)
        self.extractor = MessageExtractor()
        self.backup = _FakeBackup(self.sms_path)

    def teardown_method(self):
        self.extractor._connections.clear()
        self._tmpdir.cleanup()

    def _by_id(self):
        result = self.extractor.get_messages(self.backup, contacts={}, chat_id=1, limit=50)
        assert "error" not in result
        return {m["message_id"]: m for m in result["messages"]}

    def test_real_image_attachment(self):
        msg = self._by_id()[10]
        assert msg["message_type"] == "attachment"
        assert msg["has_attachments"] is True
        assert len(msg["attachments"]) == 1
        assert msg["attachments"][0]["transfer_name"] == "IMG_1.jpg"

    def test_plugin_only_url_balloon_is_link_not_attachment(self):
        msg = self._by_id()[20]
        assert msg["message_type"] == "link"
        assert msg["has_attachments"] is False
        assert msg["attachments"] == []

    def test_link_balloon_reads_url_from_attributed_body(self):
        """URL balloons must not skip attributedBody just because type is link."""
        msg = self._by_id()[50]
        assert msg["message_type"] == "link"
        assert msg["text"] == "https://example.com/article"
        assert msg["link_preview"] is None  # no payload_data

    def test_stale_flag_without_joins_is_system(self):
        msg = self._by_id()[30]
        assert msg["message_type"] == "system"
        assert msg["has_attachments"] is False
        assert msg["attachments"] == []

    def test_empty_text_flag_zero_is_system(self):
        msg = self._by_id()[40]
        assert msg["message_type"] == "system"
        assert msg["has_attachments"] is False
