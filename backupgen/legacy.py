"""Write a backup the way iOS 9 and earlier did: Manifest.mbdb, with files flat in the folder.

    python -m backupgen legacy --ios 9.3.5 --out build/
    python -m backupgen legacy --ios 6.1.6 --password test --out build/
    python -m backupgen legacy --ios 10.1.1 --password test --out build/

iOS 10.0 and 10.1 sit in between: Manifest.db (encrypted with ManifestKey)
and files at ``<fileID[:2]>/<fileID>``, as later, but the older keybag, with
no DPSL/DPIC; iOS 10.2 added those. Their databases are the iOS 9 ones plus
what iOS 10 added to sms.db (associated_message_type and the rest of the
tapback and effect columns) and CallHistory.storedata (ZSERVICE_PROVIDER, for
CallKit).

The rest of backupgen fills table definitions harvested from real iOS 13+
backups. No iOS 6-9 backup has been harvested, so the databases here come
from hand-written definitions of the tables and columns readers touch, after
the published layouts of those releases. They model what changed (sms.db had
no associated_message_type before iOS 10, and the oldest had no
chat.display_name, attachment.transfer_name or attributedBody either; call
history before iOS 8 is call_history.db with Unix dates; notes before iOS 9 are
HTML in notes.sqlite; iOS 6's Photos.sqlite declares ZGENERICASSET.ZUUID as a
BLOB, as a published schema dump from iOS 6.1.3 shows, where iOS 7 and later
declare it VARCHAR, so iOS 6 assets get their UUID as 16 raw bytes), not every
column of the real files, and which release dropped which sms.db column is
approximate.

Every backup holds the same small, fictional history: three contacts, three
conversations (one with a photo attachment), four calls, three photos in an
album, and two notes plus a deleted one. ``LEGACY_EXPECTED`` is what a correct
reader extracts from it, written beside the backup as expected.json.
Phone numbers are from ranges set aside for fiction (Ofcom 07700 900xxx and
020 7946 0xxx, US 555-01xx).
"""

from __future__ import annotations

import hashlib
import io
import json
import plistlib
import random
import sqlite3
import struct
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Optional

from backupgen.container import mbfile_record
from backupgen.crypto import aes_wrap, encrypt_cbc

MBDB_HEADER = b"mbdb\x05\x00"
# The Files table as in every Manifest.db (see schemas/ios-*/container.json).
MANIFEST_DB_DDL = """
    CREATE TABLE Files (fileID TEXT PRIMARY KEY, domain TEXT, relativePath TEXT, flags INTEGER, file BLOB);
    CREATE INDEX FilesDomainIdx ON Files(domain);
    CREATE INDEX FilesRelativePathIdx ON Files(relativePath);
    CREATE INDEX FilesFlagsIdx ON Files(flags);
    CREATE TABLE Properties (key TEXT PRIMARY KEY, value BLOB);
"""
FILE_MODE = 0o100644
DIR_MODE = 0o40755
MOBILE_UID = 501
COCOA_EPOCH = 978_307_200

_DEVICES = {
    6: ("iPhone5,2", "iPhone 5", "6.1.6", "10B500"),
    7: ("iPhone6,2", "iPhone 5s", "7.1.2", "11D257"),
    8: ("iPhone7,2", "iPhone 6", "8.4.1", "12H321"),
    9: ("iPhone7,2", "iPhone 6", "9.3.5", "13G36"),
    10: ("iPhone9,3", "iPhone 7", "10.1.1", "14B100"),
}


def file_id(domain: str, relative_path: str) -> str:
    return hashlib.sha1(f"{domain}-{relative_path}".encode("utf-8")).hexdigest()


def _utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


def _cocoa(moment: datetime) -> int:
    return int(moment.timestamp()) - COCOA_EPOCH


# ── the history every legacy backup holds ────────────────────────────────────

