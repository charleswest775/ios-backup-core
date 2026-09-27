"""Tests for VoiceMemoExtractor against a fake unencrypted backup."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from backup_builder import BackupBuilder
from ios_backup_core.extractors.voice_memos import VoiceMemoExtractor

APP_GROUP = "AppDomainGroup-group.com.apple.VoiceMemos.shared"
INDEX_PATH = "Recordings/CloudRecordings.db"

# 2024-01-01T12:00:00Z as seconds since 2001-01-01.
T0 = 725803200.0


def _cloud_schema(conn):
    conn.executescript("""
        CREATE TABLE ZFOLDER (Z_PK INTEGER PRIMARY KEY, ZENCRYPTEDNAME TEXT);
        CREATE TABLE ZCLOUDRECORDING (
            Z_PK INTEGER PRIMARY KEY, ZDATE TIMESTAMP, ZDURATION FLOAT, ZPATH VARCHAR,
            ZENCRYPTEDTITLE VARCHAR, ZCUSTOMLABELFORSORTING VARCHAR, ZCUSTOMLABEL VARCHAR,
            ZEVICTIONDATE TIMESTAMP, ZFOLDER INTEGER
        );
    """)


def _insert(conn, pk, date, duration, path, title=None, label=None, evicted=None, folder=None):
    conn.execute(
        "INSERT INTO ZCLOUDRECORDING VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (pk, date, duration, path, title, label, label, evicted, folder),
    )


class VoiceMemoTests(unittest.TestCase):
    def setUp(self):
        self.builder = BackupBuilder()
        self.extractor = VoiceMemoExtractor()

    def tearDown(self):
        self.builder.cleanup()

    def _add_standard_index(self):
        def build(conn):
            _cloud_schema(conn)
            conn.execute("INSERT INTO ZFOLDER VALUES (1, 'Work')")
            _insert(conn, 1, T0, 12.5, "20240101 120000-AAAA.m4a", title="Standup", folder=1)
            _insert(conn, 2, T0 + 3600, 60, "20240101 130000-BBBB.qta", label="Home")
            _insert(conn, 3, T0 + 7200, 5, "20240101 140000-CCCC.m4a",
                    title="Deleted idea", evicted=T0 + 8000)
        self.builder.add_db(APP_GROUP, INDEX_PATH, build)
        self.builder.add_bytes(APP_GROUP, "Recordings/20240101 120000-AAAA.m4a", b"M4A-AUDIO")
        self.builder.add_bytes(APP_GROUP, "Recordings/20240101 130000-BBBB.qta", b"QTA-AUDIO")

    def test_lists_memos_newest_first(self):
        self._add_standard_index()
        result = self.extractor.list_voice_memos(self.builder.accessor())
        self.assertEqual(result["errors"], [])
        memos = result["voice_memos"]
        self.assertEqual([m["id"] for m in memos], [3, 2, 1])

        deleted, home, standup = memos
        self.assertEqual(standup["title"], "Standup")
        self.assertEqual(standup["folder"], "Work")
        self.assertEqual(standup["duration"], 12.5)
        self.assertTrue(standup["date"].startswith("2024-01-01T12:00:00"))
        self.assertTrue(standup["has_audio"])
        self.assertFalse(standup["deleted"])

        self.assertEqual(home["title"], "Home")  # falls back to the custom label
        self.assertIsNone(home["folder"])

        self.assertTrue(deleted["deleted"])
        self.assertTrue(deleted["deleted_date"].startswith("2024-01-01T14:13:20"))
        self.assertFalse(deleted["has_audio"])

    def test_get_audio_file(self):
        self._add_standard_index()
        audio = self.extractor.get_audio_file(self.builder.accessor(), 2)
        self.assertEqual(audio["file_name"], "20240101 130000-BBBB.qta")
        self.assertEqual(audio["mime_type"], "audio/mp4")
        with open(audio["path"], "rb") as f:
            self.assertEqual(f.read(), b"QTA-AUDIO")

    def test_get_audio_file_missing(self):
        self._add_standard_index()
        acc = self.builder.accessor()
        self.assertIsNone(self.extractor.get_audio_file(acc, 3))   # no audio in backup
        self.assertIsNone(self.extractor.get_audio_file(acc, 99))  # no such memo

    def test_legacy_recordings_db_with_device_paths(self):
        def build(conn):
            conn.execute("CREATE TABLE ZRECORDING (Z_PK INTEGER PRIMARY KEY, ZDATE TIMESTAMP, "
                         "ZDURATION FLOAT, ZPATH VARCHAR, ZCUSTOMLABEL VARCHAR)")
            conn.execute("INSERT INTO ZRECORDING VALUES (1, ?, 30, "
                         "'/var/mobile/Media/Recordings/20150101 120000.m4a', 'Old memo')", (T0,))
        self.builder.add_db("MediaDomain", "Media/Recordings/Recordings.db", build)
        self.builder.add_bytes("MediaDomain", "Media/Recordings/20150101 120000.m4a", b"OLD")
        memos = self.extractor.list_voice_memos(self.builder.accessor())["voice_memos"]
        self.assertEqual(len(memos), 1)
        self.assertEqual(memos[0]["title"], "Old memo")
        self.assertEqual(memos[0]["file_name"], "20150101 120000.m4a")
        self.assertTrue(memos[0]["has_audio"])
        self.assertFalse(memos[0]["deleted"])

    def test_prefers_app_group_index_over_stale_media_domain_copy(self):
        def build(title):
            def run(conn):
                _cloud_schema(conn)
                _insert(conn, 1, T0, 1, "a.m4a", title=title)
            return run
        self.builder.add_db("MediaDomain", "Media/Recordings/CloudRecordings.db", build("Stale"))
        self.builder.add_db(APP_GROUP, INDEX_PATH, build("Current"))
        memos = self.extractor.list_voice_memos(self.builder.accessor())["voice_memos"]
        self.assertEqual([m["title"] for m in memos], ["Current"])

    def test_reads_uncheckpointed_wal(self):
        def build(conn):
            _cloud_schema(conn)
            _insert(conn, 1, T0, 1, "old.m4a", title="Old")

        def wal(conn):
            _insert(conn, 2, T0 + 60, 1, "new.m4a", title="Recorded just before backup")

        self.builder.add_db(APP_GROUP, INDEX_PATH, build, wal)
        titles = [m["title"] for m in
                  self.extractor.list_voice_memos(self.builder.accessor())["voice_memos"]]
        self.assertEqual(titles, ["Recorded just before backup", "Old"])

    def test_no_voice_memos(self):
        result = self.extractor.list_voice_memos(self.builder.accessor())
        self.assertEqual(result, {"voice_memos": [], "errors": []})

    def test_unrecognized_index_is_reported(self):
        self.builder.add_db(APP_GROUP, INDEX_PATH, lambda c: c.execute("CREATE TABLE other (x)"))
        result = self.extractor.list_voice_memos(self.builder.accessor())
        self.assertEqual(result["voice_memos"], [])
        self.assertEqual(len(result["errors"]), 1)


if __name__ == "__main__":
    unittest.main()
