"""backupgen's backup folder itself: Info.plist by what made the backup, the apps, chip IDs, Health.

Einstein's iOS 26 snapshot is made the way OpenExtract makes backups (host "pymobiledevice3"), so
its Info.plist has no "Last Backup Date" and only Status.plist and Manifest.plist date it; its
phone has WhatsApp installed with no chats in the backup. ios_backup_core's backup info must
still give that backup its date.
"""

import json
import os
import plistlib
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, ROOT)

from backupgen import PERSONAS_DIR  # noqa: E402
from backupgen.container import App, BackupWriter, Device, Host  # noqa: E402
from backupgen.generate import EXPECTED, generate  # noqa: E402
from backupgen.persona import PersonaError, load_persona  # noqa: E402
from backupgen.sqlitegen import load_schema, schema_objects  # noqa: E402
from ios_backup_core.backup import BackupReader, _read_backup_info, open_local_backup  # noqa: E402

WHATSAPP = "net.whatsapp.WhatsApp"
PASSWORD = "pw"

# Exactly the keys pymobiledevice3's init_mobile_backup_factory_info writes for an iPhone with a
# SIM and a phone number and no iTunes settings: no "Last Backup Date", "Product Name",
# "iTunes Settings" or macOS keys, and an "iTunes Version".
PYMOBILEDEVICE3_KEYS = {
    "Applications", "Build Version", "Device Name", "Display Name", "GUID", "ICCID", "IMEI",
    "Installed Applications", "Phone Number", "Product Type", "Product Version", "Serial Number",
    "Target Identifier", "Target Type", "Unique Identifier", "iTunes Files", "iTunes Version",
}
FINDER_KEYS = {
    "Applications", "Build Version", "Device Name", "Display Name", "GUID", "ICCID", "IMEI",
    "Installed Applications", "Last Backup Date", "Phone Number", "Product Name", "Product Type",
    "Product Version", "Serial Number", "Target Identifier", "Target Type", "Unique Identifier",
    "iTunes Files", "iTunes Settings", "macOS Build Version", "macOS Version",
}


def _plist(backup_dir: Path, name: str) -> dict:
    return plistlib.loads((backup_dir / name).read_bytes())