OWNER_NUMBER = "+44 7700 900001"
CONTACTS = [
    # first, last, organization, phones, emails
    ("Ada", "Brook", None, ["+44 7700 900123"], ["ada@example.com"]),
    ("Ben", "Carter", None, ["+1 (555) 010-0142"], []),
    (None, None, "Corner Café", ["+44 20 7946 0018"], []),
]
# chat identifier, service, display name (iOS 7+), handles, messages
# A message: (date, from me, text, attachment file name or None)
CONVERSATIONS = [
    ("+447700900123", "SMS", None, ["+447700900123"], [
        (_utc(2012, 11, 3, 9, 15), False, "Are we still on for Saturday?", None),
        (_utc(2012, 11, 3, 9, 17), True, "Yes! See you at 10 & bring the map.", None),
        (_utc(2012, 11, 3, 18, 2), False, "Perfect, thanks", None),
    ]),
    ("+15550100142", "iMessage", None, ["+15550100142"], [
        (_utc(2013, 2, 14, 20, 40), False, "Look what I found", "IMG_0042.JPG"),
        (_utc(2013, 2, 14, 20, 41), True, "Ha, that's brilliant", None),
    ]),
    ("chat784512390457812", "iMessage", "Weekend", ["+447700900123", "+15550100142"], [
        (_utc(2013, 3, 1, 12, 0), True, "Who's in for the walk?", None),
        (_utc(2013, 3, 1, 12, 5), False, "Me", None),
    ]),
]
# The handle that sent each received message in the group chat, by message index.
GROUP_SENDERS = {1: "+15550100142"}
# address, date, duration seconds, outgoing, facetime
CALLS = [
    ("+447700900123", _utc(2013, 1, 5, 17, 30), 125, False, False),
    ("+15550100142", _utc(2013, 1, 6, 8, 10), 42, True, False),
    ("+442079460018", _utc(2013, 1, 7, 12, 0), 0, False, False),
    ("ada@example.com", _utc(2013, 1, 8, 19, 45), 300, True, True),
]
# file name, date taken, (latitude, longitude) or None, album
PHOTOS = [
    ("IMG_0001.JPG", _utc(2012, 8, 18, 14, 22, 5), (51.5007, -0.1246), "Holiday"),
    ("IMG_0002.JPG", _utc(2012, 8, 19, 10, 3, 41), None, "Holiday"),
    ("IMG_0003.JPG", _utc(2013, 1, 1, 0, 1, 12), None, None),
]
# title, HTML body, created, modified, deleted
NOTES = [
    ("Shopping", "Shopping<div>Milk &amp; eggs</div><div><br></div><div>Bread</div>",
     _utc(2012, 6, 1, 8, 0), _utc(2012, 6, 2, 9, 30), False),
    ("Gate code", "Gate code<div>4711</div>", _utc(2013, 5, 4, 7, 0), _utc(2013, 5, 4, 7, 0), False),
    ("Old draft", "Old draft<div>gone</div>", _utc(2012, 1, 1, 0, 0), _utc(2012, 1, 1, 0, 0), True),
]


def legacy_expected(ios: str) -> dict:
    """What a correct reader extracts from a backup written by ``generate_legacy(ios=...)``."""
    major = int(ios.split(".")[0])
    names = {"+447700900123": "Ada Brook", "+15550100142": "Ben Carter",
             "+442079460018": "Corner Café", "ada@example.com": "Ada Brook"}
    conversations = []
    for identifier, service, display_name, handles, messages in CONVERSATIONS:
        conversations.append({
            "chat_identifier": identifier,
            "service": service,
            "display_name": display_name if major >= 7 and display_name else None,
            "participants": [names[h] for h in handles],
            "messages": [
                {"date": when.isoformat(), "is_from_me": from_me, "text": text,
                 "attachment": attachment}
                for when, from_me, text, attachment in messages
            ],
        })
    return {
        "ios": ios,
        "manifest_format": "db" if major >= 10 else "mbdb",
        "contacts": sorted(" ".join(p for p in (first, last) if p) or org
                           for first, last, org, _, _ in CONTACTS),
        "conversations": conversations,
        "calls": [
            {"address": address, "contact_name": names.get(address, address), "date": when.isoformat(),
             "duration": duration, "direction": "outgoing" if outgoing else "incoming",
             "status": "answered" if duration else "missed",
             # call_history.db (iOS 7 and earlier) doesn't say video or audio.
             "app": ("FaceTime" if major < 8 else "FaceTime Video") if facetime else "Phone"}
            for address, when, duration, outgoing, facetime in CALLS
        ],
        "photos": [
            {"filename": name, "relative_path": f"Media/DCIM/100APPLE/{name}", "date_created": when.isoformat(),
             "latitude": loc[0] if loc else None, "longitude": loc[1] if loc else None, "album": album,
             "uuid": str(uuid.UUID(int=pk)).upper()}
            for pk, (name, when, loc, album) in enumerate(PHOTOS, 1)
        ],
        "notes": [
            {"title": title, "created": created.isoformat(), "modified": modified.isoformat()}
            for title, _, created, modified, deleted in NOTES if not deleted
        ],
        "note_bodies": {"Shopping": "Shopping\nMilk & eggs\n\nBread", "Gate code": "Gate code\n4711"},
    }


# ── databases ────────────────────────────────────────────────────────────────

def _sqlite_bytes(script: str, rows: list) -> bytes:
    """Build a database from DDL plus (sql, params) inserts and return its bytes."""
    with tempfile.TemporaryDirectory(prefix="backupgen-legacy-") as tmp:
        path = Path(tmp) / "db.sqlite"
        conn = sqlite3.connect(path)
        conn.executescript(script)
        for sql, params in rows:
            conn.execute(sql, params)
        conn.commit()
        conn.close()
        return path.read_bytes()


