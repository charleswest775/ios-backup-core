"""
Voice Memos extraction from CloudRecordings.db (and the older Recordings.db).

Where an iPhone keeps Voice Memos
---------------------------------
The app keeps a Core Data database beside the audio files, in one folder:

- Current iOS: ``AppDomainGroup-group.com.apple.VoiceMemos.shared``,
  ``Recordings/CloudRecordings.db``. Read from Apple's VoiceMemos.framework
  (iOS 26.5) and seen in an iOS 26.5 backup by others; which release moved it
  there (probably iOS 17) is not known.
- iOS 12 to 16: ``MediaDomain``, ``Media/Recordings/CloudRecordings.db``.
  Seen on iOS 13.3.1 and 13.4.1, which also keep the older ``Recordings.db``
  (table ``ZRECORDING``) beside it.

The database is found through the manifest, preferring the Voice Memos app
group, then MediaDomain, then anywhere else, and never inside an edited
recording's ``.composition/`` folder. When there is no CloudRecordings.db, or
it holds no recordings and a ``Recordings.db`` sits in the same folder, that
older ``ZRECORDING`` table is read instead. One result never mixes the two.
The audio is always in the chosen database's folder.

What each recording says
------------------------
- ``title``: the first of ``ZCUSTOMLABELFORSORTING``, ``ZENCRYPTEDTITLE``
  (plain text in the phone's own copy, despite its name),
  ``ZENCRYPTEDCUSTOMLABEL`` and ``ZCUSTOMLABEL`` that has text, else None.
  From iOS 17 ``ZCUSTOMLABEL`` can hold a date string written for older
  devices, so it comes last. A title is never made up here.
- ``date``: ``ZDATE``, when recording started. ``duration_seconds``:
  ``ZLOCALDURATION`` when above 0, else ``ZDURATION``.
- ``ZFLAGS`` (kept on the phone): 0x2 audio only in iCloud, 0x4 playable,
  0x40 recorded on Apple Watch, 0x400 has a transcript with text, 0x800
  layered, 0x1000 spatial. ``ZSHAREDFLAGS`` (synced): 0x1 favourite, 0x2
  enhanced, 0x40 recorded on watchOS. A flag reads as None when its column
  isn't in this version of the table (favourites arrived with folders, about
  iOS 14). These bits are read from the iOS 26.5 framework; whether older
  versions use them the same way is not checked yet.
- ``in_phone_list``: ``(ZFLAGS & 6) != 0``, the phone's own test for a
  playable recording. It is only reported: nothing is left out on it, so a
  wrong guess about an older version can't hide a library.
- ``is_deleted``: ``ZEVICTIONDATE`` is set, which means Recently Deleted (not
  an iCloud eviction); ``deleted_date`` is when.
- Folders (``ZFOLDER``) are the user's own folders only: ``ZUUID``,
  ``ZENCRYPTEDNAME`` (plain text, like the title) and ``ZRANK``. All
  Recordings, Favorites and Recently Deleted are worked out from the flags.

Audio
-----
A recording's file is ``basename(ZPATH)`` in the database's folder (older rows
can hold a full device path; ZPATH can be NULL), else the file named
``YYYYMMDD HHMMSS-<first 8 of ZUNIQUEID>.<m4a|qta|qt>``. Presence comes from
the manifest alone: listing never opens an audio file, which in an encrypted
backup would mean decrypting every recording. A recording whose audio isn't
in the backup says why: ``"in_icloud_only"`` when ZFLAGS has 0x2, else
``"not_in_backup"``. Transcripts (iOS 18+) live inside the audio file, not
the database, and are read one at a time by get_transcript.

None of this has been checked against a backup with recordings newer than
iOS 13 yet; the corpus backups we hold have none. Columns are checked with
``PRAGMA table_info`` before they are selected, so a version that lacks one
reads it as None.
"""