def _expected(out: Path) -> dict:
    return json.loads((out / EXPECTED).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def einstein26(tmp_path_factory):
    """Einstein's iOS 26 snapshot, plain and encrypted: {password: (backup folder, answer key)}."""
    root = tmp_path_factory.mktemp("einstein26")
    out = {}
    for password in (None, PASSWORD):
        folder = root / ("encrypted" if password else "plain")
        out[password] = (generate("einstein", "ios26", folder, password=password), _expected(folder))
    return out


def _writer(tmp_path: Path, product_type: str = "iPhone15,4", **kwargs) -> BackupWriter:
    device = Device(name="Test iPhone", product_type=product_type, product_name="iPhone", ios="26.5", build="23F77",
                    phone_number="+44 7700 900001")
    return BackupWriter(tmp_path, device, datetime(2026, 6, 14, 17, 30, tzinfo=timezone.utc), "17.3", seed=3, **kwargs)


# ── Info.plist, by what made the backup ──────────────────────────────────────


@pytest.mark.parametrize("password", [None, PASSWORD], ids=["plain", "encrypted"])
def test_a_pymobiledevice3_backup_has_exactly_its_info_plist_keys(einstein26, password):
    backup_dir, expected = einstein26[password]
    assert expected["host"] == "pymobiledevice3"
    info = _plist(backup_dir, "Info.plist")
    assert set(info) == PYMOBILEDEVICE3_KEYS
    assert info["iTunes Version"] == "10.0.1" and info["iTunes Files"] == {}
    assert isinstance(info["GUID"], bytes) and len(info["GUID"]) == 16
    assert info["Unique Identifier"] == expected["udid"].upper() == backup_dir.name.upper()
    assert info["Target Identifier"] == expected["udid"]
    assert info["Product Type"] == expected["product_type"] and info["Product Version"] == expected["ios"]
    # Every installed app is listed; only App Store apps carry their store metadata.
    assert info["Installed Applications"] == expected["apps"] == [WHATSAPP, "org.example.olympia.reader"]
    assert list(info["Applications"]) == [WHATSAPP]
    store = info["Applications"][WHATSAPP]
    assert set(store) == {"ApplicationSINF", "iTunesMetadata", "PlaceholderIcon"}
    assert plistlib.loads(store["iTunesMetadata"])["softwareVersionBundleId"] == WHATSAPP
    assert store["PlaceholderIcon"].startswith(b"\x89PNG\r\n\x1a\n")
    # Manifest.plist and Status.plist are the iPhone's, whatever made the backup: both dated.
    manifest = _plist(backup_dir, "Manifest.plist")
    assert set(manifest["Applications"]) == {WHATSAPP, "org.example.olympia.reader"}
    whatsapp = manifest["Applications"][WHATSAPP]
    assert (whatsapp["CFBundleIdentifier"], whatsapp["ContainerContentClass"]) == (WHATSAPP, "Data/Application")
    assert whatsapp["Path"].startswith("/private/var/containers/Bundle/Application/") and whatsapp["Path"].endswith("/WhatsApp.app")
    made = datetime.fromisoformat(expected["backup_date"]).astimezone(timezone.utc).replace(tzinfo=None)
    assert manifest["Date"] == _plist(backup_dir, "Status.plist")["Date"] == made


def test_a_finder_backup_keeps_its_info_plist_keys(tmp_path):
    backup_dir = generate("einstein", "ios17", tmp_path)
    expected = _expected(tmp_path)
    info = _plist(backup_dir, "Info.plist")
    assert expected["host"] == "finder" and expected["apps"] == []
    assert set(info) == FINDER_KEYS
    assert isinstance(info["GUID"], str) and len(info["GUID"]) == 32
    assert info["Installed Applications"] == [] and info["Applications"] == {}
    assert _plist(backup_dir, "Manifest.plist")["Applications"] == {}
    assert info["Last Backup Date"] == datetime.fromisoformat(expected["backup_date"]).astimezone(timezone.utc).replace(tzinfo=None)


def test_pymobiledevice3_writes_the_unique_identifier_in_upper_case(tmp_path):
    """An iPhone older than the XS has a 40-hex UDID in lower case: only "Unique Identifier" is upper case."""
    writer = _writer(tmp_path, product_type="iPhone10,1", host=Host(kind="pymobiledevice3"))
    backup_dir = writer.finalize()
    info = _plist(backup_dir, "Info.plist")
    assert writer.device.udid == writer.device.udid.lower() and len(writer.device.udid) == 40
    assert info["Unique Identifier"] == writer.device.udid.upper()
    assert info["Target Identifier"] == writer.device.udid == backup_dir.name


def test_app_store_metadata_only_for_store_apps_whatever_made_the_backup(tmp_path):
    apps = [App("org.example.one", "One", "7"), App("org.example.dev", "Dev", store=False)]
    for kind in ("finder", "pymobiledevice3"):
        backup_dir = _writer(tmp_path / kind, host=Host(kind=kind), apps=apps).finalize()
        info, manifest = _plist(backup_dir, "Info.plist"), _plist(backup_dir, "Manifest.plist")
        assert info["Installed Applications"] == ["org.example.one", "org.example.dev"], kind
        assert list(info["Applications"]) == ["org.example.one"], kind
        assert {k: v["CFBundleVersion"] for k, v in manifest["Applications"].items()} == \
            {"org.example.one": "7", "org.example.dev": "1.0"}, kind


def test_an_unknown_host_kind_is_refused():
    with pytest.raises(ValueError, match="unknown host kind 'itunes'"):
        Host(kind="itunes")


# ── ios_backup_core dates a backup with no "Last Backup Date" ────────────────


@pytest.mark.parametrize("password", [None, PASSWORD], ids=["plain", "encrypted"])
def test_core_dates_a_pymobiledevice3_backup_from_status_plist(einstein26, password):
    backup_dir, expected = einstein26[password]
    made = datetime.fromisoformat(expected["backup_date"]).astimezone(timezone.utc).replace(tzinfo=None)
    assert _read_backup_info(str(backup_dir))["last_backup"] == made.isoformat()
    accessor = open_local_backup(str(backup_dir), password=password)
    try:
        assert accessor.info["last_backup"] == made.isoformat()
    finally:
        accessor.cleanup()
    assert BackupReader.from_path(str(backup_dir), password=password)._accessor.info["last_backup"] == made.isoformat()


def test_core_keeps_info_plists_date_when_there_is_one(tmp_path):
    backup_dir = generate("einstein", "ios17", tmp_path)
    info = _plist(backup_dir, "Info.plist")
    status_path = backup_dir / "Status.plist"
    status = _plist(backup_dir, "Status.plist")
    status["Date"] = datetime(2000, 1, 1)  # not the date to use
    status_path.write_bytes(plistlib.dumps(status, fmt=plistlib.FMT_BINARY))
    assert _read_backup_info(str(backup_dir))["last_backup"] == info["Last Backup Date"].isoformat()


@pytest.mark.parametrize("status", [None, b"", b"not a plist", {"SnapshotState": "finished"}, {"Date": "yesterday"}])
def test_core_has_no_date_without_one_in_status_plist(einstein26, tmp_path, status):
    import shutil

    backup_dir = tmp_path / einstein26[None][0].name
    shutil.copytree(einstein26[None][0], backup_dir)
    if status is None:
        (backup_dir / "Status.plist").unlink()
    else:
        (backup_dir / "Status.plist").write_bytes(status if isinstance(status, bytes) else plistlib.dumps(status))
    info = _read_backup_info(str(backup_dir))
    assert info["last_backup"] == "" and info["udid"] == backup_dir.name.upper()


# ── chip IDs ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("product_type", ["iPhone18,1", "iPhone18,2", "iPhone18,3", "iPhone18,4", "iPhone18,5"])
def test_iphone_17_family_udids_start_with_their_chip_id(product_type):
    import random

    device = Device(name="x", product_type=product_type, product_name="x", ios="26.5", build="23F77",
                    phone_number="").fill_identifiers(random.Random(1))
    chip, _, ecid = device.udid.partition("-")
    assert chip == "00008150" and len(ecid) == 16 and ecid == ecid.upper()


# ── Health's empty databases ─────────────────────────────────────────────────


def test_encrypted_backups_hold_healths_two_databases_empty(einstein26, tmp_path):
    backup_dir, expected = einstein26[PASSWORD]
    assert expected["absent"]["health"] == "none"
    accessor = open_local_backup(str(backup_dir), password=PASSWORD)
    try:
        health = sorted(r["path"] for r in accessor.list_files(domain="HealthDomain") if r["path"])
        assert health == ["Health/healthdb.sqlite", "Health/healthdb_secure.sqlite"]
        for path in health:
            database = path.rsplit("/", 1)[1]
            local = accessor.get_file(path, "HealthDomain")
            built = sorted((n, sql) for _, n, sql in schema_objects(local) if not n.startswith("sqlite_"))
            assert built == sorted((o["name"], o["sql"]) for o in load_schema("26.5", database)["objects"]), database
            conn = sqlite3.connect(f"{Path(local).resolve().as_uri()}?mode=ro&immutable=1", uri=True)
            try:
                tables = [n for (n,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' "
                                                     "AND name NOT LIKE 'sqlite_%'")]
                assert tables and all(conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] == 0 for t in tables)
            finally:
                conn.close()
    finally:
        accessor.cleanup()


