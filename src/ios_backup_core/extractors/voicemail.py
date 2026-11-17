"""
Voicemail extraction from voicemail.db.

Extracted from openextract/python/voicemail.py:VoicemailExtractor.
Changes:
  - export_voicemails() removed — UI concern
  - get_audio() kept (returns file path, not base64 — caller reads the bytes)

Where an iPhone keeps voicemail
-------------------------------
``Library/Voicemail/voicemail.db``, with each message's audio beside it as
``<ROWID>.amr``. Newer iPhones also keep a store per account,
``Library/Voicemail/A-<UUID>/voicemail.db``, with its own audio. Its rows
mostly repeat the main store's, under other ROWIDs and other remote_uids, so a
voicemail is matched across stores by its date, length and caller and listed once.

Which list a voicemail is in on the phone
-----------------------------------------
Checked against an iOS 27.2 iPhone's Phone app (2026-11-16), voicemail by voicemail:

- ``deleted``: ``flags`` has 0x8. Every row with a ``trashed_date`` has it too,
  and older deleted ones can have 0x8 without a ``trashed_date``.
- ``spam``: ``flags`` has 0x80 and isn't deleted. The phone's Spam list held
  exactly these.
- ``voicemail``: everything else, including old voicemails the carrier no
  longer has (``remote_uid`` 0, ``flags`` 0x40).

A voicemail that was marked spam and then deleted is in Deleted, so its
``folder`` is ``"deleted"``. Each record also says ``is_spam`` (0x80) and
``is_deleted`` (0x8, or a ``trashed_date``) on their own, so a deleted spam
voicemail keeps its spam mark.

``flags`` 0x1 marks a voicemail as listened to. The phone counted more deleted
voicemails than its backup held: whatever the rest are, they aren't in the
backup to be read.
"""

import os
import re
import sqlite3
import sys
from datetime import datetime, timezone

from ios_backup_core.contacts import resolve_contact
from ios_backup_core.db import connect_readonly, get_database

FOLDERS = ("voicemail", "spam", "deleted")

_HEARD = 0x1
_DELETED = 0x8
_SPAM = 0x80

_ACCOUNT_STORE = re.compile(r"^Library/Voicemail/(A-[0-9A-Za-z-]+)/voicemail\.db$")
# A voicemail's id: its ROWID in the main store, or "<account folder>.<ROWID>".
_ID = re.compile(r"^(?:(A-[0-9A-Za-z-]+)\.)?(\d+)$")

_LISTED_NOT_READ = (
    "Voicemail is in this backup but could not be read. "
    "The backup may be incomplete or damaged."
)
_NOT_OPENED = (
    "Voicemail is in this backup but couldn't be opened. If the backup is on an "
    "external or network drive, check that it's still connected, then try again."
)