def _sms_db(major: int) -> bytes:
    msg_extra = ""
    chat_extra = ""
    attachment_extra = ""
    if major >= 7:
        msg_extra += (", item_type INTEGER DEFAULT 0, other_handle INTEGER DEFAULT 0, group_title TEXT,"
                      " group_action_type INTEGER DEFAULT 0, share_status INTEGER, share_direction INTEGER,"
                      " is_expirable INTEGER DEFAULT 0, expire_state INTEGER DEFAULT 0,"
                      " message_action_type INTEGER DEFAULT 0, message_source INTEGER DEFAULT 0")
        chat_extra = ", display_name TEXT, group_id TEXT"
        attachment_extra = ", user_info BLOB, transfer_name TEXT, total_bytes INTEGER DEFAULT 0"
    if major >= 8:
        msg_extra += (", is_audio_message INTEGER DEFAULT 0, is_played INTEGER DEFAULT 0,"
                      " date_played INTEGER, attributedBody BLOB")
    if major >= 10:
        msg_extra += (", associated_message_guid TEXT, associated_message_type INTEGER DEFAULT 0,"
                      " balloon_bundle_id TEXT, payload_data BLOB, expressive_send_style_id TEXT,"
                      " associated_message_range_location INTEGER DEFAULT 0,"
                      " associated_message_range_length INTEGER DEFAULT 0,"
                      " time_expressive_send_played INTEGER DEFAULT 0, message_summary_info BLOB")
    script = f"""
        CREATE TABLE _SqliteDatabaseProperties (key TEXT, value TEXT, UNIQUE(key));
        CREATE TABLE handle (ROWID INTEGER PRIMARY KEY AUTOINCREMENT UNIQUE, id TEXT NOT NULL,
            country TEXT, service TEXT NOT NULL, uncanonicalized_id TEXT, UNIQUE (id, service));
        CREATE TABLE message (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, guid TEXT UNIQUE NOT NULL,
            text TEXT, replace INTEGER DEFAULT 0, service_center TEXT, handle_id INTEGER DEFAULT 0,
            subject TEXT, country TEXT, version INTEGER DEFAULT 0, type INTEGER DEFAULT 0,
            service TEXT, account TEXT, account_guid TEXT, error INTEGER DEFAULT 0, date INTEGER,
            date_read INTEGER, date_delivered INTEGER, is_delivered INTEGER DEFAULT 0,
            is_finished INTEGER DEFAULT 0, is_emote INTEGER DEFAULT 0, is_from_me INTEGER DEFAULT 0,
            is_empty INTEGER DEFAULT 0, is_delayed INTEGER DEFAULT 0, is_auto_reply INTEGER DEFAULT 0,
            is_prepared INTEGER DEFAULT 0, is_read INTEGER DEFAULT 0, is_system_message INTEGER DEFAULT 0,
            is_sent INTEGER DEFAULT 0, has_dd_results INTEGER DEFAULT 0, is_service_message INTEGER DEFAULT 0,
            is_forward INTEGER DEFAULT 0, was_downgraded INTEGER DEFAULT 0, is_archive INTEGER DEFAULT 0,
            cache_has_attachments INTEGER DEFAULT 0, cache_roomnames TEXT,
            was_data_detected INTEGER DEFAULT 0, was_deduplicated INTEGER DEFAULT 0{msg_extra});
        CREATE TABLE chat (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, guid TEXT UNIQUE NOT NULL,
            style INTEGER, state INTEGER, account_id TEXT, properties BLOB, chat_identifier TEXT,
            service_name TEXT, room_name TEXT, account_login TEXT, is_archived INTEGER DEFAULT 0,
            last_addressed_handle TEXT{chat_extra});
        CREATE TABLE attachment (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, guid TEXT UNIQUE NOT NULL,
            created_date INTEGER DEFAULT 0, start_date INTEGER DEFAULT 0, filename TEXT, uti TEXT,
            mime_type TEXT, transfer_state INTEGER DEFAULT 0, is_outgoing INTEGER DEFAULT 0{attachment_extra});
        CREATE TABLE chat_handle_join (chat_id INTEGER REFERENCES chat (ROWID) ON DELETE CASCADE,
            handle_id INTEGER REFERENCES handle (ROWID) ON DELETE CASCADE, UNIQUE(chat_id, handle_id));
        CREATE TABLE chat_message_join (chat_id INTEGER REFERENCES chat (ROWID) ON DELETE CASCADE,
            message_id INTEGER REFERENCES message (ROWID) ON DELETE CASCADE, PRIMARY KEY (chat_id, message_id));
        CREATE TABLE message_attachment_join (message_id INTEGER REFERENCES message (ROWID) ON DELETE CASCADE,
            attachment_id INTEGER REFERENCES attachment (ROWID) ON DELETE CASCADE, UNIQUE(message_id, attachment_id));
        CREATE INDEX message_idx_date ON message(date);
    """
    rows: list = []
    handles: dict = {}
    for _, service, _, chat_handles, _ in CONVERSATIONS:
        for h in chat_handles:
            if (h, service) not in handles:
                handles[(h, service)] = len(handles) + 1
                rows.append(("INSERT INTO handle (ROWID, id, country, service, uncanonicalized_id) VALUES (?, ?, ?, ?, ?)",
                             (handles[(h, service)], h, "gb" if h.startswith("+44") else "us", service, h)))
    message_id = attachment_id = 0
    for chat_id, (identifier, service, display_name, chat_handles, messages) in enumerate(CONVERSATIONS, 1):
        style = 43 if len(chat_handles) > 1 else 45
        cols = ["ROWID", "guid", "style", "state", "chat_identifier", "service_name", "account_login"]
        vals = [chat_id, f"{service};{'+' if style == 43 else '-'};{identifier}", style, 3, identifier, service,
                "E:sam@example.com" if service == "iMessage" else "P:" + OWNER_NUMBER]
        if major >= 7:
            cols.append("display_name")
            vals.append(display_name)
        rows.append((f"INSERT INTO chat ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", tuple(vals)))
        for h in chat_handles:
            rows.append(("INSERT INTO chat_handle_join VALUES (?, ?)", (chat_id, handles[(h, service)])))
        for index, (when, from_me, text, attachment) in enumerate(messages):
            message_id += 1
            sender = GROUP_SENDERS.get(index, chat_handles[0]) if len(chat_handles) > 1 else chat_handles[0]
            handle_id = 0 if from_me and len(chat_handles) > 1 else handles[(sender, service)]
            body = text if attachment is None else "￼" + (text or "")
            rows.append((
                "INSERT INTO message (ROWID, guid, text, handle_id, service, account, date, date_read,"
                " date_delivered, is_delivered, is_finished, is_from_me, is_read, is_sent,"
                " cache_has_attachments) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 1, ?, ?, ?, ?)",
                (message_id, str(uuid.UUID(int=message_id)).upper(), body, handle_id, service,
                 "e:sam@example.com", _cocoa(when), 0 if from_me else _cocoa(when) + 60,
                 _cocoa(when) + 2 if from_me else 0, int(from_me), int(not from_me), int(from_me),
                 int(attachment is not None)),
            ))
            rows.append(("INSERT INTO chat_message_join VALUES (?, ?)", (chat_id, message_id)))
            if attachment:
                attachment_id += 1
                cols = ["ROWID", "guid", "created_date", "start_date", "filename", "uti", "mime_type",
                        "transfer_state", "is_outgoing"]
                vals = [attachment_id, f"at_0_{attachment_id}", _cocoa(when), _cocoa(when),
                        f"~/{_attachment_path(attachment)}", "public.jpeg", "image/jpeg", 5, int(from_me)]
                if major >= 7:
                    cols += ["transfer_name", "total_bytes"]
                    vals += [attachment, len(_jpeg(attachment))]
                rows.append((f"INSERT INTO attachment ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                             tuple(vals)))
                rows.append(("INSERT INTO message_attachment_join VALUES (?, ?)", (message_id, attachment_id)))
    return _sqlite_bytes(script, rows)


def _attachment_path(name: str) -> str:
    digest = hashlib.sha1(name.encode()).hexdigest()
    return f"Library/SMS/Attachments/{digest[:2]}/{digest[2:4]}/{name}"


def _address_book() -> bytes:
    script = """
        CREATE TABLE ABPerson (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, First TEXT, Last TEXT, Middle TEXT,
            FirstPhonetic TEXT, MiddlePhonetic TEXT, LastPhonetic TEXT, Organization TEXT, Department TEXT,
            Note TEXT, Kind INTEGER, Birthday TEXT, JobTitle TEXT, Nickname TEXT, Prefix TEXT, Suffix TEXT,
            FirstSort TEXT, LastSort TEXT, CreationDate INTEGER, ModificationDate INTEGER,
            CompositeNameFallback TEXT, ExternalIdentifier TEXT, ExternalModificationTag TEXT,
            ExternalUUID TEXT, StoreID INTEGER, DisplayName TEXT, ExternalRepresentation BLOB,
            FirstSortSection TEXT, LastSortSection TEXT, FirstSortLanguageIndex INTEGER DEFAULT 2147483647,
            LastSortLanguageIndex INTEGER DEFAULT 2147483647, PersonLink INTEGER DEFAULT -1, ImageURI TEXT,
            IsPreferredName INTEGER DEFAULT 1);
        CREATE TABLE ABMultiValue (UID INTEGER PRIMARY KEY, record_id INTEGER, property INTEGER,
            identifier INTEGER, label INTEGER, value TEXT);
        CREATE TABLE ABMultiValueLabel (value TEXT, UNIQUE(value));
        INSERT INTO ABMultiValueLabel (rowid, value) VALUES (1, '_$!<Mobile>!$_'), (2, '_$!<Home>!$_'),
            (3, '_$!<Work>!$_');
    """
    rows: list = []
    uid = 0
    for person_id, (first, last, org, phones, emails) in enumerate(CONTACTS, 1):
        rows.append(("INSERT INTO ABPerson (ROWID, First, Last, Organization, Kind, CreationDate,"
                     " ModificationDate) VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (person_id, first, last, org, 1 if org and not first else 0,
                      _cocoa(_utc(2012, 1, 1)), _cocoa(_utc(2012, 1, 1)))))
        for prop, values, label in ((3, phones, 1), (4, emails, 2)):
            for identifier, value in enumerate(values):
                uid += 1
                rows.append(("INSERT INTO ABMultiValue VALUES (?, ?, ?, ?, ?, ?)",
                             (uid, person_id, prop, identifier, label, value)))
    return _sqlite_bytes(script, rows)


def _call_history_db() -> bytes:
    """call_history.db, iOS 7 and earlier: Unix dates, flags 4 = in, 5 = out, +16 = FaceTime."""
    script = """
        CREATE TABLE _SqliteDatabaseProperties (key TEXT, value TEXT, UNIQUE(key));
        CREATE TABLE call (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, address TEXT, date INTEGER,
            duration INTEGER, flags INTEGER, id INTEGER, name TEXT, country_code TEXT,
            network_code TEXT, read INTEGER, assisted INTEGER, face_time_data BLOB, originalAddress TEXT);
    """
    rows = [
        ("INSERT INTO call (address, date, duration, flags, id, name, country_code, network_code, read,"
         " assisted, originalAddress) VALUES (?, ?, ?, ?, -1, '', '234', '10', ?, 1, ?)",
         (address, int(when.timestamp()), duration, (16 if facetime else 0) | 4 | int(outgoing),
          1, address))
        for address, when, duration, outgoing, facetime in CALLS
    ]
    return _sqlite_bytes(script, rows)


def _call_history_storedata(major: int) -> bytes:
    """CallHistory.storedata, iOS 8 and later: Core Data, Apple dates; ZSERVICE_PROVIDER from iOS 10."""
    provider_column = ", ZSERVICE_PROVIDER VARCHAR" if major >= 10 else ""
    script = f"""
        CREATE TABLE ZCALLRECORD (Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER,
            ZANSWERED INTEGER, ZCALLTYPE INTEGER, ZDISCONNECTED_CAUSE INTEGER, ZFACE_TIME_DATA INTEGER,
            ZNUMBER_AVAILABILITY INTEGER, ZORIGINATED INTEGER, ZREAD INTEGER, ZDATE TIMESTAMP,
            ZDURATION FLOAT, ZADDRESS VARCHAR, ZDEVICE_ID VARCHAR, ZISO_COUNTRY_CODE VARCHAR,
            ZNAME VARCHAR, ZUNIQUE_ID VARCHAR{provider_column});
        CREATE TABLE Z_PRIMARYKEY (Z_ENT INTEGER PRIMARY KEY, Z_NAME VARCHAR, Z_SUPER INTEGER, Z_MAX INTEGER);
        CREATE TABLE Z_METADATA (Z_VERSION INTEGER PRIMARY KEY, Z_UUID VARCHAR(255), Z_PLIST BLOB);
        INSERT INTO Z_PRIMARYKEY VALUES (1, 'CallRecord', 0, 4);
    """
    rows: list = []
    for pk, (address, when, duration, outgoing, facetime) in enumerate(CALLS, 1):
        cols = ["Z_PK", "Z_ENT", "Z_OPT", "ZANSWERED", "ZCALLTYPE", "ZORIGINATED", "ZREAD", "ZDATE", "ZDURATION",
                "ZADDRESS", "ZISO_COUNTRY_CODE", "ZNAME", "ZUNIQUE_ID"]
        vals = [pk, 1, 1, int(bool(duration) and not outgoing), 8 if facetime else 1, int(outgoing), 1,
                _cocoa(when), float(duration), address.encode("utf-8"), "gb", "", str(uuid.UUID(int=pk)).upper()]
        if major >= 10:
            cols.append("ZSERVICE_PROVIDER")
            vals.append("com.apple.FaceTime" if facetime else "com.apple.Telephony")
        rows.append((f"INSERT INTO ZCALLRECORD ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                     tuple(vals)))
    return _sqlite_bytes(script, rows)


def _asset_uuid(pk: int, major: int):
    """An asset's ZUUID: 16 raw bytes on iOS 6 (a BLOB column there), the uppercase string later."""
    asset_uuid = uuid.UUID(int=pk)
    return asset_uuid.bytes if major < 7 else str(asset_uuid).upper()


def _photos_sqlite(major: int) -> bytes:
    asset_extra = ", ZFAVORITE INTEGER, ZHIDDEN INTEGER, ZTRASHEDSTATE INTEGER, ZTRASHEDDATE TIMESTAMP" \
        if major >= 8 else ""
    uuid_type = "BLOB" if major < 7 else "VARCHAR"
    script = f"""
        CREATE TABLE Z_PRIMARYKEY (Z_ENT INTEGER PRIMARY KEY, Z_NAME VARCHAR, Z_SUPER INTEGER, Z_MAX INTEGER);
        CREATE TABLE ZGENERICASSET (Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER,
            ZCOMPLETE INTEGER, ZHEIGHT INTEGER, ZKIND INTEGER, ZORIENTATION INTEGER, ZWIDTH INTEGER,
            ZDATECREATED TIMESTAMP, ZMODIFICATIONDATE TIMESTAMP, ZDURATION FLOAT, ZLATITUDE FLOAT,
            ZLONGITUDE FLOAT, ZDIRECTORY VARCHAR, ZFILENAME VARCHAR, ZUUID {uuid_type}{asset_extra});
        CREATE TABLE ZGENERICALBUM (Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER, ZKIND INTEGER,
            ZTITLE VARCHAR, ZUUID VARCHAR);
        CREATE TABLE Z_8ASSETS (Z_8ALBUMS INTEGER, Z_9ASSETS INTEGER, Z_FOK_9ASSETS INTEGER,
            PRIMARY KEY (Z_8ALBUMS, Z_9ASSETS));
        INSERT INTO Z_PRIMARYKEY VALUES (8, 'GenericAlbum', 0, 1), (9, 'GenericAsset', 0, {len(PHOTOS)});
    """
    rows: list = [("INSERT INTO ZGENERICALBUM VALUES (1, 8, 1, 2, 'Holiday', ?)", (str(uuid.UUID(int=99)).upper(),))]
    for pk, (name, when, loc, album) in enumerate(PHOTOS, 1):
        cols = ["Z_PK", "Z_ENT", "Z_OPT", "ZCOMPLETE", "ZHEIGHT", "ZKIND", "ZORIENTATION", "ZWIDTH",
                "ZDATECREATED", "ZMODIFICATIONDATE", "ZDURATION", "ZLATITUDE", "ZLONGITUDE", "ZDIRECTORY",
                "ZFILENAME", "ZUUID"]
        lat, lon = loc if loc else (-180.0, -180.0)
        vals = [pk, 9, 1, 1, 48, 0, 1, 64, _cocoa(when), _cocoa(when), 0.0, lat, lon, "DCIM/100APPLE", name,
                _asset_uuid(pk, major)]
        if major >= 8:
            cols += ["ZFAVORITE", "ZHIDDEN", "ZTRASHEDSTATE"]
            vals += [0, 0, 0]
        rows.append((f"INSERT INTO ZGENERICASSET ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                     tuple(vals)))
        if album:
            rows.append(("INSERT INTO Z_8ASSETS VALUES (1, ?, ?)", (pk, pk)))
    return _sqlite_bytes(script, rows)


def _notes_sqlite() -> bytes:
    script = """
        CREATE TABLE Z_PRIMARYKEY (Z_ENT INTEGER PRIMARY KEY, Z_NAME VARCHAR, Z_SUPER INTEGER, Z_MAX INTEGER);
        CREATE TABLE ZNOTE (Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER, ZCONTAINSCJK INTEGER,
            ZCONTENTTYPE INTEGER, ZDELETEDFLAG INTEGER, ZEXTERNALFLAGS INTEGER, ZEXTERNALSERVERINTID INTEGER,
            ZINTEGERID INTEGER, ZISBOOKKEEPINGENTRY INTEGER, ZBODY INTEGER, ZSTORE INTEGER,
            ZCREATIONDATE TIMESTAMP, ZMODIFICATIONDATE TIMESTAMP, ZAUTHOR VARCHAR, ZGUID VARCHAR,
            ZSERVERID VARCHAR, ZSUMMARY VARCHAR, ZTITLE VARCHAR, ZEXTERNALCONTENTREF BLOB,
            ZEXTERNALREPRESENTATION BLOB);
        CREATE TABLE ZNOTEBODY (Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER, ZOWNER INTEGER,
            ZCONTENT VARCHAR, ZEXTERNALCONTENTREF VARCHAR, ZEXTERNALREPRESENTATION BLOB);
    """
    rows: list = []
    for pk, (title, body, created, modified, deleted) in enumerate(NOTES, 1):
        summary = body.split("<div>")[1].split("</div>")[0] if "<div>" in body else ""
        rows.append(("INSERT INTO ZNOTE (Z_PK, Z_ENT, Z_OPT, ZCONTAINSCJK, ZCONTENTTYPE, ZDELETEDFLAG,"
                     " ZINTEGERID, ZISBOOKKEEPINGENTRY, ZBODY, ZSTORE, ZCREATIONDATE, ZMODIFICATIONDATE,"
                     " ZGUID, ZSUMMARY, ZTITLE) VALUES (?, 3, 1, 0, 0, ?, ?, 0, ?, 1, ?, ?, ?, ?, ?)",
                     (pk, int(deleted), pk, pk, _cocoa(created), _cocoa(modified),
                      str(uuid.UUID(int=pk)).upper(), summary, title)))
        rows.append(("INSERT INTO ZNOTEBODY VALUES (?, 1, 1, ?, ?, NULL, NULL)", (pk, pk, body)))
    return _sqlite_bytes(script, rows)


def _jpeg(name: str) -> bytes:
    """A small JPEG, a different color per file name."""
    from PIL import Image  # dev dependency (pyproject: dev extra)

    shade = hashlib.sha1(name.encode()).digest()
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), tuple(shade[:3])).save(buf, format="JPEG", quality=80)
    return buf.getvalue()


# ── the container ────────────────────────────────────────────────────────────

def _mbdb_string(value) -> bytes:
    if value is None or len(value) == 0:
        return b"\xff\xff"
    raw = value.encode("utf-8") if isinstance(value, str) else value
    return struct.pack(">H", len(raw)) + raw


def mbdb_record(domain: str, relative_path: str, *, mode: int, inode: int, size: int, stamp: int,
                data_hash: bytes = b"", encryption_key: bytes = b"", protection_class: int = 0,
                link_target: str = "", properties: Optional[dict] = None) -> bytes:
    """One Manifest.mbdb record (see ios_backup_core.mbdb for the layout)."""
    properties = properties or {}
    out = [
        struct.pack(">H", len(domain.encode())) + domain.encode(),
        struct.pack(">H", len(relative_path.encode())) + relative_path.encode(),
        _mbdb_string(link_target),
        _mbdb_string(data_hash),
        _mbdb_string(encryption_key),
        struct.pack(">HQIIIIIQBB", mode, inode, MOBILE_UID, MOBILE_UID, stamp, stamp, stamp, size,
                    protection_class, len(properties)),
    ]
    for name, value in properties.items():
        out.append(_mbdb_string(name) + _mbdb_string(value))
    return b"".join(out)


class _LegacyKeybag:
    """A keybag as iOS 10.1 and earlier wrote it: class keys wrapped with PBKDF2-SHA1(password, SALT, ITER) only."""

    def __init__(self, rng: random.Random, password: Optional[str]):
        self.salt = rng.randbytes(20)
        self.iter = 10_000
        self.class_keys = {clas: rng.randbytes(32) for clas in range(1, 12)}
        wrapping = (hashlib.pbkdf2_hmac("sha1", password.encode(), self.salt, self.iter, 32)
                    if password else rng.randbytes(32))

        def tlv(tag: bytes, value) -> bytes:
            if isinstance(value, int):
                value = struct.pack(">L", value)
            return tag + struct.pack(">L", len(value)) + value

        parts = [tlv(b"VERS", 3), tlv(b"TYPE", 1), tlv(b"UUID", rng.randbytes(16)), tlv(b"HMCK", rng.randbytes(40)),
                 tlv(b"WRAP", 0), tlv(b"SALT", self.salt), tlv(b"ITER", self.iter)]
        for clas, key in self.class_keys.items():
            parts += [tlv(b"UUID", rng.randbytes(16)), tlv(b"CLAS", clas), tlv(b"WRAP", 3 if clas != 6 else 1),
                      tlv(b"KTYP", 0), tlv(b"WPKY", aes_wrap(wrapping, key))]
        self.blob = b"".join(parts)

    def wrap_key(self, protection_class: int, key: bytes) -> bytes:
        return struct.pack("<l", protection_class) + aes_wrap(self.class_keys[protection_class], key)


@dataclass
class LegacyBackupWriter:
    out_dir: Path
    ios: str = "9.3.5"
    password: Optional[str] = None
    backup_date: datetime = field(default_factory=lambda: _utc(2016, 9, 10, 18, 30))
    seed: int = 0
    device_name: str = "Sam's iPhone"

    def __post_init__(self):
        version = tuple(int(part) for part in self.ios.split(".")[:2])
        if version >= (10, 2):
            raise ValueError(f"iOS {self.ios} backups have DPSL in their keybag; backupgen.generate writes those")
        self.rng = random.Random(self.seed)
        self.major = version[0]
        # iOS 10.0 and 10.1: Manifest.db, still with the older keybag.
        self.manifest_db = self.major >= 10
        self.product_type, self.product_name, _, self.build = _DEVICES.get(self.major, _DEVICES[9])
        self.udid = "".join(self.rng.choice("0123456789abcdef") for _ in range(40))
        self._entries: dict = {}

    def add_file(self, domain: str, relative_path: str, data: bytes, protection_class: int = 3,
                 properties: Optional[dict] = None) -> None:
        self._entries[(domain, relative_path)] = (FILE_MODE, data, protection_class, properties or {})
        parts = PurePosixPath(relative_path).parts[:-1]
        for depth in range(len(parts) + 1):
            key = (domain, "/".join(parts[:depth]))
            self._entries.setdefault(key, (DIR_MODE, b"", 0, {}))

    def finalize(self) -> Path:
        backup_dir = Path(self.out_dir) / self.udid
        backup_dir.mkdir(parents=True, exist_ok=False)
        keybag = _LegacyKeybag(self.rng, self.password)
        manifest_key = None
        if self.manifest_db:
            manifest_key = self._write_manifest_db(backup_dir, keybag)
        else:
            self._write_mbdb(backup_dir, keybag)
        self._write_plists(backup_dir, keybag, manifest_key)
        return backup_dir

    def _stored(self, keybag: _LegacyKeybag, data: bytes, pclass: int) -> tuple:
        """The bytes to store for a file, and its wrapped key (None when the backup isn't encrypted)."""
        if self.password is None:
            return data, None
        key = self.rng.randbytes(32)
        return encrypt_cbc(key, data), keybag.wrap_key(pclass, key)

    def _write_mbdb(self, backup_dir: Path, keybag: _LegacyKeybag) -> None:
        stamp = int(self.backup_date.timestamp())
        inode = 200_000
        records = [MBDB_HEADER]
        for (domain, relative_path), (mode, data, pclass, properties) in sorted(self._entries.items()):
            inode += 1
            wrapped = digest = b""
            if mode == FILE_MODE:
                stored, wrapped = self._stored(keybag, data, pclass)
                digest = hashlib.sha1(stored).digest()
                # Flat: <fileID>, no <fileID[:2]> folder.
                (backup_dir / file_id(domain, relative_path)).write_bytes(stored)
            records.append(mbdb_record(domain, relative_path, mode=mode, inode=inode, size=len(data), stamp=stamp,
                                       data_hash=digest, encryption_key=wrapped or b"", protection_class=pclass,
                                       properties=properties))
        (backup_dir / "Manifest.mbdb").write_bytes(b"".join(records))

    def _write_manifest_db(self, backup_dir: Path, keybag: _LegacyKeybag) -> Optional[bytes]:
        """iOS 10.0 and 10.1: Manifest.db and <fileID[:2]>/<fileID>. Returns the wrapped ManifestKey, if encrypted."""
        stamp = int(self.backup_date.timestamp())
        inode = 200_000
        manifest_db = backup_dir / "Manifest.db"
        conn = sqlite3.connect(manifest_db)
        conn.executescript(MANIFEST_DB_DDL)
        for (domain, relative_path), (mode, data, pclass, _) in sorted(self._entries.items()):
            inode += 1
            fid = file_id(domain, relative_path)
            wrapped = digest = None
            if mode == FILE_MODE:
                stored, wrapped = self._stored(keybag, data, pclass)
                if wrapped is not None:
                    digest = hashlib.sha1(stored).digest()
                target = backup_dir / fid[:2] / fid
                target.parent.mkdir(exist_ok=True)
                target.write_bytes(stored)
            record = mbfile_record(relative_path, mode=mode, size=len(data), protection_class=pclass, inode=inode,
                                   stamp=stamp, wrapped_key=wrapped, digest=digest)
            conn.execute("INSERT INTO Files (fileID, domain, relativePath, flags, file) VALUES (?, ?, ?, ?, ?)",
                         (fid, domain, relative_path, 1 if mode == FILE_MODE else 2, record))
        conn.commit()
        conn.close()
        if self.password is None:
            return None
        key = self.rng.randbytes(32)
        manifest_db.write_bytes(encrypt_cbc(key, manifest_db.read_bytes()))
        return keybag.wrap_key(3, key)

    def _write_plists(self, backup_dir: Path, keybag: _LegacyKeybag, manifest_key: Optional[bytes]) -> None:
        when = self.backup_date.astimezone(timezone.utc).replace(tzinfo=None)  # plists store UTC
        serial = "SYN" + "".join(self.rng.choice("0123456789ABCDEFGHJKLMNPQRSTUVWXYZ") for _ in range(9))
        lockdown = {"BuildVersion": self.build, "DeviceName": self.device_name, "ProductType": self.product_type,
                    "ProductVersion": self.ios, "SerialNumber": serial, "UniqueDeviceID": self.udid}
        manifest = {
            "Applications": {}, "BackupKeyBag": keybag.blob, "Date": when,
            "IsEncrypted": self.password is not None, "Lockdown": lockdown,
            "SystemDomainsVersion": f"{self.major + 11}.0", "Version": "10.0" if self.manifest_db else "9.1",
            "WasPasscodeSet": True,
        }
        if manifest_key is not None:
            manifest["ManifestKey"] = manifest_key
        info = {
            "Build Version": self.build, "Device Name": self.device_name, "Display Name": self.device_name,
            "GUID": "".join(self.rng.choice("0123456789ABCDEF") for _ in range(32)),
            "ICCID": "89990" + "".join(self.rng.choice("0123456789") for _ in range(14)),
            "IMEI": "00" + "".join(self.rng.choice("0123456789") for _ in range(13)),
            "Last Backup Date": when, "Phone Number": OWNER_NUMBER, "Product Name": self.product_name,
            "Product Type": self.product_type, "Product Version": self.ios, "Serial Number": serial,
            "Target Identifier": self.udid, "Target Type": "Device", "Unique Identifier": self.udid.upper(),
            "iTunes Files": {}, "iTunes Settings": {}, "iTunes Version": "12.5.1",
        }
        status = {"BackupState": "new", "Date": when, "IsFullBackup": False, "SnapshotState": "finished",
                  "UUID": str(uuid.UUID(bytes=self.rng.randbytes(16))).upper(),
                  "Version": "3.0" if self.manifest_db else "2.4"}
        for name, payload, fmt in (("Manifest.plist", manifest, plistlib.FMT_BINARY),
                                   ("Info.plist", info, plistlib.FMT_XML),
                                   ("Status.plist", status, plistlib.FMT_BINARY)):
            (backup_dir / name).write_bytes(plistlib.dumps(payload, fmt=fmt))


def generate_legacy(out_dir: Path, *, ios: str = "9.3.5", password: Optional[str] = None, seed: int = 0) -> Path:
    """Write the legacy backup for *ios* (6.x to 10.1) into *out_dir*, plus expected.json. Returns the backup folder."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    writer = LegacyBackupWriter(out_dir, ios=ios, password=password, seed=seed)
    major = writer.major
    writer.add_file("HomeDomain", "Library/SMS/sms.db", _sms_db(major))
    writer.add_file("HomeDomain", "Library/AddressBook/AddressBook.sqlitedb", _address_book())
    if major >= 8:
        writer.add_file("HomeDomain", "Library/CallHistoryDB/CallHistory.storedata", _call_history_storedata(major))
    else:
        writer.add_file("WirelessDomain", "Library/CallHistory/call_history.db", _call_history_db())
    writer.add_file("CameraRollDomain", "Media/PhotoData/Photos.sqlite", _photos_sqlite(major))
    for pk, (name, _, _, _) in enumerate(PHOTOS, 1):
        # Real records carry extended attributes like this one; readers must skip them.
        writer.add_file("CameraRollDomain", f"Media/DCIM/100APPLE/{name}", _jpeg(name),
                        properties={"com.apple.assetsd.UUID": str(uuid.UUID(int=pk)).upper().encode()})
    writer.add_file("HomeDomain", "Library/Notes/notes.sqlite", _notes_sqlite())
    for _, _, _, _, messages in CONVERSATIONS:
        for _, _, _, attachment in messages:
            if attachment:
                writer.add_file("MediaDomain", _attachment_path(attachment), _jpeg(attachment))
    backup_dir = writer.finalize()
    (out_dir / "expected.json").write_text(json.dumps(legacy_expected(ios), indent=2, ensure_ascii=False),
                                           encoding="utf-8")
    return backup_dir
