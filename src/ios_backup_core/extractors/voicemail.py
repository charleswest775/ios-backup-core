"""
Voicemail extraction from voicemail.db.

Extracted from openextract/python/voicemail.py:VoicemailExtractor.
Changes:
  - export_voicemails() removed — UI concern
  - get_audio() kept (returns file path, not base64 — caller reads the bytes)
"""

import os
import sqlite3
import sys
from datetime import datetime, timezone

from ios_backup_core.contacts import resolve_contact
from ios_backup_core.db import connect_readonly, get_database

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

    VOICEMAIL_DB_PATH = "Library/Voicemail/voicemail.db"

    def list_voicemails(self, backup, contacts: dict) -> dict:
        """List all voicemails with metadata.

        When there are none, ``voicemails`` is empty and ``empty_reason`` says why:

        - ``"not_in_backup"``: no voicemail.db. Many phones never have one,
          because visual voicemail depends on the carrier.
        - ``"no_records"``: voicemail.db exists but holds no voicemails.

        A voicemail.db that is listed in the backup but can't be read is an
        ``error``, with no ``empty_reason``.
        """
        db_path = get_database(backup, self.VOICEMAIL_DB_PATH, domain="HomeDomain")
        if not db_path:
            if backup.list_files(domain="HomeDomain", path_like=self.VOICEMAIL_DB_PATH):
                return {"voicemails": [], "error": _LISTED_NOT_READ}
            return {"voicemails": [], "empty_reason": "not_in_backup"}

        rows, error = self._read_with_retries(backup, db_path)
        if rows is None:
            return {"voicemails": [], "error": f"{_NOT_OPENED} ({error})"}

        voicemails = []
        try:
            for row in rows:
                sender = row["sender"] or ""
                caller_name = resolve_contact(sender, contacts) or sender

                date_ts = int(row["date"]) if row["date"] else 0
                iso_date = (
                    datetime.fromtimestamp(date_ts, timezone.utc).isoformat()
                    if date_ts else ""
                )

                voicemails.append({
                    "id": row["ROWID"],
                    "phone_number": sender,
                    "contact_name": caller_name,
                    "date_received": iso_date,
                    "duration": row["duration"],
                    "is_read": bool(row["flags"] & 1) if row["flags"] else False,
                    "transcript": row["transcript"] or "",
                })
        except Exception as e:
            import traceback
            traceback.print_exc()
            return {"voicemails": [], "error": str(e)}

        if not voicemails:
            return {"voicemails": [], "empty_reason": "no_records"}
        return {"voicemails": voicemails}

    def _read_with_retries(self, backup, db_path: str):
        """Read voicemail.db's rows, trying again before giving up. Returns (rows, None) or (None, error).

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
            (get_database(backup, self.VOICEMAIL_DB_PATH, domain="HomeDomain"), False),
            (backup.get_file(self.VOICEMAIL_DB_PATH, domain="HomeDomain"), True),
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
            columns = [col["name"] for col in conn.execute("PRAGMA table_info(voicemail)")]
            transcript_col = "transcript" if "transcript" in columns else "'' AS transcript"
            return conn.execute(f"""
                SELECT ROWID, sender, date, duration, flags, trashed_date, token, {transcript_col}
                FROM voicemail
                WHERE trashed_date = 0 OR trashed_date IS NULL
                ORDER BY date DESC
            """).fetchall()
        finally:
            conn.close()

    def get_audio_path(self, backup, voicemail_id: int) -> str | None:
        """Return the local path to a voicemail .amr audio file, or None."""
        audio_path = f"Library/Voicemail/{voicemail_id}.amr"
        file_path = backup.get_file(audio_path, domain="HomeDomain")
        if file_path and os.path.exists(file_path):
            return file_path
        return None


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