class VoicemailExtractor:
    """Extracts voicemails from iOS backups."""

    VOICEMAIL_DIR = "Library/Voicemail"
    VOICEMAIL_DB_PATH = "Library/Voicemail/voicemail.db"

    def list_voicemails(self, backup, contacts: dict, include_deleted: bool = False) -> dict:
        """List voicemails with metadata, newest first.

        Each has ``folder``: ``"voicemail"``, ``"spam"`` or ``"deleted"``, the
        list the Phone app shows it in, and ``is_spam`` and ``is_deleted``,
        which are separate: a spam voicemail that was deleted is in
        ``"deleted"`` with ``is_spam`` true. ``phone_number`` is the caller's
        number as the backup has it, and ``contact_name`` their name in the
        address book (the number when they aren't in it). Deleted ones are left
        out unless ``include_deleted``. ``counts`` says how many are in each
        list, deleted included either way.

        When none are listed, ``voicemails`` is empty and ``empty_reason`` says why:

        - ``"not_in_backup"``: no voicemail.db. Many phones never have one,
          because visual voicemail depends on the carrier.
        - ``"no_records"``: voicemail.db exists but holds none to list.

        A voicemail.db that is listed in the backup but can't be read is an
        ``error``, with no ``empty_reason``.
        """
        result = self.voicemail_rows(backup)
        if "error" in result:
            return {"voicemails": [], "error": result["error"]}
        if "empty_reason" in result:
            return {"voicemails": [], "empty_reason": result["empty_reason"]}

        counts = {folder: 0 for folder in FOLDERS}
        voicemails = []
        for row in result["rows"]:
            counts[row["folder"]] += 1
            if row["folder"] == "deleted" and not include_deleted:
                continue
            sender = row["sender"] or ""
            date_ts = int(row["date"]) if row["date"] else 0
            voicemails.append({
                "id": row["id"],
                "phone_number": sender,
                "contact_name": resolve_contact(sender, contacts) or sender,
                "date_received": (
                    datetime.fromtimestamp(date_ts, timezone.utc).isoformat() if date_ts else ""
                ),
                "duration": row["duration"],
                "is_read": bool(row["flags"] & _HEARD),
                "transcript": row["transcript"],
                "folder": row["folder"],
                "is_spam": row["is_spam"],
                "is_deleted": row["is_deleted"],
            })

        if not result["rows"]:
            return {"voicemails": [], "empty_reason": "no_records"}
        if not voicemails:  # only deleted ones, not asked for
            return {"voicemails": [], "empty_reason": "no_records", "counts": counts}
        return {"voicemails": voicemails, "counts": counts}

    def voicemail_rows(self, backup) -> dict:
        """Every voicemail in every store, matched across stores, newest first.

        Returns ``{"rows": [...]}``, ``{"rows": [], "empty_reason": "not_in_backup"}``
        or ``{"rows": [], "error": ...}``. Each row has ``id``, ``sender``,
        ``date`` (Unix seconds), ``duration``, ``flags``, ``transcript``,
        ``folder``, ``is_spam`` and ``is_deleted``.
        For readers that count or list voicemails (calls, statistics), so they
        agree with the Voicemail list.
        """
        stores = []
        main = get_database(backup, self.VOICEMAIL_DB_PATH, domain="HomeDomain")
        if main:
            stores.append(("", main, self.VOICEMAIL_DB_PATH))
        elif _listed(backup, self.VOICEMAIL_DB_PATH):
            return {"rows": [], "error": _LISTED_NOT_READ}
        for name, rel in _account_stores(backup):
            path = get_database(backup, rel, domain="HomeDomain")
            if path:
                stores.append((name, path, rel))
        if not stores:
            return {"rows": [], "empty_reason": "not_in_backup"}

        read, error = [], None
        for name, path, rel in stores:
            rows, failure = self._read_with_retries(backup, path, rel)
            if rows is None:
                if not name:
                    return {"rows": [], "error": f"{_NOT_OPENED} ({failure})"}
                error = error or failure  # the main store still lists
                continue
            read.append((name, rows))
        if not read:
            return {"rows": [], "error": f"{_NOT_OPENED} ({error})"}

        audio = _audio_files(backup)
        rows = [_pick(copies, audio) for copies in _match_across_stores(read)]
        rows.sort(key=lambda r: r["date"] or 0, reverse=True)
        return {"rows": rows}

    def _read_with_retries(self, backup, db_path: str, relative_path: str = VOICEMAIL_DB_PATH):
        """Read a voicemail.db's rows, trying again before giving up. Returns (rows, None) or (None, error).

        The database can be readable one moment and not the next (OE-77: the
        home screen read it, then the list couldn't open it). So a failure is
        tried again with the path looked up afresh, which makes a copy that has
        gone from the temp dir again, and then with the backup's own copy of
        the file opened without its -wal, which may miss the newest voicemails
        but shows the rest.
        """
        try:
            return self._read_rows(db_path, backup, ignore_wal=False), None
        except (sqlite3.Error, OSError) as e:
            error = e
            _log_failure(db_path, False, e)

        tried = {(db_path, False)}
        retries = (
            (get_database(backup, relative_path, domain="HomeDomain"), False),
            (backup.get_file(relative_path, domain="HomeDomain"), True),
        )
        for path, ignore_wal in retries:
            if not path or (path, ignore_wal) in tried:
                continue
            tried.add((path, ignore_wal))
            try:
                return self._read_rows(path, backup, ignore_wal), None
            except (sqlite3.Error, OSError) as e:
                error = e
                _log_failure(path, ignore_wal, e)
        return None, error

    @staticmethod
    def _read_rows(db_path: str, backup, ignore_wal: bool) -> list:
        conn = connect_readonly(db_path, backup, ignore_wal=ignore_wal)
        try:
            conn.row_factory = sqlite3.Row
            # Like every other reader here: sorting or temp tables stay in memory,
            # so a read never needs a file in SQLite's own temp folder.
            conn.execute("PRAGMA temp_store = MEMORY")
            columns = {col["name"] for col in conn.execute("PRAGMA table_info(voicemail)")}
            # A column a schema lacks reads as empty rather than failing the whole list.
            wanted = {"sender": "NULL", "duration": "0", "flags": "0", "trashed_date": "0", "transcript": "''"}
            select = ", ".join(c if c in columns else f"{empty} AS {c}" for c, empty in wanted.items())
            return conn.execute(f"""
                SELECT ROWID, date, {select}
                FROM voicemail
                ORDER BY date DESC
            """).fetchall()
        finally:
            conn.close()

    def get_audio_path(self, backup, voicemail_id) -> str | None:
        """Return the local path to a voicemail's .amr audio file, or None.

        ``voicemail_id`` is an ``id`` from list_voicemails: a ROWID in the main
        store, or ``"<account folder>.<ROWID>"``.
        """
        match = _ID.match(str(voicemail_id).strip())
        if not match:
            return None
        store, rowid = match.groups()
        folder = f"{self.VOICEMAIL_DIR}/{store}" if store else self.VOICEMAIL_DIR
        file_path = backup.get_file(f"{folder}/{rowid}.amr", domain="HomeDomain")
        if file_path and os.path.exists(file_path):
            return file_path
        return None