import math
import os
import posixpath
import re
import sqlite3
import sys
import traceback
from collections.abc import Iterable

from ios_backup_core import mp4
from ios_backup_core.db import connect_readonly, get_database
from ios_backup_core.timestamps import NANOSECOND_THRESHOLD, apple_to_iso

VOICE_MEMOS_DOMAIN = "AppDomainGroup-group.com.apple.VoiceMemos.shared"
VOICE_MEMOS_DB_PATH = "Recordings/CloudRecordings.db"
MEDIA_DOMAIN = "MediaDomain"
MEDIA_DB_PATH = "Media/Recordings/CloudRecordings.db"

CLOUD_DB = "CloudRecordings.db"
LEGACY_DB = "Recordings.db"
_TABLES = {CLOUD_DB: "ZCLOUDRECORDING", LEGACY_DB: "ZRECORDING"}

AUDIO_EXTENSIONS = (".m4a", ".qta", ".qt")

# ZFLAGS
_NEEDS_DOWNLOAD = 0x2
_IN_PHONE_LIST = 0x2 | 0x4
_ON_WATCH = 0x40
_HAS_TRANSCRIPT = 0x400
_LAYERED = 0x800
_SPATIAL = 0x1000
# ZSHAREDFLAGS
_FAVORITE = 0x1
_ENHANCED = 0x2
_SHARED_ON_WATCH = 0x40

# Voice Memos names a file "<YYYYMMDD HHMMSS>-<first 8 of its unique id>.<ext>".
_STANDARD_NAME = re.compile(r"^[0-9]{8} [0-9]{6}-([0-9A-Fa-f]{8})\.(m4a|qta|qt)$", re.IGNORECASE)
_DIGITS = re.compile(r"[0-9]+")
_MAX_ID = 2**63 - 1

# Core Data's own tables, which say nothing about the recordings.
_BOOKKEEPING = {"Z_METADATA", "Z_PRIMARYKEY", "Z_MODELCACHE", "ACHANGE", "ATRANSACTION",
                "ATRANSACTIONSTRING"}

_RECORDING_COLUMNS = (
    "ZUNIQUEID", "ZDATE", "ZDURATION", "ZLOCALDURATION", "ZCUSTOMLABELFORSORTING",
    "ZENCRYPTEDTITLE", "ZENCRYPTEDCUSTOMLABEL", "ZCUSTOMLABEL", "ZPATH", "ZFLAGS",
    "ZSHAREDFLAGS", "ZEVICTIONDATE", "ZFOLDER",
)
_TITLE_COLUMNS = ("ZCUSTOMLABELFORSORTING", "ZENCRYPTEDTITLE", "ZENCRYPTEDCUSTOMLABEL", "ZCUSTOMLABEL")
_FOLDER_COLUMNS = ("ZUUID", "ZENCRYPTEDNAME", "ZRANK")

_NOT_READABLE = (
    "Voice Memos are in this backup but could not be read. "
    "The backup may be incomplete or damaged."
)
_UNSUPPORTED = "This backup's Voice Memos are in an older format OpenExtract can't read yet."


def _text(value) -> str | None:
    """Return a TEXT value as str. Some backups store text columns as bytes."""
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray, memoryview)):
        value = bytes(value).decode("utf-8", errors="replace")
    value = str(value).strip()
    return value or None


def _int(value) -> int | None:
    if value is None or value == "" or isinstance(value, (bytes, bytearray, memoryview)):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _float(value) -> float | None:
    if value is None or value == "" or isinstance(value, (bytes, bytearray, memoryview)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _date(value) -> str | None:
    number = _float(value)
    return apple_to_iso(number) if number is not None else None


def _seconds(value) -> float | None:
    """ZDATE as Apple seconds for sorting, read the way apple_to_iso reads it."""
    number = _float(value)
    if number is None:
        return None
    return number / 1_000_000_000 if number > NANOSECOND_THRESHOLD else number


def _bits(value, mask: int, present: bool) -> bool | None:
    """Whether a flags column has any bit of ``mask``; None when the column isn't there."""
    if not present:
        return None
    return bool((_int(value) or 0) & mask)


def _basename(path: str | None) -> str | None:
    """The file name at the end of a ZPATH (a bare name, or a full device path)."""
    if not path:
        return None
    name = path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1].strip()
    return name if name and name not in (".", "..") else None


