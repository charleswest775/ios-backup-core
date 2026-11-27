"""A big Manifest.db (backupgen's filler), and looking files up in one.

OE-132: in an encrypted backup, iphone-backup-decrypt's lookup of one file used Manifest.db's
index on flags, which nearly every row has, so each file read the whole list. A persona's own
rows never showed it; the filler's tens of thousands do. open_local_backup drops
that index from the decrypted copy, so lookups go by path.
"""

import contextlib
import io
import json
import os
import plistlib
import random
import sqlite3
import stat
import sys
import time
import unicodedata
from pathlib import Path

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, ROOT)

from backupgen.__main__ import main  # noqa: E402
from backupgen.filler import FOLDER_SHARE, MEDIA_ROOT, FillerError, check_filler, filler_domains  # noqa: E402
from backupgen.generate import EXPECTED, generate  # noqa: E402
from ios_backup_core.backup import open_local_backup  # noqa: E402

PASSWORD = "pw"
FILLER = {"files": 20000, "domains": 300, "seed": 1}


def _rows(backup_dir: Path, password=None) -> list[tuple]:
    """Every Manifest.db row: (fileID, domain, relativePath, flags, file)."""
    if password is None:
        conn = sqlite3.connect(f"{(backup_dir / 'Manifest.db').resolve().as_uri()}?mode=ro&immutable=1", uri=True)
        try:
            return conn.execute("SELECT fileID, domain, relativePath, flags, file FROM Files").fetchall()
        finally:
            conn.close()
    accessor = open_local_backup(str(backup_dir), password=password)
    try:
        with accessor._decrypted_backup.manifest_db_cursor() as cur:
            return cur.execute("SELECT fileID, domain, relativePath, flags, file FROM Files").fetchall()
    finally:
        accessor.cleanup()


def _mbfile(blob: bytes) -> dict:
    """An MBFile record's top object, with its UIDs resolved."""
    archive = plistlib.loads(blob)
    objects = archive["$objects"]
    top = objects[archive["$top"]["root"].data]
    return {k: (objects[v.data] if isinstance(v, plistlib.UID) else v) for k, v in top.items()}


@pytest.fixture(scope="module")
def backups(tmp_path_factory):
    """The test persona's iOS 26 snapshot, encrypted, without and with the filler: {name: folder}."""
    root = tmp_path_factory.mktemp("scale")
    out = {}
    for name, filler in (("plain", None), ("padded", FILLER)):
        out[name] = generate("testpersona", "ios26", root / name, password=PASSWORD, filler=filler)
    return out


@pytest.fixture(scope="module")
def rows(backups):
    return {name: _rows(folder, PASSWORD) for name, folder in backups.items()}


def _added(rows) -> list[tuple]:
    before = {r[0] for r in rows["plain"]}
    return [r for r in rows["padded"] if r[0] not in before]


# ── what the filler adds ─────────────────────────────────────────────────────


def test_the_filler_adds_its_files_folders_links_and_domains(rows):
    added = _added(rows)
    by_flags = {flags: sum(1 for r in added if r[3] == flags) for flags in (1, 2, 4)}
    assert by_flags[1] == FILLER["files"]
    assert by_flags[4] == 5
    assert by_flags[1] + by_flags[2] + by_flags[4] == len(added)
    assert abs(by_flags[2] / len(added) - FOLDER_SHARE) < 0.01, by_flags
    assert {r[1] for r in added} == set(filler_domains(FILLER["domains"]))
    kinds = {d.split("-", 1)[0] for d in {r[1] for r in rows["padded"] if r[1] in filler_domains(FILLER["domains"])}}
    assert kinds == {"MediaDomain", "AppDomain", "AppDomainPlugin", "AppDomainGroup"}
    # In MediaDomain, only under its own folder (and that folder's own folders), apart from real data
    media = {r[2] for r in added if r[1] == "MediaDomain"}
    assert MEDIA_ROOT in media and f"{MEDIA_ROOT}/today" in media
    assert {p for p in media if not p.startswith(MEDIA_ROOT + "/")} <= {"", "Library", "Library/Caches", MEDIA_ROOT}
    assert not [p for p in media if p.count("com.example.filler") > 1]
    # Folders nest several deep, and some of them hold nothing
    depths = [r[2].count("/") + 1 for r in added if r[3] == 2 and r[2]]
    assert sum(depths) / len(depths) > 3
    parents = {(r[1], r[2].rpartition("/")[0]) for r in added}
    empty = [r for r in added if r[3] == 2 and r[2] and (r[1], r[2]) not in parents]
    assert len(empty) > len(depths) // 40  # about 1 in 20 at FOLDER_SHARE 0.30


