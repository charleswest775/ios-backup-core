"""
Voicemail extraction from voicemail.db.

Extracted from openextract/python/voicemail.py:VoicemailExtractor.
Changes:
  - export_voicemails() removed — UI concern
  - get_audio() kept (returns file path, not base64 — caller reads the bytes)
"""

import os
import sqlite3
from datetime import datetime, timezone

from ios_backup_core.contacts import resolve_contact
from ios_backup_core.db import connect_readonly, get_database


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
                return {
                    "voicemails": [],
                    "error": (
                        "Voicemail is in this backup but could not be read. "
                        "The backup may be incomplete or damaged."
                    ),
                }
            return {"voicemails": [], "empty_reason": "not_in_backup"}

        voicemails = []
        try:
            conn = connect_readonly(db_path, backup)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            cursor.execute("PRAGMA table_info(voicemail)")
            columns = [col["name"] for col in cursor.fetchall()]
            transcript_col = "transcript" if "transcript" in columns else "'' as transcript"

            rows = cursor.execute(f"""
                SELECT
                    ROWID,
                    sender,
                    date,
                    duration,
                    flags,
                    trashed_date,
                    token,
                    {transcript_col}
                FROM voicemail
                WHERE trashed_date = 0 OR trashed_date IS NULL
                ORDER BY date DESC
            """).fetchall()

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
                    "transcript": row["transcript"] if "transcript" in list(row.keys()) else "",
                })

            conn.close()
        except Exception as e:
            import traceback
            traceback.print_exc()
            return {"voicemails": [], "error": str(e)}

        if not voicemails:
            return {"voicemails": [], "empty_reason": "no_records"}
        return {"voicemails": voicemails}

    def get_audio_path(self, backup, voicemail_id: int) -> str | None:
        """Return the local path to a voicemail .amr audio file, or None."""
        audio_path = f"Library/Voicemail/{voicemail_id}.amr"
        file_path = backup.get_file(audio_path, domain="HomeDomain")
        if file_path and os.path.exists(file_path):
            return file_path
        return None
