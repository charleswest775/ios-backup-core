"""Tests for BrowserHistoryExtractor and the accessor helpers it relies on.

Each test builds a small *unencrypted* backup on disk — a Manifest.db plus
files stored under their SHA-1 names — so LocalBackupAccessor is exercised
the same way it is against a real backup.
"""

import hashlib
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ios_backup_core.backup import LocalBackupAccessor
from ios_backup_core.extractors.browser_history import (
    UNENCRYPTED_SAFARI_NOTICE,
    BrowserHistoryExtractor,
)

# 2024-01-01T12:00:00Z in each browser's epoch.
UNIX_TS = 1704110400
APPLE_TS = float(UNIX_TS - 978307200)              # seconds since 2001-01-01
WEBKIT_TS = (UNIX_TS + 11644473600) * 1_000_000    # microseconds since 1601-01-01

CHROME_DOMAIN = "AppDomain-com.google.chrome.ios"
CHROME_PATH = "Library/Application Support/Google/Chrome/Default/History"
EDGE_DOMAIN = "AppDomain-com.microsoft.msedge"


def _safari_schema(conn):
    conn.executescript("""
        CREATE TABLE history_items (
            id INTEGER PRIMARY KEY, url TEXT, domain_expansion TEXT, visit_count INTEGER
        );
        CREATE TABLE history_visits (
            id INTEGER PRIMARY KEY, history_item INTEGER, visit_time REAL, title TEXT
        );
    """)


def _chromium_schema(conn):
    conn.executescript("""
        CREATE TABLE urls (
            id INTEGER PRIMARY KEY, url TEXT, title TEXT, visit_count INTEGER,
            hidden INTEGER DEFAULT 0
        );
        CREATE TABLE visits (
            id INTEGER PRIMARY KEY, url INTEGER, visit_time INTEGER, transition INTEGER
        );
    """)


class _BackupBuilder:
    """Writes files into a fake unencrypted backup directory."""

    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="bh_test_")
        self._scratch = tempfile.mkdtemp(prefix="bh_scratch_")
        self._manifest = sqlite3.connect(os.path.join(self.dir, "Manifest.db"))
        self._manifest.execute(
            "CREATE TABLE Files (fileID TEXT PRIMARY KEY, domain TEXT, "
            "relativePath TEXT, flags INTEGER, file BLOB)"
        )

    def add_bytes(self, domain: str, relative_path: str, data: bytes) -> None:
        file_id = hashlib.sha1(f"{domain}-{relative_path}".encode()).hexdigest()
        os.makedirs(os.path.join(self.dir, file_id[:2]), exist_ok=True)
        with open(os.path.join(self.dir, file_id[:2], file_id), "wb") as f:
            f.write(data)
        self._manifest.execute(
            "INSERT INTO Files VALUES (?, ?, ?, 1, NULL)", (file_id, domain, relative_path)
        )
        self._manifest.commit()

    def add_db(self, domain: str, relative_path: str, build, wal_build=None) -> None:
        """Create a SQLite DB with build(conn).

        If wal_build is given, it runs in WAL mode after build and its writes
        are left un-checkpointed, so they exist only in the -wal file — the
        state iOS often leaves a database in when a backup is taken.
        """
        path = os.path.join(self._scratch, hashlib.sha1(relative_path.encode()).hexdigest())
        conn = sqlite3.connect(path)
        build(conn)
        conn.commit()
        if wal_build is None:
            conn.close()
            with open(path, "rb") as f:
                self.add_bytes(domain, relative_path, f.read())
            return
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_autocheckpoint=0")
        wal_build(conn)
        conn.commit()
        # Snapshot while the connection is open, before any checkpoint.
        for suffix in ("", "-wal"):
            with open(path + suffix, "rb") as f:
                self.add_bytes(domain, relative_path + suffix, f.read())
        conn.close()

    def accessor(self, encrypted: bool = False) -> LocalBackupAccessor:
        self._manifest.close()
        return LocalBackupAccessor(self.dir, "test-udid", {}, encrypted=encrypted)

    def cleanup(self):
        shutil.rmtree(self.dir, ignore_errors=True)
        shutil.rmtree(self._scratch, ignore_errors=True)