def test_the_filler_changes_no_data_and_no_answer_key(backups, rows):
    plain, padded = backups["plain"], backups["padded"]
    assert (plain.parent / EXPECTED).read_bytes() == (padded.parent / EXPECTED).read_bytes()
    padded_rows = {r[0]: r for r in rows["padded"]}
    assert all(r[0] in padded_rows and r[1:4] == padded_rows[r[0]][1:4] for r in rows["plain"])
    accessors = {name: open_local_backup(str(folder), password=PASSWORD) for name, folder in backups.items()}
    try:
        for _, domain, path, flags, _ in rows["plain"]:
            if flags == 1:
                files = [Path(a.get_file(path, domain)).read_bytes() for a in accessors.values()]
                assert files[0] == files[1], (domain, path)
    finally:
        for accessor in accessors.values():
            accessor.cleanup()


def test_backups_have_no_filler_unless_asked(rows):
    assert not [r for r in rows["plain"] if "org.example.app" in r[1] or "com.example.filler" in r[2]]


def test_the_filler_follows_its_own_seed(tmp_path):
    small = {"files": 200, "domains": 12}
    names = {}
    for name, seed in (("a", 1), ("b", 1), ("c", 2)):
        backup_dir = generate("testpersona", "ios15", tmp_path / name, filler={**small, "seed": seed})
        names[name] = sorted((r[1], r[2], r[3]) for r in _rows(backup_dir))
    assert names["a"] == names["b"] != names["c"]


def test_the_same_options_make_the_same_filler_beside_any_pack(tmp_path):
    """What the filler adds doesn't depend on what the pack already has (einstein has no MediaDomain rows)."""
    filler = {"files": 400, "domains": 10, "seed": 5}
    found = []
    for persona in ("testpersona", "einstein"):
        backup_dir = generate(persona, "ios26", tmp_path / persona, filler=filler)
        found.append({(r[1], r[2], r[3]) for r in _rows(backup_dir)
                      if r[1] in filler_domains(10) and (r[1] != "MediaDomain" or r[2].startswith(MEDIA_ROOT))})
    assert found[0] == found[1] and sum(1 for row in found[0] if row[2] == 2) > filler["files"] // 4


def test_the_filler_never_replaces_a_packs_file(tmp_path):
    from datetime import datetime, timezone

    from backupgen.container import BackupWriter, Device
    from backupgen.filler import add_filler

    writer = BackupWriter(tmp_path, Device("x", "iPhone15,4", "x", "17.3", "21D50", ""),
                          datetime(2024, 1, 1, tzinfo=timezone.utc), "17.3", seed=1)
    writer.add_file("MediaDomain", f"{MEDIA_ROOT}/today", b"the pack's")
    with pytest.raises(FillerError, match="already in the backup"):
        add_filler(writer, check_filler({"files": 60, "domains": 4}))


