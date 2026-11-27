"""Write a backup folder: hashed files, Manifest.db, Manifest.plist, Info.plist, Status.plist.

Layout and record formats follow real iOS 13–17 backups (see ``schemas/ios-*/container.json``):
files live at ``<fileID[:2]>/<fileID>`` with ``fileID = SHA-1("<domain>-<relativePath>")``.
Each Manifest.db row carries an NSKeyedArchiver ``MBFile`` record. Directories,
including every domain root, get their own rows, as on a device.

The iPhone writes Manifest.db, Manifest.plist and the last Status.plist itself, whatever
program asked for the backup; only Info.plist is the computer's (see Host). So the host kind
changes Info.plist and nothing else.
"""

from __future__ import annotations

import hashlib
import json
import plistlib
import random
import sqlite3
import struct
import uuid
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Optional

from backupgen import SCHEMAS_DIR
from backupgen.crypto import Keybag, encrypt_cbc

FILE_MODE = 0o100644
DIR_MODE = 0o40700
ROOT_MODE = 0o40711
LINK_MODE = 0o120755  # S_IFLNK, lrwxr-xr-x
# Manifest.db's flags column: what a row is.
FLAG_FILE, FLAG_DIRECTORY, FLAG_SYMLINK = 1, 2, 4
MOBILE_UID = 501
_LOCKDOWN_EXTRAS = {
    "com.apple.Accessibility": {
        "ClosedCaptioningEnabledByiTunes": False,
        "InvertDisplayEnabledByiTunes": False,
        "MonoAudioEnabledByiTunes": False,
        "SpeakAutoCorrectionsEnabledByiTunes": False,
        "VoiceOverTouchEnabledByiTunes": False,
        "ZoomTouchEnabledByiTunes": False,
    },
    "com.apple.MobileDeviceCrashCopy": {"ShouldSubmit": False},
    "com.apple.TerminalFlashr": {},
    "com.apple.mobile.data_sync": {},
    "com.apple.mobile.iTunes.accessories": {},
    "com.apple.mobile.wireless_lockdown": {"EnableWifiConnections": False},
}
# Chip identifiers that prefix modern UDIDs, by ProductType.
# Phones from the iPhone XS on have a UDID made of the chip ID and the ECID; older ones a 40-hex one.
# From public device tables (The Apple Wiki's model list): the iPhone 17 family (iPhone18,1 17 Pro,
# 18,2 17 Pro Max, 18,3 17, 18,4 Air, 18,5 17e) all use chip ID 0x8150, as the iPhone 16 family
# (iPhone17,x) all use 0x8140.
_CHIP_IDS = {
    "iPhone12,1": "00008030", "iPhone15,4": "00008120", "iPhone16,2": "00008130",
    "iPhone17,1": "00008140", "iPhone17,2": "00008140", "iPhone17,3": "00008140", "iPhone17,4": "00008140",
    "iPhone18,1": "00008150", "iPhone18,2": "00008150", "iPhone18,3": "00008150", "iPhone18,4": "00008150",
    "iPhone18,5": "00008150",
}
# The "iTunes Version" pymobiledevice3 writes when the iPhone doesn't report the lowest iTunes it
# works with (com.apple.mobile.iTunes MinITunesVersion). Readers don't use it.
_ITUNES_VERSION = "10.0.1"


def file_id(domain: str, relative_path: str) -> str:
    return hashlib.sha1(f"{domain}-{relative_path}".encode("utf-8")).hexdigest()


def load_container_shape(version: str) -> dict:
    path = SCHEMAS_DIR / f"ios-{version}" / "container.json"
    return json.loads(path.read_text(encoding="utf-8"))