def test_unencrypted_backups_leave_health_out(einstein26):
    backup_dir, expected = einstein26[None]
    assert expected["absent"]["health"] == "none"
    manifest = sqlite3.connect(backup_dir / "Manifest.db")
    try:
        assert manifest.execute("SELECT COUNT(*) FROM Files WHERE domain = 'HealthDomain'").fetchone()[0] == 0
    finally:
        manifest.close()


# ── WhatsApp on the phone, its chats not in the backup ───────────────────────


@pytest.mark.parametrize("password", [None, PASSWORD], ids=["plain", "encrypted"])
def test_einstein_has_whatsapp_installed_and_none_of_its_files(einstein26, password):
    backup_dir, expected = einstein26[password]
    assert WHATSAPP in expected["apps"] and expected["whatsapp"] is None and expected["absent"]["whatsapp"] == "none"
    accessor = open_local_backup(str(backup_dir), password=password)
    try:
        assert not [r for r in accessor.list_files() if "whatsapp" in r["domain"].lower()]
    finally:
        accessor.cleanup()


# ── the apps list in persona.json ────────────────────────────────────────────


def _pack(tmp_path: Path, persona_apps=None, snapshot_apps=None, host=None) -> Path:
    import shutil

    root = tmp_path / "packs"
    shutil.copytree(PERSONAS_DIR / "testpersona", root / "p")
    path = root / "p" / "persona.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["id"] = "p"
    if persona_apps is not None:
        data["apps"] = persona_apps
    if snapshot_apps is not None:
        data["snapshots"][0]["apps"] = snapshot_apps
    if host is not None:
        data["snapshots"][0]["host"] = host
    path.write_text(json.dumps(data), encoding="utf-8")
    return root