def test_symlinks_have_a_target_and_no_stored_file(backups, rows):
    links = [r for r in _added(rows) if r[3] == 4]
    assert len(links) == 5
    targets = []
    for file_id, domain, path, _, blob in links:
        record = _mbfile(blob)
        assert stat.S_ISLNK(record["Mode"]) and record["Target"] and record["Size"] == 0, path
        assert "EncryptionKey" not in record and "Digest" not in record
        assert not (backups["padded"] / file_id[:2] / file_id).exists()
        assert (domain, path.rpartition("/")[0]) in {(r[1], r[2]) for r in rows["padded"] if r[3] == 2}
        targets.append(record["Target"])
    assert any(t.startswith("/private/var/") for t in targets) and any(t.startswith("..") for t in targets)
    accessor = open_local_backup(str(backups["padded"]), password=PASSWORD)
    try:
        listed = {(f["domain"], f["path"]) for f in accessor.list_files()}
        assert not [(d, p) for _, d, p, _, _ in links if (d, p) in listed]  # files only
        assert all(accessor.get_file(p, d) is None for _, d, p, _, _ in links)
    finally:
        accessor.cleanup()


def test_deep_long_and_non_ascii_paths_read_back(backups, rows):
    files = {r[2]: r[1] for r in _added(rows) if r[3] == 1}
    deep = sorted(p for p in files if p.count("/") >= 24)
    long = sorted(p for p in files if len(p) > 260)  # Windows's MAX_PATH
    decomposed = [unicodedata.normalize("NFD", p) for p in ("Library/Café/menu.txt", "Documents/Ångström.txt")]
    other_scripts = ["Documents/Größe/Übersicht.txt", "Documents/日本語のメモ/写真.dat", "Documents/Ελληνικά/σημείωση.txt",
                     "Documents/emoji 🎻/notes 📝.txt", "Documents/Café/menu.txt", *decomposed]
    assert deep and long and set(other_scripts) <= set(files)
    assert all(unicodedata.normalize("NFC", p) != p for p in decomposed)
    assert [p for p in files if p.endswith("Café/menu.txt")] == ["Documents/Café/menu.txt"]  # composed, only there
    accessor = open_local_backup(str(backups["padded"]), password=PASSWORD)
    try:
        for path in deep[:3] + long[:3] + other_scripts:
            local = accessor.get_file(path, files[path])
            assert local and 1 <= os.path.getsize(local) <= 64, path
        # Looked up byte for byte: the composed form of a decomposed name isn't that file
        for path in decomposed:
            assert accessor.get_file(unicodedata.normalize("NFC", path), files[path]) is None
    finally:
        accessor.cleanup()


def test_an_unencrypted_backup_takes_the_filler_too(tmp_path):
    backup_dir = generate("testpersona", "ios15", tmp_path, filler={"files": 120, "domains": 9, "seed": 3})
    rows = _rows(backup_dir)
    added = [r for r in rows if "org.example.app" in r[1] or "com.example.filler" in r[2]]
    assert sum(1 for r in added if r[3] == 1) == 120 and sum(1 for r in added if r[3] == 4) == 5
    for file_id, _, _, flags, _ in added:
        assert (backup_dir / file_id[:2] / file_id).is_file() is (flags == 1)
    accessor = open_local_backup(str(backup_dir))
    try:
        # (An unencrypted backup's list_files lists folders and links too, as it always has; an
        # encrypted one's, files only.) A link reads as no file, and a file reads back.
        listed = {(f["domain"], f["path"]) for f in accessor.list_files()}
        links = [(d, p) for _, d, p, flags, _ in added if flags == 4]
        assert set(links) <= listed and all(accessor.get_file(p, d) is None for d, p in links)
        domain, path = next((d, p) for _, d, p, flags, _ in added if flags == 1)
        assert 1 <= os.path.getsize(accessor.get_file(path, domain)) <= 64
    finally:
        accessor.cleanup()


# ── options ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("filler, message", [
    ([1], "should be a JSON object"),
    ({"rows": 1}, "unknown key 'rows'"),
    ({"files": -1}, "files should be a whole number"),
    ({"files": 1.5}, "files should be a whole number"),
    ({"files": True}, "files should be a whole number"),
    ({"files": 10, "domains": 0}, "domains should be 1 or more"),
])
def test_bad_filler_options_are_refused(tmp_path, filler, message):
    with pytest.raises(FillerError, match=message):
        check_filler(filler)
    with pytest.raises(FillerError, match=message):
        generate("testpersona", "ios15", tmp_path, filler=filler)