def mbfile_record(relative_path: str, *, mode: int, size: int, protection_class: int, inode: int, stamp: int,
                  wrapped_key: Optional[bytes] = None, digest: Optional[bytes] = None,
                  target: Optional[str] = None) -> bytes:
    """A Manifest.db row's ``file`` column: an NSKeyedArchiver ``MBFile``. A symbolic link's has its
    ``Target``, the path it points to."""
    top = {
        "LastModified": stamp,
        "Flags": 0,
        "GroupID": MOBILE_UID,
        "LastStatusChange": stamp,
        "RelativePath": plistlib.UID(2),
        "Birth": stamp,
        "Size": size,
        "InodeNumber": inode,
        "Mode": mode,
        "UserID": MOBILE_UID,
        "ProtectionClass": protection_class,
    }
    objects: list = ["$null", top, relative_path]
    if target is not None:
        top["Target"] = plistlib.UID(len(objects))
        objects.append(target)
    if digest is not None:
        top["Digest"] = plistlib.UID(len(objects))
        objects.append(digest)
    if wrapped_key is not None:
        top["EncryptionKey"] = plistlib.UID(len(objects))
        objects.append({"NS.data": wrapped_key, "$class": plistlib.UID(len(objects) + 1)})
        objects.append({"$classname": "NSMutableData", "$classes": ["NSMutableData", "NSData", "NSObject"]})
    top["$class"] = plistlib.UID(len(objects))
    objects.append({"$classname": "MBFile", "$classes": ["MBFile", "NSObject"]})
    archive = {"$version": 100000, "$archiver": "NSKeyedArchiver", "$top": {"root": plistlib.UID(1)}, "$objects": objects}
    return plistlib.dumps(archive, fmt=plistlib.FMT_BINARY, sort_keys=False)


@dataclass
class Device:
    """Identity of the backed-up phone. Identifiers are obviously synthetic (serials start with SYN)."""

    name: str
    product_type: str
    product_name: str
    ios: str
    build: str
    phone_number: str
    udid: str = ""
    serial: str = ""
    imei: str = ""
    iccid: str = ""
    guid: str = ""

    def fill_identifiers(self, rng: random.Random) -> "Device":
        hex_upper = "0123456789ABCDEF"
        if not self.udid:
            chip = _CHIP_IDS.get(self.product_type)
            self.udid = (
                f"{chip}-{''.join(rng.choice(hex_upper) for _ in range(16))}"
                if chip else "".join(rng.choice("0123456789abcdef") for _ in range(40))
            )
        if not self.serial:
            self.serial = "SYN" + "".join(rng.choice("0123456789ABCDEFGHJKLMNPQRSTUVWXYZ") for _ in range(9))
        if not self.imei:
            self.imei = "00" + "".join(rng.choice("0123456789") for _ in range(13))  # "00" TAC: not allocated
        if not self.iccid:
            self.iccid = "89990" + "".join(rng.choice("0123456789") for _ in range(14))
        if not self.guid:
            self.guid = "".join(rng.choice(hex_upper) for _ in range(32))
        return self


HOST_KINDS = ("finder", "pymobiledevice3")


@dataclass
class Host:
    """What made the backup, which decides how Info.plist looks (persona.json snapshot "host").

    "finder": Finder on macOS (iTunes' keys plus "macOS Version" and "macOS Build Version").
    "pymobiledevice3": pymobiledevice3's Mobilebackup2Service, which OpenExtract backs up with:
    exactly the keys its init_mobile_backup_factory_info writes (read from its source to match
    the file format), so no "Last Backup Date", "Product Name", "iTunes Settings" (it writes that
    only for an iPhone that has iTunes settings) or macOS keys; an "iTunes Version"; "Unique
    Identifier" in upper case; "GUID" as 16 raw bytes; and "Applications" only for apps with App
    Store metadata. A backup made this way is dated only by Manifest.plist and Status.plist,
    which the iPhone writes. The macOS fields are only Finder's.
    """

    kind: str = "finder"
    macos_version: str = "12.2.1"
    macos_build: str = "21D62"

    def __post_init__(self) -> None:
        if self.kind not in HOST_KINDS:
            raise ValueError(f"unknown host kind {self.kind!r} (one of: {', '.join(HOST_KINDS)})")