def test_a_pack_without_apps_lists_none():
    persona = load_persona("testpersona")
    assert all(s.apps == [] for s in persona.snapshots.values())


def test_apps_default_to_the_packs_list(tmp_path):
    root = _pack(tmp_path, persona_apps=[{"id": "org.example.a", "name": "A"}],
                 snapshot_apps=[{"id": "org.example.b", "name": "B", "version": "2", "store": False}])
    persona = load_persona("p", root=root)
    first, *rest = persona.snapshots.values()
    assert first.apps == [App("org.example.b", "B", "2", False)]
    assert rest and all(s.apps == [App("org.example.a", "A", "1.0", True)] for s in rest)


@pytest.mark.parametrize("apps, message", [
    ({"id": "x"}, "apps should be a JSON list"),
    (["net.whatsapp.WhatsApp"], r"apps\[1\] should be a JSON object"),
    ([{"name": "No id"}], r"apps\[1\]: needs an 'id'"),
    ([{"id": "org.example.a"}], r"apps\[1\]: needs an 'name'"),
    ([{"id": "org.example.a", "name": "A", "bundle": "x"}], r"apps\[1\]: unknown key 'bundle'"),
    ([{"id": "org.example.a", "name": "A", "store": "yes"}], r"apps\[1\]: store should be a bool"),
    ([{"id": "org.example.a", "name": "A", "version": 3}], r"apps\[1\]: version should be a str"),
    ([{"id": "org.example.a", "name": "A"}, {"id": "org.example.a", "name": "Again"}], r"apps\[2\]: org.example.a is listed twice"),
])
@pytest.mark.parametrize("on, where", [("persona", "persona.json"), ("snapshot", "persona.json snapshot ios15")])
def test_bad_apps_are_refused(tmp_path, apps, message, on, where):
    root = _pack(tmp_path, **{f"{on}_apps": apps})
    with pytest.raises(PersonaError, match=f"{where}: {message}"):
        load_persona("p", root=root)


@pytest.mark.parametrize("host, message", [
    ({"kind": "itunes"}, r"host: unknown kind 'itunes' \(one of: finder, pymobiledevice3\)"),
    ({"kind": "finder", "macos": "14"}, r"host: .*unexpected keyword argument 'macos'"),
    ("pymobiledevice3", "host should be a JSON object"),
])
def test_bad_hosts_are_refused(tmp_path, host, message):
    with pytest.raises(PersonaError, match=f"persona.json snapshot ios15: {message}"):
        load_persona("p", root=_pack(tmp_path, host=host))