def test_the_command_line_takes_a_filler(tmp_path, capsys):
    assert main(["generate", "testpersona", "--snapshot", "ios15", "--out", str(tmp_path),
                 "--filler", json.dumps({"files": 60, "domains": 7, "seed": 2})]) == 0
    backup_dir = Path(capsys.readouterr().out.strip())
    assert sum(1 for r in _rows(backup_dir) if r[1].startswith("AppDomain") and r[3] == 1) > 0
    for bad in ("{files: 1}", '{"rows": 1}'):
        with pytest.raises(SystemExit):
            main(["generate", "testpersona", "--snapshot", "ios15", "--out", str(tmp_path / "bad"), "--filler", bad])
        assert "--filler" in capsys.readouterr().err


# ── looking a file up in a padded encrypted backup ───────────────────────────


def _filler_files(rows, count: int, seed: int = 1) -> list[tuple[str, str]]:
    files = sorted((r[1], r[2]) for r in _added(rows) if r[3] == 1)
    return random.Random(seed).sample(files, count)


def test_a_files_lookup_goes_by_its_path(backups, rows):
    """The plan of the query iphone-backup-decrypt itself runs for one file, as open_local_backup leaves it."""
    accessor = open_local_backup(str(backups["padded"]), password=PASSWORD)
    try:
        library = accessor._decrypted_backup
        ran = []
        library._temp_manifest_db_conn.set_trace_callback(ran.append)
        domain, path = _filler_files(rows, 1)[0]
        assert accessor.get_file(path, domain)
        library._temp_manifest_db_conn.set_trace_callback(None)
        lookup = [sql for sql in ran if "relativePath" in sql and "LIMIT 1" in sql]
        assert lookup, ran
        with library.manifest_db_cursor() as cur:
            plan = " | ".join(row[-1] for row in cur.execute("EXPLAIN QUERY PLAN " + lookup[-1].strip().rstrip(";")))
        assert "FilesFlagsIdx" not in plan and "FilesRelativePathIdx" in plan, plan
    finally:
        accessor.cleanup()


def test_files_in_a_padded_backup_are_found_quickly(backups, rows):
    """A loose budget on lookups of files that aren't there: each one, read row by row, is the whole list.

    20,000 files (about 28,600 rows): 1,000 such lookups took 0.02 s on a Mac by path and 5 s with
    FilesFlagsIdx, and a Windows PC reads every row about three times slower. Lookups of files that
    are there also decrypt and write each one, which on Windows takes most of the time (about 2 s
    for 300) and hides the difference, so those are only checked to be found.
    """
    accessor = open_local_backup(str(backups["padded"]), password=PASSWORD)
    try:
        assert all(accessor.get_file(path, domain) for domain, path in _filler_files(rows, 300))
        missing = [(domain, f"{path}.not-there") for domain, path in _filler_files(rows, 1000, 2)]
        with contextlib.redirect_stderr(io.StringIO()):  # get_file logs each one it can't find
            start = time.perf_counter()
            assert not any(accessor.get_file(path, domain) for domain, path in missing)
            seconds = time.perf_counter() - start
    finally:
        accessor.cleanup()
    assert seconds < 1.0, round(seconds, 3)


def test_a_backup_still_opens_if_lookups_cant_be_sped_up(backups, monkeypatch):
    from ios_backup_core import backup

    def refuse(self):
        raise sqlite3.OperationalError("disk full")

    real = backup.EncryptedBackup.manifest_db_cursor
    calls = []

    def flaky(self):
        calls.append(1)
        return real(self) if len(calls) == 1 else refuse(self)  # the password check works, the speed-up doesn't

    monkeypatch.setattr(backup.EncryptedBackup, "manifest_db_cursor", flaky)
    accessor = open_local_backup(str(backups["plain"]), password=PASSWORD)
    try:
        assert len(calls) == 2
    finally:
        accessor.cleanup()