def _format(file_name: str | None) -> str | None:
    if not file_name:
        return None
    extension = posixpath.splitext(file_name)[1].lower().lstrip(".")
    return extension or None


def _columns(cursor, table: str) -> set:
    cursor.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in cursor.fetchall()}


def _select(columns: set, wanted: Iterable[str]) -> str:
    """SELECT list with the columns that exist; missing ones read as NULL."""
    return ", ".join(name if name in columns else f"NULL AS {name}" for name in wanted)


def _pk(columns: set) -> str:
    # Z_PK is the table's INTEGER PRIMARY KEY, so the same value as its rowid.
    return "Z_PK" if "Z_PK" in columns else "rowid AS Z_PK"


def _log(message: str) -> None:
    print(f"[voice_memos] {message}", file=sys.stderr, flush=True)


class _Source:
    """One database to read: where it is and which table holds its recordings."""

    def __init__(self, domain: str, path: str, name: str):
        self.domain, self.path, self.name = domain, path, name
        self.table = _TABLES[name]
        self.folder = posixpath.dirname(path)

    def relative(self, file_name: str) -> str:
        return f"{self.folder}/{file_name}" if self.folder else file_name


class _Audio:
    """The audio files in a database's folder, from the manifest (none are opened)."""

    def __init__(self, names: Iterable[str]):
        self.names = set(names)
        self.folded = {}
        self.by_id = {}
        order = {ext.lstrip("."): i for i, ext in enumerate(AUDIO_EXTENSIONS)}
        for name in sorted(self.names):
            self.folded.setdefault(name.casefold(), name)
            match = _STANDARD_NAME.match(name)
            if match:
                key = match.group(1).lower()
                current = self.by_id.get(key)
                rank = order.get(match.group(2).lower(), len(order))
                if current is None or rank < current[0]:
                    self.by_id[key] = (rank, name)

    def find(self, path_name: str | None, unique_id: str | None) -> str | None:
        """The file for a recording: its ZPATH's name, else its standard name; None when absent."""
        if path_name:
            if path_name in self.names:
                return path_name
            folded = self.folded.get(path_name.casefold())
            if folded:
                return folded
        if unique_id and len(unique_id) >= 8:
            found = self.by_id.get(unique_id[:8].lower())
            if found:
                return found[1]
        return None


