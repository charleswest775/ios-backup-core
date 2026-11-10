"""
WhatsApp extraction from ChatStorage.sqlite.

WhatsApp keeps its chats in a Core Data store in its shared app-group container
(AppDomainGroup-group.net.whatsapp.WhatsApp.shared). The layout below was worked
out on five public test backups (WhatsApp 2.20.31 to 2.24.15, iOS 13.3 to 17.3)
and checked against their creator's activity logs; codes that never occur in
them come from public forensic references and are marked as such.

The things that are easy to get wrong:

- Order a chat by ZSORT (arrival order on the phone). ZMESSAGEDATE is the
  sender's clock and Z_PK is insertion order; both disagree with what the user
  saw.
- ZGROUPEVENTTYPE is an event code only when ZMESSAGETYPE is a system type
  (6, 10, 55). On ordinary messages it only marks direction.
- A text message with a ZWAMEDIAITEM row is a reply: the media row carries the
  quoted message in ZMETADATA, not an attachment.
- On image rows ZLATITUDE/ZLONGITUDE hold the picture's height/width, and
  ZVCARDSTRING its MIME type. They are coordinates only on location rows.
- From WhatsApp 2.23 on, ZWAMESSAGE.ZPUSHNAME and ZWACHATSESSION.ZLASTMESSAGETEXT
  hold base64 protobuf, not text. Never show them.
- Reactions are not rows: they sit in ZWAMESSAGEINFO.ZRECEIPTINFO of the message
  they react to. Calls (from WhatsApp 2.24) are type-59 rows whose details sit in
  ZMETADATA.
- ZWACHATSESSION.ZMESSAGECOUNTER is the next ZSORT, not a count.
- A message deleted "for me" leaves no row, only a gap in ZSORT.

Methods return plain dicts shaped like MessageExtractor's, with extra keys for
what WhatsApp has and Messages doesn't (quotes, reactions, calls, locations).
Ids are ChatStorage primary keys (Z_PK): chat_id, message_id and attachment_id
overlap with sms.db's ROWIDs, so a caller showing both must keep them apart.
Attachment filenames are backup-relative paths in SHARED_DOMAIN, not sms.db's
~/Library/SMS paths.
"""

import base64
import binascii
import json
import math
import mimetypes
import os
import plistlib
import re
import sqlite3
import sys
import traceback
import weakref
from datetime import datetime, timezone

from ios_backup_core.contacts import resolve_contact
from ios_backup_core.db import connect_readonly, get_database
from ios_backup_core.protobuf import (
    WIRE_LEN,
    ProtobufError,
    all_fields,
    field_int,
    field_text,
    parse_fields,
    sub_message,
)
from ios_backup_core.timestamps import apple_to_iso, iso_to_apple

SHARED_DOMAIN = "AppDomainGroup-group.net.whatsapp.WhatsApp.shared"
CHAT_DB = "ChatStorage.sqlite"
CONTACTS_DB = "ContactsV2.sqlite"
LID_DB = "LID.sqlite"
GROUP_PLIST = "Library/Preferences/group.net.whatsapp.WhatsApp.shared.plist"
MEDIA_PREFIX = "Message/"

UNREADABLE = (
    "WhatsApp is in this backup but could not be read. "
    "The backup may be incomplete or damaged."
)
UNSUPPORTED = (
    "This backup's WhatsApp data is in a layout this version can't read yet."
)

# Apple-epoch seconds outside this range are WhatsApp's "no date" sentinels
# (NSDate distantPast / distantFuture: years 1 and 4001), not real times.
_MAX_APPLE_SECONDS = 4_000_000_000

_PHONE_SERVERS = ("s.whatsapp.net", "c.us")
_PHONE_LIKE = re.compile(r"^[\d\s+().\-‎‏‪-‮]*$")
_MIME_LIKE = re.compile(r"^[a-z]+/[a-z0-9.+\-]+(\s*;.*)?$", re.IGNORECASE)
_VCARD_NAME_SEPARATOR = "_$!<Name-Separator>!$_"

# ZMESSAGETYPE values. 0, 1, 5, 6, 10 and 59 occur in the test backups; the rest
# follow IPED's ExtractorIOS and other public references (unverified here).
TEXT, IMAGE, VIDEO, AUDIO, CONTACT, LOCATION = 0, 1, 2, 3, 4, 5
GROUP_EVENT, LINK, DOCUMENT, SYSTEM, GIF = 6, 7, 8, 10, 11
DELETED, STICKER, DISAPPEARING_DEFAULT = 14, 15, 28
VIEW_ONCE_IMAGE, VIEW_ONCE_VIDEO, VIEW_ONCE_AUDIO = 38, 39, 53
POLL, ROUND_VIDEO, CHANNEL_EVENT, CALL = 46, 54, 55, 59

SYSTEM_TYPES = (GROUP_EVENT, SYSTEM, CHANNEL_EVENT, DISAPPEARING_DEFAULT)

_KIND_BY_TYPE = {
    TEXT: "text",
    IMAGE: "image",
    VIDEO: "video",
    AUDIO: "audio",
    CONTACT: "contact",
    # Not "location": in MessageExtractor's vocabulary that is a location-sharing notice.
    LOCATION: "shared_location",
    LINK: "link",
    DOCUMENT: "document",
    GIF: "gif",
    DELETED: "deleted",
    STICKER: "sticker",
    VIEW_ONCE_IMAGE: "view_once",
    VIEW_ONCE_VIDEO: "view_once",
    VIEW_ONCE_AUDIO: "view_once",
    POLL: "poll",
    ROUND_VIDEO: "video",
    CALL: "call",
}
# Types whose media row may own a file (the rest carry metadata only).
_MEDIA_TYPES = (IMAGE, VIDEO, AUDIO, DOCUMENT, GIF, STICKER,
                VIEW_ONCE_IMAGE, VIEW_ONCE_VIDEO, VIEW_ONCE_AUDIO, ROUND_VIDEO)
_DURATION_TYPES = (VIDEO, AUDIO, GIF, ROUND_VIDEO, VIEW_ONCE_VIDEO, VIEW_ONCE_AUDIO)
_MEDIA_LABELS = {
    IMAGE: "Photo", VIDEO: "Video", AUDIO: "Audio", DOCUMENT: "Document", GIF: "GIF", STICKER: "Sticker",
    VIEW_ONCE_IMAGE: "View-once photo", VIEW_ONCE_VIDEO: "View-once video",
    VIEW_ONCE_AUDIO: "View-once voice message", ROUND_VIDEO: "Video message",
}

# Wording for system rows. Type 10/2 and 6/12, 6/2 are verified on the test
# backups; the others follow IPED. Codes that public sources disagree on, and
# type 10/56 (IPED: "added to contacts", another source: "security code
# changed"), fall through to the neutral "WhatsApp notice".
_GROUP_EVENTS = {
    1: "The group name was changed",
    5: "The group photo was removed",
    7: "A member was removed",
    9: "A member changed their phone number",
    10: "You're now an admin",
    11: "You're no longer an admin",
    15: "A member joined using an invite link",
    17: "The group description was changed",
    22: "The group description was removed",
    23: "A member joined from an invitation",
    26: "The disappearing messages setting was changed",
    50: "Members were added",
}
_SYSTEM_EVENTS = {
    2: "Messages and calls are end-to-end encrypted.",
    3: "The security code changed",
    36: "The security code changed",
    34: "You blocked this contact",
    35: "You unblocked this contact",
}
# Type 10 missed-call notices (IPED): event code -> is_video.
_MISSED_CALL_EVENTS = {1: False, 45: False, 4: True, 46: True, 21: False, 22: True}

# CallLogMessage.callOutcome (WAProto). Only 0 occurs in the test backups.
_CALL_OUTCOMES = {
    0: "answered",
    1: "missed",
    2: "failed",
    3: "declined",
    4: "answered_elsewhere",
    5: "ongoing",
    6: "missed",   # silenced by Do Not Disturb
    7: "missed",   # silenced: unknown caller
}

# What a damaged or unexpected store can raise while being read. Each public
# method turns these into an "error" result instead of an exception.
_READ_ERRORS = (sqlite3.Error, ValueError, TypeError, OverflowError)

_EXTRA_MIME = {
    ".opus": "audio/ogg",
    ".thumb": "image/jpeg",
    ".webp": "image/webp",
    ".m4a": "audio/mp4",
    ".caf": "audio/x-caf",
}