def _add_safari(builder, rows, domain="HomeDomain", path="Library/Safari/History.db",
                wal_rows=None):
    def insert(rows_):
        def run(conn):
            for i, (url, title) in enumerate(rows_, start=1 + conn.execute(
                    "SELECT COUNT(*) FROM history_items").fetchone()[0]):
                conn.execute("INSERT INTO history_items VALUES (?, ?, NULL, 1)", (i, url))
                conn.execute("INSERT INTO history_visits VALUES (?, ?, ?, ?)",
                             (i, i, APPLE_TS + i * 600, title))
        return run

    def build(conn):
        _safari_schema(conn)
        insert(rows)(conn)

    builder.add_db(domain, path, build, insert(wal_rows) if wal_rows else None)


def _add_chromium(builder, domain, rows, path=CHROME_PATH):
    """rows: (url, title, transition, hidden)."""
    def build(conn):
        _chromium_schema(conn)
        for i, (url, title, transition, hidden) in enumerate(rows, start=1):
            conn.execute("INSERT INTO urls VALUES (?, ?, ?, 1, ?)", (i, url, title, hidden))
            conn.execute("INSERT INTO visits VALUES (?, ?, ?, ?)",
                         (i, i, WEBKIT_TS + i * 600_000_000, transition))
    builder.add_db(domain, path, build)