def folder_of(flags, trashed_date) -> str:
    """The Phone app's list for a voicemail row: "voicemail", "spam" or "deleted".

    Deleted comes first, as on the phone: a spam voicemail that was deleted is
    in Deleted. ``is_spam`` keeps its spam mark.
    """
    if is_deleted(flags, trashed_date):
        return "deleted"
    if is_spam(flags):
        return "spam"
    return "voicemail"


def is_spam(flags) -> bool:
    """Marked spam (0x80), whichever list it's in now."""
    return bool((flags or 0) & _SPAM)


def is_deleted(flags, trashed_date) -> bool:
    """In Deleted: 0x8, or a trashed_date (every such row has 0x8 too)."""
    return bool(trashed_date or (flags or 0) & _DELETED)


def _listed(backup, relative_path: str) -> bool:
    try:
        return bool(backup.list_files(domain="HomeDomain", path_like=relative_path))
    except Exception:
        return False


def _account_stores(backup) -> list:
    """(folder name, relative path) of each per-account voicemail.db, in name order."""
    try:
        listed = backup.list_files(domain="HomeDomain", path_like="Library/Voicemail/%/voicemail.db")
    except Exception:
        return []
    stores = {}
    for entry in listed:
        match = _ACCOUNT_STORE.match(entry["path"])
        if match:
            stores[match.group(1)] = entry["path"]
    return sorted(stores.items())


def _audio_files(backup) -> set:
    try:
        return {e["path"] for e in backup.list_files(domain="HomeDomain", path_like="Library/Voicemail/%.amr")}
    except Exception:
        return set()


def _match_across_stores(stores: list) -> list:
    """Group the rows of each store with their copies in other stores.

    ``stores`` is [(name, rows)], the main store ("") first. A copy is a row
    in another store with the same date, length and caller. Rows that match
    within one store stay apart: they are separate rows on the phone too.
    """
    groups, by_key = [], {}
    for name, rows in stores:
        for row in rows:
            row = dict(row)
            row["_store"] = name
            key = (row["date"], row["duration"], row["sender"])
            group = by_key.get(key)
            if group is not None and all(copy["_store"] != name for copy in group):
                group.append(row)
                continue
            group = [row]
            groups.append(group)
            by_key.setdefault(key, group)
    return groups


def _pick(copies: list, audio: set) -> dict:
    """One row for a voicemail from its copies: the first whose audio is in the backup.

    It's deleted if any copy is, so a deletion recorded in one store counts,
    and spam if any copy is marked spam.
    """
    def has_audio(row):
        folder = f"Library/Voicemail/{row['_store']}" if row["_store"] else "Library/Voicemail"
        return f"{folder}/{row['ROWID']}.amr" in audio

    row = next((r for r in copies if has_audio(r)), copies[0])
    spam = any(is_spam(r["flags"]) for r in copies)
    deleted = any(is_deleted(r["flags"], r["trashed_date"]) for r in copies)
    folder = "deleted" if deleted else "spam" if spam else "voicemail"
    return {
        "id": f"{row['_store']}.{row['ROWID']}" if row["_store"] else row["ROWID"],
        "sender": row["sender"],
        "date": row["date"],
        "duration": row["duration"],
        "flags": row["flags"] or 0,
        "transcript": next((r["transcript"] for r in copies if r["transcript"]), ""),
        "folder": folder,
        "is_spam": spam,
        "is_deleted": deleted,
    }


def _log_failure(path: str, ignore_wal: bool, error: Exception) -> None:
    """Say on stderr which file failed and how, so the next report of it says more."""
    def size(p):
        try:
            return os.path.getsize(p)
        except OSError:
            return None

    print(
        f"[voicemail] could not read {path} (size={size(path)}, wal_size={size(path + '-wal')}, "
        f"ignore_wal={ignore_wal}): {type(error).__name__}: {error}",
        file=sys.stderr, flush=True,
    )