# ── Small helpers ─────────────────────────────────────────────────────────────

def _iso(apple_seconds):
    """Apple-epoch seconds (int or real) to ISO 8601 UTC; None for missing or sentinel dates."""
    if apple_seconds is None:
        return None
    try:
        ts = float(apple_seconds)
    except (TypeError, ValueError, OverflowError):
        return None
    if not 0 < ts < _MAX_APPLE_SECONDS:
        return None
    return apple_to_iso(ts)


def _unix_ms_iso(ms):
    if not isinstance(ms, int) or ms <= 0:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _split_jid(jid) -> tuple[str, str]:
    """``"12025550123:7@s.whatsapp.net"`` -> ``("12025550123", "s.whatsapp.net")``."""
    if not jid or not isinstance(jid, str):
        return "", ""
    user, _, server = jid.strip().partition("@")
    return user.split(":")[0], server.lower()


def _bare_digits(value) -> str | None:
    """Digits of a LID.sqlite/ContactsV2 value, which may or may not carry a domain."""
    if value is None:
        return None
    user, _ = _split_jid(str(value))
    return user if user.isdigit() else None


def _is_status_jid(jid) -> bool:
    if not isinstance(jid, str):
        return False
    jid = jid.lower()
    return jid == "status@broadcast" or jid.endswith("@status")


def _phone_like(name) -> bool:
    return not name or bool(_PHONE_LIKE.match(name))


def _clean_name(value) -> str:
    """A display name from WhatsApp's own tables, without control or direction marks."""
    if not value or not isinstance(value, str):
        return ""
    return "".join(c for c in value if c.isprintable() or c == "‍").strip()


def _plain_push_name(value) -> str | None:
    """ZPUSHNAME when it is a name. From WhatsApp 2.23 it is a base64 protobuf instead."""
    if not value or not isinstance(value, str):
        return None
    try:
        raw = base64.b64decode(value, validate=True)
        if raw[:1] == b"\x08":
            parse_fields(raw)
            return None
    except (binascii.Error, ValueError):
        pass
    return _clean_name(value) or None


def _backup_rel(path) -> str | None:
    """A ZWAMEDIAITEM path ("Media/...") as a backup-relative path in the shared domain."""
    if not path or not isinstance(path, str):
        return None
    path = path.lstrip("/")
    return path if path.startswith(MEDIA_PREFIX) else MEDIA_PREFIX + path


def _guess_mime(path: str | None, declared) -> str | None:
    if isinstance(declared, str) and _MIME_LIKE.match(declared.strip()):
        return declared.strip()
    if not path:
        return None
    ext = os.path.splitext(path)[1].lower()
    return _EXTRA_MIME.get(ext) or mimetypes.guess_type("x" + ext)[0]


def _parse(blob):
    """Protobuf fields of a blob, or None when it is empty or not protobuf."""
    if not blob or not isinstance(blob, (bytes, bytearray, memoryview)):
        return None
    try:
        return parse_fields(bytes(blob))
    except ProtobufError:
        return None


def _float_or_none(value):
    """A finite float, or None (JSON has no NaN or Infinity)."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _int(value, default=None):
    """An int from a column that should hold one, or ``default`` when it holds something else."""
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _bounded(value, default: int) -> int:
    """A paging number: an int from 0 to 2**62 (SQLite's integers are 64-bit)."""
    return min(max(_int(value, default), 0), 2 ** 62)


def _text(value) -> str:
    """Free text (a caption, a document name) as stored, without surrounding whitespace."""
    return value.strip() if isinstance(value, str) else ""


def _decode_text(raw: bytes) -> str:
    """SQLite text_factory: one damaged value must not fail a whole query."""
    return raw.decode("utf-8", "replace")


def _duration_text(seconds) -> str:
    seconds = int(seconds or 0)
    if seconds < 60:
        return f"{seconds} s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}:{secs:02d}"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}"


def _call_text(call: dict) -> str:
    medium = "video" if call.get("is_video") else "voice"
    outcome = call.get("outcome")
    if call.get("is_video") is None and outcome == "unknown":
        return "WhatsApp call"
    if outcome == "missed":
        return f"Missed {medium} call"
    if outcome == "declined":
        return f"Declined {medium} call"
    if outcome == "failed":
        return f"Failed {medium} call"
    if outcome == "no_answer":
        return f"Unanswered {medium} call"
    direction = "Outgoing" if call.get("direction") == "outgoing" else "Incoming"
    text = f"{direction} {medium} call"
    if call.get("duration"):
        text += f" · {_duration_text(call['duration'])}"
    return text