class BrowserHistoryTests(unittest.TestCase):
    def setUp(self):
        self.builder = _BackupBuilder()
        self.extractor = BrowserHistoryExtractor()

    def tearDown(self):
        self.builder.cleanup()

    def test_safari_visits(self):
        _add_safari(self.builder, [("https://example.com/a", "A")])
        result = self.extractor.list_browser_history(self.builder.accessor())
        self.assertEqual(result["browsers_found"], ["safari"])
        visit = result["visits"][0]
        self.assertEqual(visit["url"], "https://example.com/a")
        self.assertEqual(visit["domain"], "example.com")
        self.assertEqual(visit["browser"], "safari")
        self.assertTrue(visit["visit_date"].startswith("2024-01-01T12:10"))
        self.assertEqual(result["errors"], [])

    def test_safari_reads_uncheckpointed_wal(self):
        _add_safari(self.builder, [("https://old.example/", "Old")],
                    wal_rows=[("https://recent.example/", "Recent")])
        urls = {v["url"] for v in self.extractor.list_browser_history(
            self.builder.accessor())["visits"]}
        self.assertEqual(urls, {"https://old.example/", "https://recent.example/"})

    def test_safari_profile_database_is_included(self):
        _add_safari(self.builder, [("https://personal.example/", "Personal")])
        _add_safari(self.builder, [("https://work.example/", "Work")],
                    path="Library/Safari/Profiles/ABCD-1234/History.db")
        result = self.extractor.list_browser_history(self.builder.accessor())
        urls = {v["url"] for v in result["visits"]}
        self.assertEqual(urls, {"https://personal.example/", "https://work.example/"})
        ids = [v["visit_id"] for v in result["visits"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_chrome_visits_skip_hidden_and_subframes(self):
        _add_chromium(self.builder, CHROME_DOMAIN, [
            ("https://news.example/", "News", 0, 0),          # LINK
            ("https://ads.example/frame", "Ad", 3, 0),        # AUTO_SUBFRAME
            ("https://hidden.example/", "Hidden", 1, 1),
            ("https://typed.example/", "Typed", 0x30000001, 0),  # TYPED + chain qualifiers
        ])
        result = self.extractor.list_browser_history(self.builder.accessor())
        self.assertEqual(result["browsers_found"], ["chrome"])
        urls = [v["url"] for v in result["visits"]]
        self.assertEqual(urls, ["https://typed.example/", "https://news.example/"])
        self.assertTrue(result["visits"][1]["visit_date"].startswith("2024-01-01T12:10"))
        self.assertTrue(all(v["browser"] == "chrome" for v in result["visits"]))

    def test_edge_detected_by_domain(self):
        _add_chromium(self.builder, EDGE_DOMAIN, [("https://bing.example/", "Bing", 1, 0)],
                      path="Library/Application Support/Microsoft/Edge/Default/History")
        probe = self.extractor.has_browser_history(self.builder.accessor())
        self.assertTrue(probe["edge"])
        self.assertFalse(probe["chrome"])
        self.assertEqual(probe["browsers"], ["edge"])

    def test_non_chromium_history_file_is_ignored(self):
        self.builder.add_db(CHROME_DOMAIN, "Library/Other/History",
                            lambda c: c.execute("CREATE TABLE unrelated (x)"))
        result = self.extractor.list_browser_history(self.builder.accessor())
        self.assertEqual(result["visits"], [])
        self.assertEqual(result["errors"], [])

    def test_browser_filter(self):
        _add_safari(self.builder, [("https://s.example/", "S")])
        _add_chromium(self.builder, CHROME_DOMAIN, [("https://c.example/", "C", 1, 0)])
        acc = self.builder.accessor()
        only_chrome = self.extractor.list_browser_history(acc, browser="chrome")
        self.assertEqual({v["browser"] for v in only_chrome["visits"]}, {"chrome"})
        both = self.extractor.list_browser_history(acc)
        self.assertEqual(both["browsers_found"], ["safari", "chrome"])

    def test_unrecognized_safari_schema_is_reported(self):
        self.builder.add_db("HomeDomain", "Library/Safari/History.db",
                            lambda c: c.execute("CREATE TABLE something_else (x)"))
        result = self.extractor.list_browser_history(self.builder.accessor())
        self.assertEqual(result["visits"], [])
        self.assertEqual([e["browser"] for e in result["errors"]], ["safari"])

    def test_unencrypted_backup_without_safari_explains_why(self):
        _add_chromium(self.builder, CHROME_DOMAIN, [("https://c.example/", "C", 1, 0)])
        probe = self.extractor.has_browser_history(self.builder.accessor(encrypted=False))
        self.assertEqual(probe["notice"], UNENCRYPTED_SAFARI_NOTICE)
        self.assertTrue(probe["has_any"])

    def test_no_notice_when_safari_present(self):
        _add_safari(self.builder, [("https://s.example/", "S")])
        probe = self.extractor.has_browser_history(self.builder.accessor())
        self.assertIsNone(probe["notice"])
        self.assertTrue(probe["safari"])


class _FakeDecryptedBackup:
    """Stands in for iphone_backup_decrypt.EncryptedBackup."""

    def __init__(self, files: dict):
        self._files = files  # (domain, relative_path) -> bytes

    def extract_file(self, relative_path, domain_like, output_filename):
        data = self._files.get((domain_like, relative_path))
        if data is None:
            raise FileNotFoundError(relative_path)
        with open(output_filename, "wb") as f:
            f.write(data)


class EncryptedGetFileTests(unittest.TestCase):
    def test_same_path_in_two_domains_does_not_collide(self):
        path = "profile.profile/browser.db"
        fake = _FakeDecryptedBackup({("DomainA", path): b"A", ("DomainB", path): b"B"})
        acc = LocalBackupAccessor("/nonexistent", "udid", {}, encrypted=True,
                                  decrypted_backup=fake)
        try:
            a = acc.get_file(path, domain="DomainA")
            b = acc.get_file(path, domain="DomainB")
            self.assertNotEqual(a, b)
            with open(a, "rb") as fa, open(b, "rb") as fb:
                self.assertEqual((fa.read(), fb.read()), (b"A", b"B"))
        finally:
            acc.cleanup()


if __name__ == "__main__":
    unittest.main()