@dataclass(frozen=True)
class App:
    """An app installed on the phone (persona.json "apps"), as Info.plist and Manifest.plist list it.

    ``store`` is whether it came from the App Store, so it has the store metadata (its SINF and
    iTunesMetadata) that Info.plist's "Applications" holds; every app is in "Installed
    Applications" and Manifest.plist's "Applications" either way.
    """

    bundle_id: str
    name: str  # the .app bundle's name, without ".app"
    version: str = "1.0"  # CFBundleVersion
    store: bool = True


@dataclass
class _Entry:
    domain: str
    relative_path: str
    flags: int  # FLAG_FILE, FLAG_DIRECTORY or FLAG_SYMLINK
    data: bytes = b""
    protection_class: int = 3
    mode: int = FILE_MODE
    target: Optional[str] = None  # a symbolic link's


@dataclass
class BackupWriter:
    out_dir: Path
    device: Device
    backup_date: datetime  # aware
    container_version: str
    password: Optional[str] = None
    iterations: str = "fast"
    seed: int = 0
    host: Host = field(default_factory=Host)
    apps: list = field(default_factory=list)  # App, in the order persona.json lists them

    def __post_init__(self):
        self.rng = random.Random(self.seed)
        self.device.fill_identifiers(self.rng)
        self.shape = load_container_shape(self.container_version)
        self._entries: dict[tuple[str, str], _Entry] = {}

    @property
    def encrypted(self) -> bool:
        return self.password is not None

    def add_file(self, domain: str, relative_path: str, data: bytes, protection_class: int = 3) -> None:
        self._entries[(domain, relative_path)] = _Entry(domain, relative_path, FLAG_FILE, data, protection_class)
        self._add_parents(domain, relative_path)

    def has_row(self, domain: str, relative_path: str) -> bool:
        """Whether Manifest.db will have a row (file, folder or link) for this path."""
        return (domain, relative_path) in self._entries

    def add_directory(self, domain: str, relative_path: str) -> None:
        """A folder, empty or not (a file's folders are added with it)."""
        self._add_parents(domain, f"{relative_path}/x" if relative_path else "x")

    def add_symlink(self, domain: str, relative_path: str, target: str) -> None:
        """A symbolic link: a row with flags 4 whose MBFile has a Target, and no stored file."""
        self._entries[(domain, relative_path)] = _Entry(domain, relative_path, FLAG_SYMLINK, b"", 0, LINK_MODE, target)
        self._add_parents(domain, relative_path)

    def _add_parents(self, domain: str, relative_path: str) -> None:
        parts = PurePosixPath(relative_path).parts[:-1]
        for depth in range(len(parts) + 1):
            parent = "/".join(parts[:depth])
            key = (domain, parent)
            if key not in self._entries:
                mode, pclass = (ROOT_MODE, 0) if parent == "" else (DIR_MODE, 4)
                self._entries[key] = _Entry(domain, parent, FLAG_DIRECTORY, b"", pclass, mode)

    # ── records ──────────────────────────────────────────────────────────────

    def _mbfile(self, entry: _Entry, inode: int, stamp: int, wrapped_key: Optional[bytes], digest: Optional[bytes]) -> bytes:
        return mbfile_record(entry.relative_path, mode=entry.mode, size=len(entry.data),
                             protection_class=entry.protection_class, inode=inode, stamp=stamp,
                             wrapped_key=wrapped_key, digest=digest, target=entry.target)

    # ── writing ──────────────────────────────────────────────────────────────

    def finalize(self) -> Path:
        backup_dir = Path(self.out_dir) / self.device.udid
        backup_dir.mkdir(parents=True, exist_ok=False)
        keybag = Keybag(self.rng, self.shape["keybag"], self.password, self.iterations)
        stamp = int(self.backup_date.timestamp())
        inode = 100_000 + self.rng.randrange(1_000_000)

        manifest_db = backup_dir / "Manifest.db"
        conn = sqlite3.connect(manifest_db)
        for ddl in self.shape["manifest_db_ddl"]:
            conn.execute(ddl)
        for (domain, relative_path), entry in sorted(self._entries.items()):
            inode += 1
            fid = file_id(domain, relative_path)
            wrapped = digest = None
            if entry.flags == FLAG_FILE:
                data = entry.data
                if self.encrypted:
                    key = self.rng.randbytes(32)
                    data = encrypt_cbc(key, data)
                    wrapped = keybag.wrap_key(entry.protection_class, key)
                    digest = hashlib.sha1(data).digest()
                target = backup_dir / fid[:2] / fid
                target.parent.mkdir(exist_ok=True)
                target.write_bytes(data)
            conn.execute(
                "INSERT INTO Files (fileID, domain, relativePath, flags, file) VALUES (?, ?, ?, ?, ?)",
                (fid, domain, relative_path, entry.flags, self._mbfile(entry, inode, stamp, wrapped, digest)),
            )
        # Real Manifest.db files are WAL databases with user_version 2, shipped without -wal/-shm.
        pragmas = self.shape.get("manifest_db_pragmas", {"journal_mode": "wal", "user_version": 2})
        conn.execute(f"PRAGMA user_version = {int(pragmas['user_version'])}")
        conn.commit()
        if pragmas["journal_mode"] == "wal":
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()
        for suffix in ("-wal", "-shm"):
            sidecar = manifest_db.with_name(manifest_db.name + suffix)
            if sidecar.exists():
                sidecar.unlink()

        manifest_key = None
        if self.encrypted:
            key = self.rng.randbytes(32)
            manifest_db.write_bytes(encrypt_cbc(key, manifest_db.read_bytes()))
            manifest_key = keybag.wrap_key(self.shape.get("manifest_key_class") or 3, key)

        self._write_plists(backup_dir, keybag, manifest_key)
        return backup_dir

    def _write_plists(self, backup_dir: Path, keybag: Keybag, manifest_key: Optional[bytes]) -> None:
        d = self.device
        when = self.backup_date.astimezone(timezone.utc).replace(tzinfo=None)  # plists store UTC
        lockdown = {
            "BuildVersion": d.build,
            "DeviceName": d.name,
            "ProductType": d.product_type,
            "ProductVersion": d.ios,
            "SerialNumber": d.serial,
            "UniqueDeviceID": d.udid,
            **_LOCKDOWN_EXTRAS,
        }
        manifest = {
            "Applications": {app.bundle_id: self._manifest_app(app) for app in self.apps},
            "BackupKeyBag": keybag.blob,
            "Date": when,
            "IsEncrypted": self.encrypted,
            "Lockdown": {k: lockdown[k] for k in self.shape["lockdown_keys"] if k in lockdown},
            "SystemDomainsVersion": self.shape.get("system_domains_version") or "24.0",
            "Version": self.shape.get("manifest_version") or "10.0",
            "WasPasscodeSet": True,
        }
        if manifest_key is not None:
            manifest["ManifestKey"] = manifest_key
        info = self._pymobiledevice3_info() if self.host.kind == "pymobiledevice3" else self._finder_info(when)
        status = {
            "BackupState": "new",
            "Date": when,
            "IsFullBackup": False,
            "SnapshotState": "finished",
            "UUID": str(uuid.UUID(bytes=self.rng.randbytes(16))).upper(),
            "Version": self.shape["status_plist"].get("Version", "3.3"),
        }
        for name, payload, fmt in (
            ("Manifest.plist", manifest, plistlib.FMT_BINARY),
            ("Info.plist", info, plistlib.FMT_XML),
            ("Status.plist", status, plistlib.FMT_BINARY),
        ):
            (backup_dir / name).write_bytes(plistlib.dumps(payload, fmt=fmt))

    def _finder_info(self, when: datetime) -> dict:
        d = self.device
        info = {
            "Applications": self._store_apps(),
            "Build Version": d.build,
            "Device Name": d.name,
            "Display Name": d.name,
            "GUID": d.guid,
            "ICCID": d.iccid,
            "IMEI": d.imei,
            "Installed Applications": [app.bundle_id for app in self.apps],
            "Last Backup Date": when,
            "Phone Number": d.phone_number,
            "Product Name": d.product_name,
            "Product Type": d.product_type,
            "Product Version": d.ios,
            "Serial Number": d.serial,
            "Target Identifier": d.udid,
            "Target Type": "Device",
            "Unique Identifier": d.udid,
            "iTunes Files": {},
            "iTunes Settings": {},
        }
        info["macOS Version"] = self.host.macos_version
        info["macOS Build Version"] = self.host.macos_build
        return info

    def _pymobiledevice3_info(self) -> dict:
        """Info.plist as pymobiledevice3 writes it (see Host). The keys it writes only when the
        iPhone reports them: ICCID, IMEI and Phone Number are written when the phone has them;
        MEID (CDMA phones), iBooks Data 2 (an old Books file) and iTunes Settings are left out."""
        d = self.device
        info = {
            "iTunes Version": _ITUNES_VERSION,
            "iTunes Files": {},
            "Unique Identifier": d.udid.upper(),
            "Target Type": "Device",
            "Target Identifier": d.udid,
            "Serial Number": d.serial,
            "Product Version": d.ios,
            "Product Type": d.product_type,
            "Installed Applications": [app.bundle_id for app in self.apps],
            "GUID": bytes.fromhex(d.guid),
            "Display Name": d.name,
            "Device Name": d.name,
            "Build Version": d.build,
            "Applications": self._store_apps(),
        }
        for key, value in (("ICCID", d.iccid), ("IMEI", d.imei), ("Phone Number", d.phone_number)):
            if value:
                info[key] = value
        return info

    def _app_uuid(self, app: App) -> str:
        """The app's bundle container folder name: fixed by the phone and the app, so no random draw."""
        digest = hashlib.sha256(f"{self.device.udid}/{app.bundle_id}".encode("utf-8")).digest()
        return str(uuid.UUID(bytes=digest[:16], version=4)).upper()

    def _manifest_app(self, app: App) -> dict:
        """One app in Manifest.plist's "Applications", which the iPhone writes for every installed app."""
        return {
            "CFBundleIdentifier": app.bundle_id,
            "CFBundleVersion": app.version,
            "ContainerContentClass": "Data/Application",
            "Path": f"/private/var/containers/Bundle/Application/{self._app_uuid(app)}/{app.name}.app",
        }

    def _store_apps(self) -> dict:
        """Info.plist's "Applications": the App Store apps, each with its (here made-up) SINF,
        iTunesMetadata and icon, which the computer keeps so it can put the app back on a restore."""
        apps = {}
        for app in self.apps:
            if not app.store:
                continue
            metadata = {"itemName": app.name, "kind": "software", "softwareVersionBundleId": app.bundle_id,
                        "bundleVersion": app.version}
            apps[app.bundle_id] = {
                "ApplicationSINF": b"backupgen synthetic SINF " + hashlib.sha1(app.bundle_id.encode("utf-8")).digest(),
                "iTunesMetadata": plistlib.dumps(metadata, fmt=plistlib.FMT_BINARY),
                "PlaceholderIcon": _png(hashlib.sha1(app.bundle_id.encode("utf-8")).digest()[:3]),
            }
        return apps


def _png(rgb: bytes) -> bytes:
    """A 1 × 1 PNG of one colour, for an app's icon."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)  # 1 × 1, 8-bit RGB
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(b"\x00" + rgb)) + chunk(b"IEND", b"")