class _Schema:
    """Which tables and columns this ChatStorage has, for building SELECT lists."""

    def __init__(self, conn: sqlite3.Connection):
        self.tables = {
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        self.columns: dict[str, set[str]] = {}
        for table in ("ZWAMESSAGE", "ZWACHATSESSION", "ZWAMEDIAITEM", "ZWAMESSAGEINFO",
                      "ZWAGROUPMEMBER", "ZWAGROUPINFO", "ZWAPROFILEPUSHNAME", "ZWAMESSAGEDATAITEM"):
            if table in self.tables:
                self.columns[table] = {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}

    def usable(self) -> bool:
        need = {
            "ZWAMESSAGE": {"Z_PK", "ZCHATSESSION", "ZMESSAGETYPE", "ZMESSAGEDATE"},
            "ZWACHATSESSION": {"Z_PK", "ZCONTACTJID"},
        }
        return all(cols <= self.columns.get(table, set()) for table, cols in need.items())

    def has(self, table: str, column: str) -> bool:
        return column in self.columns.get(table, set())

    def col(self, alias: str, table: str, column: str, as_name: str) -> str:
        if self.has(table, column):
            return f"{alias}.{column} AS {as_name}"
        return f"NULL AS {as_name}"

    @property
    def sort_key(self) -> str:
        return "ZSORT" if self.has("ZWAMESSAGE", "ZSORT") else "ZMESSAGEDATE"


_MESSAGE_COLUMNS = (
    "Z_PK", "ZCHATSESSION", "ZSORT", "ZMESSAGETYPE", "ZGROUPEVENTTYPE", "ZISFROMME",
    "ZMESSAGEDATE", "ZSENTDATE", "ZTEXT", "ZFROMJID", "ZTOJID", "ZGROUPMEMBER",
    "ZPUSHNAME", "ZSTANZAID", "ZMEDIAITEM", "ZMESSAGEINFO", "ZSTARRED",
)
_MEDIA_COLUMNS = (
    "Z_PK", "ZMEDIALOCALPATH", "ZXMPPTHUMBPATH", "ZTHUMBNAILLOCALPATH", "ZFILESIZE",
    "ZMOVIEDURATION", "ZTITLE", "ZVCARDNAME", "ZVCARDSTRING", "ZLATITUDE",
    "ZLONGITUDE", "ZMEDIAORIGIN", "ZMETADATA",
)


def _message_select(schema: _Schema) -> str:
    cols = [schema.col("m", "ZWAMESSAGE", c, f"m_{c}") for c in _MESSAGE_COLUMNS]
    joins = ""
    if "ZWAMEDIAITEM" in schema.tables and schema.has("ZWAMESSAGE", "ZMEDIAITEM"):
        cols += [schema.col("mi", "ZWAMEDIAITEM", c, f"mi_{c}") for c in _MEDIA_COLUMNS]
        joins += " LEFT JOIN ZWAMEDIAITEM mi ON mi.Z_PK = m.ZMEDIAITEM"
    else:
        cols += [f"NULL AS mi_{c}" for c in _MEDIA_COLUMNS]
    if ("ZWAMESSAGEINFO" in schema.tables and schema.has("ZWAMESSAGE", "ZMESSAGEINFO")
            and schema.has("ZWAMESSAGEINFO", "ZRECEIPTINFO")):
        cols.append("inf.ZRECEIPTINFO AS inf_ZRECEIPTINFO")
        joins += " LEFT JOIN ZWAMESSAGEINFO inf ON inf.Z_PK = m.ZMESSAGEINFO"
    else:
        cols.append("NULL AS inf_ZRECEIPTINFO")
    return f"SELECT {', '.join(cols)} FROM ZWAMESSAGE m{joins}"


class _Context:
    """Per-store facts used to name people and find files: loaded once per ChatStorage."""

    def __init__(self):
        self.owner = None              # owner's phone digits
        self.owner_name = ""
        self.owner_lids: set[str] = set()
        self.lid_to_phone: dict[str, str] = {}
        self.lid_names: dict[str, str] = {}
        self.contacts_v2: dict[str, str] = {}
        self.push_names: dict[str, str] = {}      # "pn:<digits>" / "lid:<digits>" -> push name
        self.partner_names: dict[str, str] = {}   # phone digits -> ZPARTNERNAME of 1:1 chats
        self.member_names: dict[str, str] = {}    # "pn:"/"lid:" -> ZWAGROUPMEMBER name
        self.members: dict[int, str] = {}         # ZWAGROUPMEMBER.Z_PK -> member JID
        self.group_creators: dict[int, str] = {}  # chat Z_PK -> creator JID
        self.media_paths: set[str] | None = None  # backup-relative Message/ paths


class WhatsAppExtractor:
    """Extracts WhatsApp chats from iOS backups."""

    def __init__(self):
        # Per accessor, the facts of the ChatStorage it holds. Keyed by the accessor
        # (a reopened backup is a new accessor) and by the file's size and mtime
        # (a backup made again in the same folder keeps its path).
        self._contexts: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()

    def clear_cache(self) -> None:
        """Forget what was read about every store (names, LIDs, the list of media files)."""
        self._contexts = weakref.WeakKeyDictionary()

    # ── Opening ────────────────────────────────────────────────────────────────

    def _open(self, backup):
        """``(conn, schema, db_path)``, or ``(None, None, fields)`` with the error/empty_reason to return."""
        try:
            db_path = get_database(backup, CHAT_DB, domain=SHARED_DOMAIN)
        except Exception as e:
            print(f"[whatsapp] could not extract {CHAT_DB}: {e}", file=sys.stderr, flush=True)
            db_path = None
        if not db_path:
            try:
                listed = backup.list_files(domain=SHARED_DOMAIN, path_like=CHAT_DB)
            except Exception as e:
                print(f"[whatsapp] could not list {CHAT_DB}: {e}", file=sys.stderr, flush=True)
                listed = []
            if listed:
                return None, None, {"error": UNREADABLE}
            return None, None, {"empty_reason": "not_in_backup"}
        try:
            conn = connect_readonly(db_path, backup)
        except sqlite3.Error as e:
            print(f"[whatsapp] could not open {CHAT_DB}: {e}", file=sys.stderr, flush=True)
            return None, None, {"error": UNREADABLE}
        try:
            conn.row_factory = sqlite3.Row
            conn.text_factory = _decode_text
            schema = _Schema(conn)
        except sqlite3.Error as e:
            conn.close()
            print(f"[whatsapp] could not read {CHAT_DB}: {e}", file=sys.stderr, flush=True)
            return None, None, {"error": UNREADABLE}
        if not schema.tables:  # an empty or zero-length file: not a WhatsApp store at all
            conn.close()
            return None, None, {"error": UNREADABLE}
        if not schema.usable():
            conn.close()
            return None, None, {"error": UNSUPPORTED, "empty_reason": "unsupported_format"}
        return conn, schema, db_path

    # ── Context (owner, names, files) ──────────────────────────────────────────

    def _context(self, backup, conn, schema, db_path) -> _Context:
        try:
            stat = os.stat(db_path)
            key = (db_path, stat.st_mtime_ns, stat.st_size)
        except OSError:
            key = (db_path, None, None)
        try:
            per_backup = self._contexts.setdefault(backup, {})
        except TypeError:  # an accessor that can't be weakly referenced: don't cache
            per_backup = {}
        ctx = per_backup.get(key)
        if ctx is not None:
            return ctx
        ctx = _Context()
        self._load_owner(backup, ctx)
        self._load_lids(backup, ctx)
        self._load_contacts_v2(backup, ctx)
        self._load_store_names(conn, schema, ctx)
        if not ctx.owner:
            ctx.owner = self._owner_from_rows(conn, schema)
        if ctx.owner:
            ctx.owner_lids.update(lid for lid, pn in ctx.lid_to_phone.items() if pn == ctx.owner)
        per_backup.clear()
        per_backup[key] = ctx
        return ctx

    def _load_owner(self, backup, ctx: _Context) -> None:
        try:
            path = backup.get_file(GROUP_PLIST, domain=SHARED_DOMAIN)
            if not path:
                return
            with open(path, "rb") as f:
                prefs = plistlib.load(f)
        except Exception as e:
            print(f"[whatsapp] could not read the app preferences: {e}", file=sys.stderr, flush=True)
            return
        if not isinstance(prefs, dict):
            return
        for key in ("OwnJabberID", "LastOwnJabberID", "OwnDeviceID"):
            user, server = _split_jid(prefs.get(key))
            if user.isdigit() and server in _PHONE_SERVERS:
                ctx.owner = user
                break
        for key in ("lid", "lidd"):
            user, server = _split_jid(prefs.get(key))
            if user.isdigit() and server == "lid":
                ctx.owner_lids.add(user)
        ctx.owner_name = _clean_name(prefs.get("FullUserName"))

    def _owner_from_rows(self, conn, schema: _Schema) -> str | None:
        """System notices are addressed to the owner (ZTOJID), in every version seen."""
        if not schema.has("ZWAMESSAGE", "ZTOJID") or not schema.has("ZWAMESSAGE", "ZISFROMME"):
            return None
        try:
            row = conn.execute(
                "SELECT ZTOJID, COUNT(*) AS n FROM ZWAMESSAGE "
                "WHERE ZISFROMME = 0 AND ZMESSAGETYPE IN (6, 10) AND ZTOJID LIKE '%@s.whatsapp.net' "
                "GROUP BY ZTOJID ORDER BY n DESC LIMIT 1"
            ).fetchone()
        except sqlite3.Error:
            return None
        if not row:
            return None
        user, _ = _split_jid(row[0])
        return user if user.isdigit() else None

    def _side_db(self, backup, name: str):
        try:
            path = get_database(backup, name, domain=SHARED_DOMAIN)
        except Exception as e:
            print(f"[whatsapp] could not extract {name}: {e}", file=sys.stderr, flush=True)
            return None
        if not path:
            return None
        try:
            conn = connect_readonly(path, backup)
            conn.row_factory = sqlite3.Row
            conn.text_factory = _decode_text
            return conn
        except sqlite3.Error as e:
            print(f"[whatsapp] could not open {name}: {e}", file=sys.stderr, flush=True)
            return None

    def _load_lids(self, backup, ctx: _Context) -> None:
        conn = self._side_db(backup, LID_DB)
        if conn is None:
            return
        try:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            if "ZWAPHONENUMBERLIDPAIR" in tables:
                cols = {r[1] for r in conn.execute("PRAGMA table_info(ZWAPHONENUMBERLIDPAIR)")}
                if {"ZLID", "ZPHONENUMBER"} <= cols:
                    order = "ORDER BY ZTIMESTAMP" if "ZTIMESTAMP" in cols else ""
                    for row in conn.execute(f"SELECT ZLID, ZPHONENUMBER FROM ZWAPHONENUMBERLIDPAIR {order}"):
                        lid, phone = _bare_digits(row[0]), _bare_digits(row[1])
                        if lid and phone:
                            ctx.lid_to_phone[lid] = phone  # the latest pairing wins
            if "ZWALIDPROPERTIES" in tables:
                cols = {r[1] for r in conn.execute("PRAGMA table_info(ZWALIDPROPERTIES)")}
                if {"ZLID", "ZDISPLAYNAME"} <= cols:
                    for row in conn.execute("SELECT ZLID, ZDISPLAYNAME FROM ZWALIDPROPERTIES"):
                        lid, name = _bare_digits(row[0]), _clean_name(row[1])
                        if lid and name:
                            ctx.lid_names[lid] = name
        except sqlite3.Error as e:
            print(f"[whatsapp] could not read {LID_DB}: {e}", file=sys.stderr, flush=True)
        finally:
            conn.close()

    def _load_contacts_v2(self, backup, ctx: _Context) -> None:
        conn = self._side_db(backup, CONTACTS_DB)
        if conn is None:
            return
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(ZWAADDRESSBOOKCONTACT)")}
            if {"ZWHATSAPPID", "ZFULLNAME"} <= cols:
                for row in conn.execute(
                    "SELECT ZWHATSAPPID, ZFULLNAME FROM ZWAADDRESSBOOKCONTACT WHERE ZWHATSAPPID IS NOT NULL"
                ):
                    digits, name = _bare_digits(row[0]), _clean_name(row[1])
                    if digits and name and not _phone_like(name):
                        ctx.contacts_v2.setdefault(digits, name)
        except sqlite3.Error as e:
            print(f"[whatsapp] could not read {CONTACTS_DB}: {e}", file=sys.stderr, flush=True)
        finally:
            conn.close()

    def _load_store_names(self, conn, schema: _Schema, ctx: _Context) -> None:
        def key_for(jid):
            user, server = _split_jid(jid)
            if not user.isdigit():
                return None
            if server in _PHONE_SERVERS:
                return f"pn:{user}"
            if server == "lid":
                return f"lid:{user}"
            return None

        def push_names():
            if schema.has("ZWAPROFILEPUSHNAME", "ZJID") and schema.has("ZWAPROFILEPUSHNAME", "ZPUSHNAME"):
                for row in conn.execute("SELECT ZJID, ZPUSHNAME FROM ZWAPROFILEPUSHNAME"):
                    key, name = key_for(row[0]), _clean_name(row[1])
                    if key and name:
                        ctx.push_names[key] = name

        def partner_names():
            if schema.has("ZWACHATSESSION", "ZPARTNERNAME"):
                type_filter = "WHERE ZSESSIONTYPE = 0" if schema.has("ZWACHATSESSION", "ZSESSIONTYPE") else ""
                for row in conn.execute(f"SELECT ZCONTACTJID, ZPARTNERNAME FROM ZWACHATSESSION {type_filter}"):
                    user, server = _split_jid(row[0])
                    name = _clean_name(row[1])
                    if user.isdigit() and server in _PHONE_SERVERS and name and not _phone_like(name):
                        ctx.partner_names[user] = name

        def group_members():
            if schema.has("ZWAGROUPMEMBER", "ZMEMBERJID"):
                name_cols = [c for c in ("ZCONTACTNAME", "ZFIRSTNAME") if schema.has("ZWAGROUPMEMBER", c)]
                select = ", ".join(["Z_PK", "ZMEMBERJID"] + name_cols)
                for row in conn.execute(f"SELECT {select} FROM ZWAGROUPMEMBER"):
                    ctx.members[row["Z_PK"]] = row["ZMEMBERJID"]
                    key = key_for(row["ZMEMBERJID"])
                    for c in name_cols:
                        name = _clean_name(row[c])
                        if key and name and not _phone_like(name):
                            ctx.member_names.setdefault(key, name)
                            break

        def group_creators():
            if schema.has("ZWAGROUPINFO", "ZCHATSESSION") and schema.has("ZWAGROUPINFO", "ZCREATORJID"):
                for row in conn.execute(
                    "SELECT ZCHATSESSION, ZCREATORJID FROM ZWAGROUPINFO WHERE ZCREATORJID IS NOT NULL"
                ):
                    ctx.group_creators[row[0]] = row[1]

        # One source failing (a damaged table) must not take the others with it.
        for source in (push_names, partner_names, group_members, group_creators):
            try:
                source()
            except sqlite3.Error as e:
                print(f"[whatsapp] could not read {source.__name__} from {CHAT_DB}: {e}",
                      file=sys.stderr, flush=True)

    def _media_paths(self, backup, ctx: _Context) -> set[str]:
        if ctx.media_paths is None:
            try:
                files = backup.list_files(domain=SHARED_DOMAIN, path_like=MEDIA_PREFIX + "%")
                ctx.media_paths = {f["path"] for f in files}
            except Exception as e:
                print(f"[whatsapp] could not list media files: {e}", file=sys.stderr, flush=True)
                ctx.media_paths = set()
        return ctx.media_paths

    # ── Names ──────────────────────────────────────────────────────────────────

    def _person(self, jid, ctx: _Context, contacts: dict, cache: dict) -> dict | None:
        """Name a person JID: ``{"name", "handle", "is_me"}``, or None for groups, lists and channels."""
        if not jid:
            return None
        if jid in cache:
            return cache[jid]
        user, server = _split_jid(jid)
        result = None
        lid = None  # a LID that maps to a phone number: names WhatsApp keyed by the LID still count
        if user.isdigit() and server == "lid":
            if user in ctx.owner_lids:
                result = {"name": "You", "handle": ("+" + ctx.owner) if ctx.owner else jid, "is_me": True}
            elif user in ctx.lid_to_phone:
                lid = user
                user, server = ctx.lid_to_phone[user], "s.whatsapp.net"
            else:
                name = (ctx.lid_names.get(user) or ctx.member_names.get(f"lid:{user}")
                        or ctx.push_names.get(f"lid:{user}") or "Unknown member")
                result = {"name": name, "handle": jid, "is_me": False}
        if result is None and user.isdigit() and server in _PHONE_SERVERS:
            handle = "+" + user
            if ctx.owner and user == ctx.owner:
                result = {"name": "You", "handle": handle, "is_me": True}
            else:
                name = (
                    resolve_contact(handle, contacts or {})
                    or ctx.contacts_v2.get(user)
                    or ctx.partner_names.get(user)
                    or ctx.member_names.get(f"pn:{user}")
                    or (lid and (ctx.member_names.get(f"lid:{lid}") or ctx.lid_names.get(lid)))
                    or ctx.push_names.get(f"pn:{user}")
                    or (lid and ctx.push_names.get(f"lid:{lid}"))
                    or handle
                )
                result = {"name": name, "handle": handle, "is_me": False}
        cache[jid] = result
        return result

    def _chat_info(self, row, ctx: _Context, contacts: dict, cache: dict) -> dict:
        jid = row["ZCONTACTJID"] or ""
        session_type = row["ZSESSIONTYPE"]
        _, server = _split_jid(jid)
        partner_name = _clean_name(row["ZPARTNERNAME"])
        if server == "g.us" or session_type == 1:
            chat_type, name = "group", partner_name or "WhatsApp group"
        elif server == "newsletter":
            chat_type, name = "channel", partner_name or "WhatsApp channel"
        elif server == "broadcast" or session_type == 2:
            chat_type, name = "broadcast", partner_name or "Broadcast list"
        else:
            chat_type = "individual"
            person = self._person(jid, ctx, contacts, cache)
            usable_partner = partner_name if not _phone_like(partner_name) else ""
            if person is None:
                name = usable_partner or jid or "WhatsApp chat"
            elif person["name"] == "Unknown member":  # a LID nothing maps to a number
                name = usable_partner or "Unknown contact"
            else:
                name = person["name"]
        return {"chat_id": row["Z_PK"], "jid": jid, "type": chat_type, "name": name}

    def _sender(self, row, chat: dict, ctx: _Context, contacts: dict, cache: dict) -> dict:
        """``{"name", "handle", "jid"}`` of an incoming message's author."""
        candidates = []
        member = row["m_ZGROUPMEMBER"]
        if member is not None and member in ctx.members:
            candidates.append(ctx.members[member])
        candidates.append(row["m_ZFROMJID"])
        if chat["type"] == "individual":
            candidates.append(chat["jid"])
        for jid in candidates:
            person = self._person(jid, ctx, contacts, cache)
            if person:
                return {"name": person["name"], "handle": person["handle"], "jid": jid}
        push = _plain_push_name(row["m_ZPUSHNAME"])
        if push:
            return {"name": push, "handle": "", "jid": row["m_ZFROMJID"] or ""}
        if chat["type"] in ("channel", "broadcast"):
            return {"name": chat["name"], "handle": "", "jid": chat["jid"]}
        return {"name": "Unknown member", "handle": "", "jid": row["m_ZFROMJID"] or ""}

    # ── Building messages ──────────────────────────────────────────────────────

    def _system_text(self, row, msg_type: int, event: int, chat: dict, ctx, contacts, cache) -> str:
        if msg_type == DISAPPEARING_DEFAULT:
            return "The disappearing messages setting was changed"
        if msg_type == CHANNEL_EVENT:
            return "Channel update"
        if msg_type == SYSTEM:
            return _SYSTEM_EVENTS.get(event, "WhatsApp notice")
        # Group events.
        if event == 12:
            creator = self._person(ctx.group_creators.get(chat["chat_id"]), ctx, contacts, cache)
            if creator:
                return "You created the group" if creator["is_me"] else f"{creator['name']} created the group"
            return "The group was created"
        if event == 2:
            who = self._event_member(row, ctx, contacts, cache)
            if who:
                return "You were added" if who["is_me"] else f"{who['name']} was added"
            if row["m_ZGROUPMEMBER"] is None and not _text(row["m_ZTEXT"]):
                # No member named: the notice is about its addressee (always the owner in
                # the test backups, in groups someone else created).
                addressee = self._person(row["m_ZTOJID"], ctx, contacts, cache)
                if addressee and addressee["is_me"]:
                    return "You were added"
            return "A member was added"
        if event in (1, 56):
            subject = self._new_subject(row["m_ZTEXT"])
            if subject:
                return f"The group name was changed to “{subject}”"
            return "The group name was changed"
        return _GROUP_EVENTS.get(event, "The group was updated")

    def _event_member(self, row, ctx, contacts, cache):
        member = row["m_ZGROUPMEMBER"]
        if member is not None and member in ctx.members:
            return self._person(ctx.members[member], ctx, contacts, cache)
        text = row["m_ZTEXT"]
        if isinstance(text, str) and "@" in text and " " not in text.strip():
            return self._person(text.strip(), ctx, contacts, cache)
        return None

    @staticmethod
    def _new_subject(text) -> str | None:
        if not isinstance(text, str) or not text.strip():
            return None
        text = text.strip()
        if text.startswith("{"):
            try:
                data = json.loads(text)
            except ValueError:
                return None
            subject = data.get("subject") if isinstance(data, dict) else None
            return _clean_name(subject) or None
        if "@" in text and " " not in text:
            return None  # a JID, not a name
        return _clean_name(text) or None

    def _call(self, meta, from_me: bool) -> dict:
        call_log = sub_message(sub_message(meta, 87) or [], 1) if meta else None
        if not call_log:
            return {"is_video": None, "duration": None, "outcome": "unknown",
                    "direction": "outgoing" if from_me else "incoming"}
        code = field_int(call_log, 2) or 0
        duration = field_int(call_log, 3)
        outcome = _CALL_OUTCOMES.get(code, "unknown")
        if outcome == "answered" and not duration:
            outcome = "no_answer" if from_me else "missed"
        return {
            "is_video": bool(field_int(call_log, 1)),
            "duration": duration,
            "outcome": outcome,
            "direction": "outgoing" if from_me else "incoming",
        }

    def _reactions(self, receipt_blob, ctx, contacts, cache) -> list[dict]:
        fields = _parse(receipt_blob)
        if not fields:
            return []
        block = sub_message(fields, 7)
        if not block:
            return []
        reactions = []
        for raw in all_fields(block, 1, WIRE_LEN):
            entry = _parse(raw)
            if not entry:
                continue
            emoji = field_text(entry, 3)
            if not emoji:
                continue  # an empty emoji is a withdrawn reaction
            jid = field_text(entry, 2)
            person = self._person(jid, ctx, contacts, cache) if jid else None
            reactions.append({
                "emoji": emoji,
                "sender": person["name"] if person else "Unknown member",
                "sender_handle": person["handle"] if person else (jid or ""),
                "is_from_me": bool(person and person["is_me"]),
                "date": _unix_ms_iso(field_int(entry, 4)),
            })
        for raw in all_fields(block, 2, WIRE_LEN):
            entry = _parse(raw)
            if not entry:
                continue
            emoji = field_text(entry, 2)
            if not emoji:
                continue
            reactions.append({
                "emoji": emoji,
                "sender": "You",
                "sender_handle": ("+" + ctx.owner) if ctx.owner else "",
                "is_from_me": True,
                "date": _unix_ms_iso(field_int(entry, 3)),
            })
        reactions.sort(key=lambda r: r["date"] or "")
        return reactions

    def _quote(self, meta, chat: dict, quoted_rows: dict, ctx, contacts, cache) -> dict | None:
        if not meta:
            return None
        stanza = field_text(meta, 5)
        content = sub_message(meta, 19)
        if not stanza and content is None:
            return None
        target = quoted_rows.get(stanza) if stanza else None
        if target is not None:
            t = target["ZMESSAGETYPE"]
            if target["ZISFROMME"]:
                sender, from_me = "You", True
            else:
                fake = {"m_ZGROUPMEMBER": target["ZGROUPMEMBER"], "m_ZFROMJID": target["ZFROMJID"],
                        "m_ZPUSHNAME": None}
                sender, from_me = self._sender(fake, chat, ctx, contacts, cache)["name"], False
            text = target["ZTEXT"] if t not in SYSTEM_TYPES and isinstance(target["ZTEXT"], str) else ""
            if not text and t in _MEDIA_TYPES:
                text = _text(target["ZCAPTION"])
            return {
                "message_id": target["Z_PK"],
                "text": text or "",
                "message_type": _KIND_BY_TYPE.get(t, "unknown"),
                "is_from_me": from_me,
                "sender": sender,
            }
        # The quoted message is gone (deleted, or before this phone's history): use the copy.
        text, kind = "", "text"
        if content is not None:
            text = field_text(content, 1) or ""
            if not text:
                # WAProto Message: 3 image (3 caption), 9 video (7 caption), 7 document
                # (8 file name), 6 link (1 text); 8 audio, 4 contact, 5 location.
                for number, label, text_field in ((3, "image", 3), (9, "video", 7), (8, "audio", None),
                                                  (7, "document", 8), (4, "contact", None),
                                                  (5, "shared_location", None), (6, "link", 1)):
                    part = sub_message(content, number)
                    if part is not None:
                        kind = label
                        text = (field_text(part, text_field) or "") if text_field else ""
                        break
        sender_jid = field_text(meta, 6)
        if sender_jid:
            person = self._person(sender_jid, ctx, contacts, cache)
            sender, from_me = (person["name"], person["is_me"]) if person else ("Unknown member", False)
        else:
            sender, from_me = "You", True
        return {"message_id": None, "text": text, "message_type": kind, "is_from_me": from_me, "sender": sender}

    def _attachment(self, row, msg_type: int, backup, ctx) -> dict | None:
        media_pk = row["mi_Z_PK"]
        if media_pk is None:
            return None
        rel = _backup_rel(row["mi_ZMEDIALOCALPATH"])
        thumb = _backup_rel(row["mi_ZXMPPTHUMBPATH"] or row["mi_ZTHUMBNAILLOCALPATH"])
        if rel is None and msg_type not in _MEDIA_TYPES:
            return None
        paths = self._media_paths(backup, ctx)
        declared = row["mi_ZVCARDSTRING"] if msg_type not in (CONTACT, DELETED, LOCATION) else None
        title = _text(row["mi_ZTITLE"])
        if msg_type == DOCUMENT and title:
            name = title
        elif rel:
            name = os.path.basename(rel)
        else:  # never downloaded, or left out of the backup
            name = _MEDIA_LABELS.get(msg_type, "Attachment")
        attachment = {
            "attachment_id": media_pk,
            "filename": rel or "",
            "mime_type": _guess_mime(rel, declared),
            "transfer_name": name,
            "total_bytes": _int(row["mi_ZFILESIZE"]) or None,
            "thumbnail_path": thumb if thumb and thumb in paths else None,
            "width": None,
            "height": None,
            "duration": None,
            "available": bool(rel and rel in paths),
        }
        if msg_type == IMAGE:
            height, width = _float_or_none(row["mi_ZLATITUDE"]), _float_or_none(row["mi_ZLONGITUDE"])
            if height and width and height > 0 and width > 0:
                attachment["width"], attachment["height"] = int(width), int(height)
        if msg_type in _DURATION_TYPES:
            duration = _float_or_none(row["mi_ZMOVIEDURATION"])
            if duration and duration > 0:
                attachment["duration"] = duration
        return attachment

    def _location(self, row, backup, ctx) -> dict:
        thumb = _backup_rel(row["mi_ZXMPPTHUMBPATH"] or row["mi_ZTHUMBNAILLOCALPATH"])
        lat, lon = _float_or_none(row["mi_ZLATITUDE"]), _float_or_none(row["mi_ZLONGITUDE"])
        if lat == 0 and lon == 0:
            lat = lon = None
        thumb = thumb if thumb and thumb in self._media_paths(backup, ctx) else None
        return {
            "latitude": lat,
            "longitude": lon,
            "live": _int(row["mi_ZMEDIAORIGIN"]) == 1,
            "place_name": _text(row["mi_ZTITLE"]) or None,
            "thumbnail_path": thumb,
            "attachment_id": row["mi_Z_PK"],  # get_attachment(id, thumbnail=True) serves the map preview
        }

    def _build(self, row, chat, backup, ctx, contacts, cache, quoted_rows, link_previews) -> dict:
        msg_type = _int(row["m_ZMESSAGETYPE"], -1)
        event = _int(row["m_ZGROUPEVENTTYPE"], 0)
        from_me = bool(row["m_ZISFROMME"])
        meta = _parse(row["mi_ZMETADATA"])
        text = row["m_ZTEXT"] if isinstance(row["m_ZTEXT"], str) else ""
        message = {
            "message_id": row["m_Z_PK"],
            "text": text,
            "message_type": _KIND_BY_TYPE.get(msg_type, "unknown"),
            "event_text": None,
            "link_preview": None,
            "date": _iso(row["m_ZMESSAGEDATE"]),
            "is_from_me": from_me,
            "sender": "me",
            "sender_handle": ("+" + ctx.owner) if ctx.owner else "",
            "has_attachments": False,
            "attachments": [],
            "is_reaction": False,
            "sort": row["m_ZSORT"],
            "sender_jid": "",
            "quoted": None,
            "reactions": self._reactions(row["inf_ZRECEIPTINFO"], ctx, contacts, cache),
            "call": None,
            "location": None,
            "received_date": None,
            "event_code": None,
            "starred": bool(row["m_ZSTARRED"]),
            "vcard": None,
        }

        missed_call_notice = msg_type == SYSTEM and event in _MISSED_CALL_EVENTS
        if msg_type in SYSTEM_TYPES and not missed_call_notice:
            message.update({
                "message_type": "system",
                "text": "",
                "event_text": self._system_text(row, msg_type, event, chat, ctx, contacts, cache),
                "event_code": f"{msg_type}/{event}",
                "is_from_me": False,
                "sender": "",
                "sender_handle": "",
                "reactions": [],
            })
            return message

        if not from_me:
            sender = self._sender(row, chat, ctx, contacts, cache)
            message.update(sender=sender["name"], sender_handle=sender["handle"], sender_jid=sender["jid"])
            message["received_date"] = _iso(row["m_ZSENTDATE"])

        if msg_type == SYSTEM:  # a missed-call notice (IPED codes; not in the test backups)
            message.update(message_type="call", text="", event_code=f"{msg_type}/{event}")
            call = {"is_video": _MISSED_CALL_EVENTS[event], "duration": 0, "outcome": "missed",
                    "direction": "incoming"}
            message.update(call=call, event_text=_call_text(call))
            return message

        if msg_type == CALL:
            call = self._call(meta, from_me)
            message.update(text="", call=call, event_text=_call_text(call))
            return message

        if msg_type == LOCATION:
            location = self._location(row, backup, ctx)
            if location["live"]:
                message.update(message_type="live_location", event_text="Live location")
            else:
                place = location["place_name"]
                message["event_text"] = f"Location: {place}" if place else "Location"
            message["location"] = location
            return message

        if msg_type == DELETED:
            message.update(text="", event_text="You deleted this message" if from_me else "This message was deleted")
            return message

        if msg_type in (VIEW_ONCE_IMAGE, VIEW_ONCE_VIDEO, VIEW_ONCE_AUDIO):
            what = {VIEW_ONCE_IMAGE: "photo", VIEW_ONCE_VIDEO: "video", VIEW_ONCE_AUDIO: "voice message"}[msg_type]
            message["event_text"] = f"View-once {what}"

        if msg_type == CONTACT:
            names = row["mi_ZVCARDNAME"] if isinstance(row["mi_ZVCARDNAME"], str) else ""
            message["text"] = text or ", ".join(
                n for n in (_clean_name(x) for x in names.split(_VCARD_NAME_SEPARATOR)) if n)
            vcard = row["mi_ZVCARDSTRING"]
            message["vcard"] = vcard if isinstance(vcard, str) and "BEGIN:VCARD" in vcard.upper() else None
            if not message["text"]:
                message["event_text"] = "Contact card"

        if msg_type == POLL:
            message["event_text"] = "Poll"

        if msg_type in (TEXT, LINK):
            message["quoted"] = self._quote(meta, chat, quoted_rows, ctx, contacts, cache)
            preview = link_previews.get(row["m_Z_PK"])
            if preview:
                message["link_preview"] = preview
                if msg_type == TEXT:
                    message["message_type"] = "link"

        attachment = self._attachment(row, msg_type, backup, ctx)
        if attachment:
            message["attachments"] = [attachment]
            message["has_attachments"] = True
            if not text and msg_type in _MEDIA_TYPES:
                message["text"] = _text(row["mi_ZTITLE"]) if msg_type != DOCUMENT else ""
            if msg_type not in _KIND_BY_TYPE:
                mime = attachment["mime_type"] or ""
                major = mime.split("/")[0]
                message["message_type"] = {"image": "image", "video": "video", "audio": "audio"}.get(
                    major, "document" if mime else "unknown")
        if not message["text"] and not message["attachments"] and not message["event_text"]:
            # Never leave an empty bubble: say what the message was.
            if message["message_type"] == "unknown":
                message["event_text"] = "Unsupported message"
            elif message["message_type"] != "text":
                message["event_text"] = self.preview_text(message)
        return message

    def _quoted_rows(self, conn, schema: _Schema, chat_id: int, rows) -> dict:
        stanzas = set()
        for row in rows:
            meta = _parse(row["mi_ZMETADATA"])
            stanza = field_text(meta, 5) if meta else None
            if stanza:
                stanzas.add(stanza)
        if not stanzas or not schema.has("ZWAMESSAGE", "ZSTANZAID"):
            return {}
        cols = ["q.Z_PK AS Z_PK", "q.ZSTANZAID AS ZSTANZAID", "q.ZMESSAGETYPE AS ZMESSAGETYPE"]
        cols += [f"q.{c} AS {c}" if schema.has("ZWAMESSAGE", c) else f"NULL AS {c}"
                 for c in ("ZTEXT", "ZISFROMME", "ZFROMJID", "ZGROUPMEMBER")]
        join = ""
        if schema.has("ZWAMESSAGE", "ZMEDIAITEM") and schema.has("ZWAMEDIAITEM", "ZTITLE"):
            cols.append("qm.ZTITLE AS ZCAPTION")
            join = " LEFT JOIN ZWAMEDIAITEM qm ON qm.Z_PK = q.ZMEDIAITEM"
        else:
            cols.append("NULL AS ZCAPTION")
        found = {}
        stanza_list = sorted(stanzas)
        for i in range(0, len(stanza_list), 500):
            chunk = stanza_list[i:i + 500]
            marks = ",".join("?" * len(chunk))
            for r in conn.execute(
                f"SELECT {', '.join(cols)} FROM ZWAMESSAGE q{join} "
                f"WHERE q.ZCHATSESSION = ? AND q.ZSTANZAID IN ({marks})",
                [chat_id, *chunk],
            ):
                found.setdefault(r["ZSTANZAID"], r)
        return found

    def _link_previews(self, conn, schema: _Schema, message_ids) -> dict:
        table = "ZWAMESSAGEDATAITEM"
        if not message_ids or not schema.has(table, "ZMESSAGE"):
            return {}
        wanted = [c for c in ("ZMATCHEDTEXT", "ZTITLE", "ZSUMMARY") if schema.has(table, c)]
        if not wanted:
            return {}
        previews = {}
        ids = list(message_ids)
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            marks = ",".join("?" * len(chunk))
            for r in conn.execute(
                f"SELECT ZMESSAGE, {', '.join(wanted)} FROM {table} WHERE ZMESSAGE IN ({marks})", chunk
            ):
                preview = {}
                for col, key in (("ZMATCHEDTEXT", "url"), ("ZTITLE", "title"), ("ZSUMMARY", "summary")):
                    if col in wanted and isinstance(r[col], str) and r[col].strip():
                        preview[key] = r[col].strip()
                if preview.get("title") or preview.get("summary"):
                    previews.setdefault(r["ZMESSAGE"], preview)
        return previews

    # ── Chats ──────────────────────────────────────────────────────────────────

    def _chat_rows(self, conn, schema: _Schema):
        cols = ["s.Z_PK AS Z_PK", "s.ZCONTACTJID AS ZCONTACTJID"]
        cols += [schema.col("s", "ZWACHATSESSION", c, c) for c in ("ZPARTNERNAME", "ZSESSIONTYPE", "ZARCHIVED")]
        rows = conn.execute(f"SELECT {', '.join(cols)} FROM ZWACHATSESSION s").fetchall()
        return [r for r in rows if not (_is_status_jid(r["ZCONTACTJID"]) or r["ZSESSIONTYPE"] == 3)]

    def _chat_by_id(self, conn, schema, chat_id):
        for row in self._chat_rows(conn, schema):
            if row["Z_PK"] == chat_id:
                return row
        return None

    def has_whatsapp(self, backup) -> dict:
        """Quick probe for the sidebar: is there at least one WhatsApp chat with messages?"""
        conn, schema, info = self._open(backup)
        if conn is None:
            return {"has_whatsapp": False, "chats": 0, "messages": 0, **info}
        try:
            listed = {r["Z_PK"] for r in self._chat_rows(conn, schema)}
            counts = conn.execute(
                "SELECT ZCHATSESSION, COUNT(*) FROM ZWAMESSAGE GROUP BY ZCHATSESSION"
            ).fetchall()
            chats = [(c, n) for c, n in counts if c in listed]
            messages = sum(n for _, n in chats)
            return {"has_whatsapp": messages > 0, "chats": len(chats), "messages": messages}
        except _READ_ERRORS as e:
            print(f"[whatsapp] probe failed: {e}", file=sys.stderr, flush=True)
            return {"has_whatsapp": False, "chats": 0, "messages": 0, "error": UNREADABLE}
        finally:
            conn.close()

    def list_conversations(self, backup, contacts: dict) -> dict:
        """List WhatsApp chats that have messages, newest first.

        Keys match MessageExtractor.list_conversations, plus ``chat_jid``,
        ``chat_type`` ("individual", "group", "broadcast" or "channel") and
        ``archived``. Status updates are not chats and are left out.

        When there are none, ``conversations`` is empty and ``empty_reason`` is
        ``"not_in_backup"`` (WhatsApp isn't in this backup) or ``"no_records"``.
        A ChatStorage.sqlite that is listed but can't be read is an ``error``.
        """
        conn, schema, info = self._open(backup)
        if conn is None:
            return {"conversations": [], **info}
        try:
            ctx = self._context(backup, conn, schema, info)
            cache: dict = {}
            sort = schema.sort_key
            stats = {
                r[0]: (r[1], r[2])
                for r in conn.execute(
                    "SELECT ZCHATSESSION, COUNT(*), MAX(CASE WHEN ZMESSAGEDATE > 0 AND ZMESSAGEDATE < ? "
                    "THEN CAST(ZMESSAGEDATE AS REAL) END) FROM ZWAMESSAGE GROUP BY ZCHATSESSION",
                    (_MAX_APPLE_SECONDS,),
                )
            }
            last_rows = {}
            select = _message_select(schema)
            for row in conn.execute(
                f"{select} JOIN (SELECT ZCHATSESSION AS cs, MAX({sort}) AS top FROM ZWAMESSAGE "
                f"GROUP BY ZCHATSESSION) last ON last.cs = m.ZCHATSESSION AND m.{sort} = last.top "
                f"ORDER BY m.Z_PK DESC"
            ):
                last_rows.setdefault(row["m_ZCHATSESSION"], row)

            conversations = []
            for chat_row in self._chat_rows(conn, schema):
                count, last_date = stats.get(chat_row["Z_PK"], (0, None))
                if not count:
                    continue
                chat = self._chat_info(chat_row, ctx, contacts, cache)
                preview = ""
                last = last_rows.get(chat_row["Z_PK"])
                if last is not None:
                    msg = self._build(last, chat, backup, ctx, contacts, cache, {}, {})
                    preview = self.preview_text(msg)
                    # The preview's own time. The latest date in the chat can belong to a
                    # different message: a late delivery arrives after newer-stamped ones.
                    last_date = last["m_ZMESSAGEDATE"] if _iso(last["m_ZMESSAGEDATE"]) else last_date
                conversations.append({
                    "chat_id": chat["chat_id"],
                    "chat_identifier": chat["jid"],
                    "display_name": chat["name"],
                    "service": "WhatsApp",
                    "message_count": count,
                    "last_message_date": _iso(last_date),
                    "last_message_preview": preview[:100],
                    "is_group": chat["type"] == "group",
                    "chat_jid": chat["jid"],
                    "chat_type": chat["type"],
                    "archived": bool(chat_row["ZARCHIVED"]),
                })
        except _READ_ERRORS as e:
            print(f"[whatsapp] list_conversations failed: {e}", file=sys.stderr, flush=True)
            traceback.print_exc()
            return {"conversations": [], "error": UNREADABLE}
        finally:
            conn.close()

        if not conversations:
            return {"conversations": [], "empty_reason": "no_records"}
        conversations.sort(key=lambda c: (c["last_message_date"] or "", c["chat_id"]), reverse=True)
        return {"conversations": conversations}

    @staticmethod
    def preview_text(message: dict) -> str:
        """A one-line summary of a message dict, for chat lists and search results."""
        if message.get("event_text") and message["message_type"] in ("system", "call", "deleted"):
            return message["event_text"]
        text = (message.get("text") or "").strip()
        if text:
            return " ".join(text.split())
        labels = {
            "image": "Photo", "video": "Video", "audio": "Audio", "gif": "GIF", "sticker": "Sticker",
            "shared_location": "Location", "live_location": "Live location", "contact": "Contact card",
            "poll": "Poll", "link": "Link",
        }
        if message["message_type"] == "document":
            att = (message.get("attachments") or [{}])[0]
            return att.get("transfer_name") or "Document"
        if message.get("event_text"):
            return message["event_text"]
        return labels.get(message["message_type"], "Message")

    # ── Messages ───────────────────────────────────────────────────────────────

    def get_messages(self, backup, chat_id: int, contacts: dict, offset: int = 0, limit: int = 100,
                     date_from: str | None = None, date_to: str | None = None) -> dict:
        """One page of a chat, oldest first.

        ``offset`` counts back from the newest message, like
        MessageExtractor.get_messages, and the order is the phone's arrival order
        (ZSORT). ``date_from``/``date_to`` (ISO, inclusive) filter inside the
        paging. Message dicts carry MessageExtractor's keys plus ``sort``,
        ``sender_jid``, ``quoted``, ``reactions``, ``call``, ``location``,
        ``received_date``, ``event_code`` and ``starred``.
        """
        offset = _bounded(offset, 0)
        limit = _bounded(limit, 100)
        empty = {"messages": [], "total": 0, "next_offset": offset, "offset": offset, "limit": limit}
        chat_id = _int(chat_id)
        if chat_id is None:
            return empty
        conn, schema, info = self._open(backup)
        if conn is None:
            return {**empty, **info}
        try:
            chat_row = self._chat_by_id(conn, schema, chat_id)
            if chat_row is None:
                return empty
            ctx = self._context(backup, conn, schema, info)
            cache: dict = {}
            chat = self._chat_info(chat_row, ctx, contacts, cache)

            where, params = ["ZCHATSESSION = ?"], [chat_id]
            start = iso_to_apple(date_from, nanoseconds=False) if date_from else None
            end = iso_to_apple(date_to, nanoseconds=False) if date_to else None
            if start is not None:
                where.append("ZMESSAGEDATE >= ?")
                params.append(start)
            if end is not None:
                where.append("ZMESSAGEDATE <= ?")
                params.append(end)
            clause = " AND ".join(where)
            total = conn.execute(f"SELECT COUNT(*) FROM ZWAMESSAGE WHERE {clause}", params).fetchone()[0]
            sort = schema.sort_key
            rows = conn.execute(
                f"{_message_select(schema)} WHERE m.Z_PK IN (SELECT Z_PK FROM ZWAMESSAGE WHERE {clause} "
                f"ORDER BY {sort} DESC, Z_PK DESC LIMIT ? OFFSET ?) ORDER BY m.{sort} ASC, m.Z_PK ASC",
                [*params, limit, offset],
            ).fetchall()
            quoted_rows = self._quoted_rows(conn, schema, chat_id, rows)
            link_ids = [r["m_Z_PK"] for r in rows if r["m_ZMESSAGETYPE"] in (TEXT, LINK)]
            link_previews = self._link_previews(conn, schema, link_ids)
            messages = [
                self._build(r, chat, backup, ctx, contacts, cache, quoted_rows, link_previews) for r in rows
            ]
        except _READ_ERRORS as e:
            print(f"[whatsapp] get_messages failed: {e}", file=sys.stderr, flush=True)
            traceback.print_exc()
            return {**empty, "error": UNREADABLE}
        finally:
            conn.close()
        return {
            "messages": messages,
            "total": total,
            "next_offset": offset + len(rows),
            "offset": offset,
            "limit": limit,
        }

    def get_message_offset(self, backup, chat_id: int, message_id: int) -> dict:
        """How many messages are newer than ``message_id`` in its chat, for opening a page around it."""
        chat_id, message_id = _int(chat_id), _int(message_id)
        if chat_id is None or message_id is None:
            return {"offset": 0, "found": False}
        conn, schema, info = self._open(backup)
        if conn is None:
            return {"offset": 0, "found": False}
        try:
            sort = schema.sort_key
            row = conn.execute(
                f"SELECT {sort} FROM ZWAMESSAGE WHERE Z_PK = ? AND ZCHATSESSION = ?", (message_id, chat_id)
            ).fetchone()
            if row is None:
                return {"offset": 0, "found": False}
            key = row[0]
            if key is None:
                # get_messages pages by "sort DESC, Z_PK DESC", where NULLs come last.
                newer = conn.execute(
                    f"SELECT COUNT(*) FROM ZWAMESSAGE WHERE ZCHATSESSION = ? AND ({sort} IS NOT NULL OR Z_PK > ?)",
                    (chat_id, message_id),
                ).fetchone()[0]
            else:
                newer = conn.execute(
                    f"SELECT COUNT(*) FROM ZWAMESSAGE WHERE ZCHATSESSION = ? "
                    f"AND ({sort} > ? OR ({sort} = ? AND Z_PK > ?))",
                    (chat_id, key, key, message_id),
                ).fetchone()[0]
            return {"offset": newer, "found": True}
        except _READ_ERRORS as e:
            print(f"[whatsapp] get_message_offset failed: {e}", file=sys.stderr, flush=True)
            return {"offset": 0, "found": False}
        finally:
            conn.close()

    def search_messages(self, backup, query: str, contacts: dict, chat_id: int | None = None,
                        date_from: str | None = None, date_to: str | None = None, limit: int = 500) -> dict:
        """Messages whose text, caption or document name contains ``query``, oldest first."""
        query = query if isinstance(query, str) else ""
        limit = _int(limit, 500)
        if not query.strip() or limit <= 0:
            return {"results": [], "query": query}
        if chat_id is not None:
            chat_id = _int(chat_id)
            if chat_id is None:
                return {"results": [], "query": query}
        conn, schema, info = self._open(backup)
        if conn is None:
            return {"results": [], "query": query, **info}
        try:
            ctx = self._context(backup, conn, schema, info)
            cache: dict = {}
            chats = {r["Z_PK"]: r for r in self._chat_rows(conn, schema)}
            pattern = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            match = ["m.ZTEXT LIKE ? ESCAPE '\\'"] if schema.has("ZWAMESSAGE", "ZTEXT") else []
            params: list = [pattern] if match else []
            if schema.has("ZWAMEDIAITEM", "ZTITLE") and schema.has("ZWAMESSAGE", "ZMEDIAITEM"):
                match.append(f"(mi.ZTITLE LIKE ? ESCAPE '\\' AND m.ZMESSAGETYPE IN "
                             f"({', '.join(str(t) for t in _MEDIA_TYPES)}))")
                params.append(pattern)
            if not match:
                return {"results": [], "query": query}
            where = [f"({' OR '.join(match)})",
                     f"m.ZMESSAGETYPE NOT IN ({', '.join(str(t) for t in SYSTEM_TYPES)})"]
            if chat_id is not None:
                where.append("m.ZCHATSESSION = ?")
                params.append(chat_id)
            start = iso_to_apple(date_from, nanoseconds=False) if date_from else None
            end = iso_to_apple(date_to, nanoseconds=False) if date_to else None
            if start is not None:
                where.append("m.ZMESSAGEDATE >= ?")
                params.append(start)
            if end is not None:
                where.append("m.ZMESSAGEDATE <= ?")
                params.append(end)
            sql = (f"{_message_select(schema)} WHERE {' AND '.join(where)} "
                   f"ORDER BY m.ZMESSAGEDATE ASC, m.Z_PK ASC")
            results = []
            chat_infos: dict = {}
            for row in conn.execute(sql, params):
                if len(results) >= limit:
                    break
                chat_row = chats.get(row["m_ZCHATSESSION"])
                if chat_row is None:
                    continue  # status updates
                if chat_row["Z_PK"] not in chat_infos:
                    chat_infos[chat_row["Z_PK"]] = self._chat_info(chat_row, ctx, contacts, cache)
                chat = chat_infos[chat_row["Z_PK"]]
                msg = self._build(row, chat, backup, ctx, contacts, cache, {}, {})
                results.append({
                    "message_id": msg["message_id"],
                    "text": msg["text"] or self.preview_text(msg),
                    "date": msg["date"],
                    "is_from_me": msg["is_from_me"],
                    "sender": msg["sender"],
                    "chat_id": chat["chat_id"],
                    "chat_name": chat["name"],
                })
        except _READ_ERRORS as e:
            print(f"[whatsapp] search_messages failed: {e}", file=sys.stderr, flush=True)
            traceback.print_exc()
            return {"results": [], "query": query, "error": UNREADABLE}
        finally:
            conn.close()
        return {"results": results, "query": query}

    # ── Files ──────────────────────────────────────────────────────────────────

    def get_attachment(self, backup, attachment_id: int, thumbnail: bool = False) -> dict:
        """Local path of a media file (or its thumbnail): ``{"path", "mime_type", "filename"}``.

        ``attachment_id`` is the ``attachment_id`` from get_messages. The caller
        reads the bytes. Thumbnails are JPEG whatever their extension.
        """
        attachment_id = _int(attachment_id)
        if attachment_id is None:
            return {"error": "Attachment not found"}
        conn, schema, info = self._open(backup)
        if conn is None:
            return {"error": info.get("error") or "WhatsApp is not in this backup"}
        try:
            if "ZWAMEDIAITEM" not in schema.tables:
                return {"error": "Attachment not found"}
            cols = [schema.col("mi", "ZWAMEDIAITEM", c, c) for c in
                    ("ZMEDIALOCALPATH", "ZXMPPTHUMBPATH", "ZTHUMBNAILLOCALPATH", "ZVCARDSTRING", "ZTITLE")]
            msg_type = ("(SELECT m.ZMESSAGETYPE FROM ZWAMESSAGE m WHERE m.ZMEDIAITEM = mi.Z_PK LIMIT 1)"
                        if schema.has("ZWAMESSAGE", "ZMEDIAITEM") else "NULL")
            row = conn.execute(
                f"SELECT {', '.join(cols)}, {msg_type} AS msg_type FROM ZWAMEDIAITEM mi WHERE mi.Z_PK = ?",
                (attachment_id,),
            ).fetchone()
        except _READ_ERRORS as e:
            print(f"[whatsapp] get_attachment failed: {e}", file=sys.stderr, flush=True)
            return {"error": UNREADABLE}
        finally:
            conn.close()
        if row is None:
            return {"error": "Attachment not found"}
        if thumbnail:
            rel = _backup_rel(row["ZXMPPTHUMBPATH"] or row["ZTHUMBNAILLOCALPATH"])
            mime = "image/jpeg"
        else:
            rel = _backup_rel(row["ZMEDIALOCALPATH"])
            declared = row["ZVCARDSTRING"] if row["msg_type"] not in (CONTACT, DELETED, LOCATION) else None
            mime = _guess_mime(rel, declared)
        if not rel:
            return {"error": "Attachment file not found in backup"}
        path = backup.get_file(rel, domain=SHARED_DOMAIN)
        if not path or not os.path.exists(path):
            return {"error": "Attachment file not found in backup"}
        title = _text(row["ZTITLE"])
        name = title if row["msg_type"] == DOCUMENT and title and not thumbnail else os.path.basename(rel)
        return {"path": path, "mime_type": mime, "filename": name}
