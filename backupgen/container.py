"""Write a backup folder: hashed files, Manifest.db, Manifest.plist, Info.plist, Status.plist.

Layout and record formats follow real iOS 13–17 backups (see ``schemas/ios-*/container.json``):
files live at ``<fileID[:2]>/<fileID>`` with ``fileID = SHA-1("<domain>-<relativePath>")``.
Each Manifest.db row carries an NSKeyedArchiver ``MBFile`` record. Directories,
including every domain root, get their own rows, as on a device.
"""

from __future__ import annotations

import hashlib
import json
import plistlib
import random
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Optional

from backupgen import SCHEMAS_DIR
from backupgen.crypto import Keybag, encrypt_cbc

FILE_MODE = 0o100644
DIR_MODE = 0o40700
ROOT_MODE = 0o40711
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
_CHIP_IDS = {
    "iPhone12,1": "00008030", "iPhone15,4": "00008120", "iPhone16,2": "00008130",
    "iPhone17,1": "00008140", "iPhone17,2": "00008140", "iPhone17,3": "00008140", "iPhone17,4": "00008140",
}


def file_id(domain: str, relative_path: str) -> str:
    return hashlib.sha1(f"{domain}-{relative_path}".encode("utf-8")).hexdigest()


def load_container_shape(version: str) -> dict:
    path = SCHEMAS_DIR / f"ios-{version}" / "container.json"
    return json.loads(path.read_text(encoding="utf-8"))


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


@dataclass
class Host:
    """The computer the backup was made on. Only Finder (macOS) for now."""

    kind: str = "finder"
    macos_version: str = "12.2.1"
    macos_build: str = "21D62"


@dataclass
class _Entry:
    domain: str
    relative_path: str
    flags: int  # 1 file, 2 directory
    data: bytes = b""
    protection_class: int = 3
    mode: int = FILE_MODE


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

    def __post_init__(self):
        self.rng = random.Random(self.seed)
        self.device.fill_identifiers(self.rng)
        self.shape = load_container_shape(self.container_version)
        self._entries: dict[tuple[str, str], _Entry] = {}

    @property
    def encrypted(self) -> bool:
        return self.password is not None

    def add_file(self, domain: str, relative_path: str, data: bytes, protection_class: int = 3) -> None:
        self._entries[(domain, relative_path)] = _Entry(domain, relative_path, 1, data, protection_class)
        parts = PurePosixPath(relative_path).parts[:-1]
        for depth in range(len(parts) + 1):
            parent = "/".join(parts[:depth])
            key = (domain, parent)
            if key not in self._entries:
                mode, pclass = (ROOT_MODE, 0) if parent == "" else (DIR_MODE, 4)
                self._entries[key] = _Entry(domain, parent, 2, b"", pclass, mode)

    # ── records ──────────────────────────────────────────────────────────────

    def _mbfile(self, entry: _Entry, inode: int, stamp: int, wrapped_key: Optional[bytes], digest: Optional[bytes]) -> bytes:
        top = {
            "LastModified": stamp,
            "Flags": 0,
            "GroupID": MOBILE_UID,
            "LastStatusChange": stamp,
            "RelativePath": plistlib.UID(2),
            "Birth": stamp,
            "Size": len(entry.data),
            "InodeNumber": inode,
            "Mode": entry.mode,
            "UserID": MOBILE_UID,
            "ProtectionClass": entry.protection_class,
        }
        objects: list = ["$null", top, entry.relative_path]
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
            if entry.flags == 1:
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
            "Applications": {},
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
        info = {
            "Applications": {},
            "Build Version": d.build,
            "Device Name": d.name,
            "Display Name": d.name,
            "GUID": d.guid,
            "ICCID": d.iccid,
            "IMEI": d.imei,
            "Installed Applications": [],
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
        if self.host.kind == "finder":
            info["macOS Version"] = self.host.macos_version
            info["macOS Build Version"] = self.host.macos_build
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