class VoiceMemoExtractor:
    """Extracts Voice Memos recordings, their folders and their audio from iOS backups."""

    VOICE_MEMOS_DB_PATH = VOICE_MEMOS_DB_PATH

    # ------------------------------------------------------------------
    # Finding and reading the database
    # ------------------------------------------------------------------

    @staticmethod
    def _candidates(backup, name: str) -> list:
        """(domain, path) of each copy of ``name`` in the manifest, most likely first."""
        try:
            entries = backup.list_files(path_like=f"%{name}") or []
        except Exception:
            entries = []
        found = set()
        for entry in entries:
            path = entry.get("path") or ""
            if posixpath.basename(path).casefold() != name.casefold() or ".composition/" in path:
                continue
            found.add((entry.get("domain") or "", path))
        canonical = {(VOICE_MEMOS_DOMAIN, posixpath.join(posixpath.dirname(VOICE_MEMOS_DB_PATH), name)),
                     (MEDIA_DOMAIN, posixpath.join(posixpath.dirname(MEDIA_DB_PATH), name))}
        return sorted(found, key=lambda e: (
            "voicememos.shared" not in e[0].lower(),
            e[0] != MEDIA_DOMAIN,
            e not in canonical,
            e,
        ))

    @staticmethod
    def _listed(backup, domain: str, path: str) -> bool:
        try:
            return any(e.get("path") == path for e in backup.list_files(domain=domain, path_like=path) or [])
        except Exception:
            return False

    @staticmethod
    def _read(backup, source: _Source):
        """Read one database. Returns ("rows", (rows, folder rows, columns)), ("empty", None),
        ("unsupported", None) or ("error", message)."""
        db_path = get_database(backup, source.path, domain=source.domain)
        if not db_path:
            return "error", _NOT_READABLE
        try:
            conn = connect_readonly(db_path, backup)
        except sqlite3.Error as e:
            return "error", f"{_NOT_READABLE} ({e})"
        try:
            conn.row_factory = sqlite3.Row
            # Sorting or temp tables stay in memory, like every other reader here.
            conn.execute("PRAGMA temp_store = MEMORY")
            # Titles are typed by people: one value that isn't valid UTF-8 must
            # not fail the whole list.
            conn.text_factory = lambda raw: raw.decode("utf-8", "replace")
            cursor = conn.cursor()
            tables = {r[0] for r in cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if source.table not in tables:
                others = {t for t in tables if not t.startswith("sqlite_") and t not in _BOOKKEEPING}
                if others and not others & set(_TABLES.values()):
                    return "unsupported", None
                return "empty", None

            columns = _columns(cursor, source.table)
            rows = cursor.execute(
                f"SELECT {_pk(columns)}, {_select(columns, _RECORDING_COLUMNS)} FROM {source.table}"
            ).fetchall()
            if not rows:
                return "empty", None

            folders = []
            # The older ZRECORDING entity has no folders, so its result has none.
            if source.name == CLOUD_DB and "ZFOLDER" in tables:
                folder_columns = _columns(cursor, "ZFOLDER")
                folders = cursor.execute(
                    f"SELECT {_pk(folder_columns)}, {_select(folder_columns, _FOLDER_COLUMNS)} FROM ZFOLDER"
                ).fetchall()
            return "rows", (rows, folders, columns)
        except Exception as e:
            _log(f"could not read {source.domain}/{source.path}: {type(e).__name__}: {e}")
            traceback.print_exc()
            return "error", f"{_NOT_READABLE} ({e})"
        finally:
            conn.close()

    def _load(self, backup):
        """Choose the database and read it. Returns ((source, rows, folders, columns), None)
        or (None, empty_or_error_result)."""
        def outcome(state, payload, source):
            if state == "rows":
                return (source, *payload), None
            if state == "error":
                return None, {"voice_memos": [], "error": payload}
            if state == "unsupported":
                return None, {"voice_memos": [], "error": _UNSUPPORTED, "empty_reason": "unsupported_format"}
            return None, {"voice_memos": [], "empty_reason": "no_records"}

        cloud = self._candidates(backup, CLOUD_DB)
        if cloud:
            source = _Source(*cloud[0], CLOUD_DB)
            state, payload = self._read(backup, source)
            if state != "empty":
                return outcome(state, payload, source)
            # No recordings in it: an older phone may still have them in
            # Recordings.db beside it.
            legacy_path = source.relative(LEGACY_DB)
            if self._listed(backup, source.domain, legacy_path):
                legacy = _Source(source.domain, legacy_path, LEGACY_DB)
                state, payload = self._read(backup, legacy)
                if state in ("rows", "error"):
                    return outcome(state, payload, legacy)
            return outcome("empty", None, source)

        legacy = self._candidates(backup, LEGACY_DB)
        if not legacy:
            return None, {"voice_memos": [], "empty_reason": "not_in_backup"}
        source = _Source(*legacy[0], LEGACY_DB)
        return outcome(*self._read(backup, source), source)

    @staticmethod
    def _audio(backup, source: _Source) -> _Audio:
        """The audio files directly in the database's folder, from one manifest query."""
        prefix = f"{source.folder}/" if source.folder else ""
        try:
            entries = backup.list_files(domain=source.domain, path_like=f"{prefix}%") or []
        except Exception:
            entries = []
        names = []
        for entry in entries:
            path = entry.get("path") or ""
            if not path.startswith(prefix) or ".composition/" in path:
                continue
            name = path[len(prefix):]
            if name and "/" not in name and name.lower().endswith(AUDIO_EXTENSIONS):
                names.append(name)
        return _Audio(names)

    # ------------------------------------------------------------------
    # Records
    # ------------------------------------------------------------------

    def _memos(self, backup, loaded) -> tuple:
        """(records newest first, folders in the phone's order)."""
        source, rows, folder_rows, columns = loaded
        audio = self._audio(backup, source)
        has_flags = "ZFLAGS" in columns
        has_shared = "ZSHAREDFLAGS" in columns

        folders = {}
        for row in folder_rows:
            folders[_int(row["Z_PK"])] = {
                "id": _text(row["ZUUID"]),
                "name": _text(row["ZENCRYPTEDNAME"]),
                "rank": _int(row["ZRANK"]),
                "count": 0,
            }

        keyed = []
        for row in rows:
            flags, shared = row["ZFLAGS"], row["ZSHAREDFLAGS"]
            unique_id = _text(row["ZUNIQUEID"])
            folder_pk = _int(row["ZFOLDER"])
            folder = folders.get(folder_pk) if folder_pk is not None else None
            eviction = row["ZEVICTIONDATE"]
            deleted = eviction is not None and eviction != ""
            local = _float(row["ZLOCALDURATION"])
            duration = local if local is not None and local > 0 else _float(row["ZDURATION"])
            path_name = _basename(_text(row["ZPATH"]))
            found = audio.find(path_name, unique_id)
            file_name = found or path_name
            if found:
                missing = None
            elif _int(flags) is not None and _int(flags) & _NEEDS_DOWNLOAD:
                missing = "in_icloud_only"
            else:
                missing = "not_in_backup"
            on_watch = None
            if has_flags or has_shared:
                on_watch = bool((_int(flags) or 0) & _ON_WATCH or (_int(shared) or 0) & _SHARED_ON_WATCH)
            date = _date(row["ZDATE"])
            memo = {
                "id": _int(row["Z_PK"]),
                "unique_id": unique_id,
                "title": next((t for t in (_text(row[c]) for c in _TITLE_COLUMNS) if t), None),
                "date": date,
                "duration_seconds": duration,
                "folder_id": folder["id"] if folder else None,
                "folder": folder["name"] if folder else None,
                "is_favorite": _bits(shared, _FAVORITE, has_shared),
                "is_deleted": deleted,
                "deleted_date": _date(eviction) if deleted else None,
                "in_phone_list": _bits(flags, _IN_PHONE_LIST, has_flags),
                "recorded_on_watch": on_watch,
                "is_enhanced": _bits(shared, _ENHANCED, has_shared),
                "is_layered": _bits(flags, _LAYERED, has_flags),
                "is_spatial": _bits(flags, _SPATIAL, has_flags),
                "has_transcript": _bits(flags, _HAS_TRANSCRIPT, has_flags),
                "file_name": file_name,
                "format": _format(file_name),
                "audio_available": found is not None,
                "audio_missing_reason": missing,
                "source": source.name,
            }
            if folder and not deleted:
                folder["count"] += 1
            seconds = _seconds(row["ZDATE"]) if date else None
            keyed.append(((seconds is None, -(seconds or 0.0), -(memo["id"] or 0)), memo))

        keyed.sort(key=lambda pair: pair[0])
        ordered_folders = sorted(folders.items(), key=lambda item: (
            item[1]["rank"] is None, item[1]["rank"] or 0,
            item[1]["name"] is None, (item[1]["name"] or "").casefold(), item[0] or 0,
        ))
        return [memo for _key, memo in keyed], [folder for _pk, folder in ordered_folders]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def list_voice_memos(self, backup) -> dict:
        """List every recording, newest first (by date, then by id, highest first).

        Returns ``{"voice_memos": [...], "folders": [...]}``. Recently Deleted
        recordings are included with ``is_deleted`` true; ``folders`` are the
        user's folders in the phone's order, each with ``count``, the number
        of recordings in it that aren't deleted (``[]`` when this version has
        no folders). See the module docstring for each field.

        When there are none, ``voice_memos`` is empty and ``empty_reason`` says why:

        - ``"not_in_backup"``: no CloudRecordings.db or Recordings.db (Voice
          Memos never used, or the app was removed).
        - ``"no_records"``: the database exists but holds no recordings.
        - ``"unsupported_format"``: the database has neither recordings table,
          so it is a layout this reader doesn't know; ``error`` says so too.

        A database that is listed in the backup but can't be read is an
        ``error``, with no ``empty_reason``.

        No audio file is opened: ``audio_available`` comes from the manifest.
        """
        loaded, early = self._load(backup)
        if early is not None:
            return early
        try:
            memos, folders = self._memos(backup, loaded)
        except Exception as e:
            _log(f"could not list recordings: {type(e).__name__}: {e}")
            traceback.print_exc()
            return {"voice_memos": [], "error": f"{_NOT_READABLE} ({e})"}
        if not memos:
            return {"voice_memos": [], "empty_reason": "no_records"}
        return {"voice_memos": memos, "folders": folders}

    def get_audio_path(self, backup, memo_id) -> str | None:
        """Return the local path to a recording's audio file, or None.

        ``memo_id`` is an ``id`` from list_voice_memos: an int, or a string of
        digits. Anything else (negative numbers, booleans, other strings) gives
        None, so an id can never point outside the recordings folder. In an
        unencrypted backup the path is the file inside the backup, stored
        under its hash with no extension; ``file_name`` in the record has the
        real name.
        """
        wanted = _memo_id(memo_id)
        if wanted is None:
            return None
        try:
            loaded, early = self._load(backup)
            if early is not None:
                return None
            memos, _folders = self._memos(backup, loaded)
            memo = next((m for m in memos if m["id"] == wanted), None)
            # Only a file the manifest lists can be extracted; asking for any
            # other would just log a failed extraction in an encrypted backup.
            if memo is None or not memo["audio_available"]:
                return None
            source = loaded[0]
            path = backup.get_file(source.relative(memo["file_name"]), domain=source.domain)
            if path and os.path.exists(path):
                return path
        except Exception as e:
            _log(f"could not find the audio of recording {wanted}: {type(e).__name__}: {e}")
        return None

    def get_transcript(self, backup, memo_id) -> dict | None:
        """The transcript inside a recording's audio file (iOS 18+), or None.

        Returns ``{"text": str, "segments": [{"text", "start", "end"}],
        "locale": str | None}``. None when the id isn't a recording, its audio
        isn't in the backup, or the file has no transcript. Reading it means
        extracting the whole audio file in an encrypted backup, so it is read
        on demand, never while listing.
        """
        try:
            path = self.get_audio_path(backup, memo_id)
            return mp4.read_transcript(path) if path else None
        except Exception:
            return None


def _memo_id(memo_id) -> int | None:
    """A recording id as an int, or None when it isn't one."""
    if isinstance(memo_id, bool):
        return None
    if isinstance(memo_id, int):
        value = memo_id
    elif isinstance(memo_id, str) and _DIGITS.fullmatch(memo_id):
        try:
            value = int(memo_id)
        except ValueError:  # more digits than Python converts (4300)
            return None
    else:
        return None
    return value if 0 <= value <= _MAX_ID else None
