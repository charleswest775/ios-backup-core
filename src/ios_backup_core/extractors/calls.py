"""
Call history extraction from CallHistory.storedata.

Extracted from openextract/python/calls.py:CallExtractor.
Changes:
  - export_calls_csv() removed — UI concern
  - _clean_phone_number() and _resolve_contact() replaced with imports from
    ios_backup_core.contacts
  - apple_date_to_iso import replaced with ios_backup_core.timestamps.apple_to_iso
  - APPLE_EPOCH_OFFSET imported from ios_backup_core.timestamps
"""

import plistlib
import sqlite3
import unicodedata
from datetime import datetime, timezone
from typing import Optional

from ios_backup_core.contacts import clean_phone_number, resolve_contact
from ios_backup_core.timestamps import APPLE_EPOCH_OFFSET, apple_to_iso


def _clean_text(value) -> str:
    """Return *value* as display-safe text.

    Third-party (CallKit) providers such as Teams/Skype can store ZADDRESS /
    ZNAME as raw bytes, a binary plist, or text containing control and
    private-use characters, which showed up as garbage in the call list.
    """
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray)):
        raw = bytes(value)
        if raw.startswith(b"bplist00"):
            try:
                decoded = plistlib.loads(raw)
                value = decoded if isinstance(decoded, str) else ""
            except Exception:
                value = ""
        else:
            value = raw.decode("utf-8", errors="ignore")
    text = "".join(
        c for c in str(value)
        # Drop control, format (bidi marks etc.), private-use, surrogate and
        # unassigned code points and U+FFFD — but keep the zero-width joiner
        # so multi-part emoji in names survive.
        if c == "\u200d"
        or (unicodedata.category(c) not in ("Cc", "Cf", "Co", "Cs", "Cn") and c != "\ufffd")
    )
    return " ".join(text.split())


def _call_status(direction: str, answered, duration) -> str:
    """Derive answered/missed.

    ZANSWERED only describes incoming calls: iOS leaves it 0 on outgoing calls
    even when they connected, so outgoing calls were shown as "missed" with a
    positive duration (#59 in openextract). A non-zero duration means the call
    connected, whichever way it went.
    """
    if (duration or 0) > 0:
        return "answered"
    if direction == "incoming" and answered:
        return "answered"
    return "missed"


class CallExtractor:
    """Extracts call history from iOS backups."""

    CALL_HISTORY_PATH = "Library/CallHistoryDB/CallHistory.storedata"

    def _find_db_paths(self, backup) -> list:
        """Return all candidate call history DB paths by searching the manifest.

        Also extracts any accompanying WAL/SHM files so SQLite can read
        un-checkpointed records automatically.
        """
        candidates = []
        seen_hashes: set = set()

        entries = backup.list_files(path_like="%CallHistory%")
        for entry in entries:
            rel_path = entry.get("path", "")
            domain = entry.get("domain", "HomeDomain")
            file_hash = entry.get("hash", "")

            if not (rel_path.endswith(".storedata") or rel_path.endswith(".db")):
                if rel_path.endswith("-wal") or rel_path.endswith("-shm"):
                    backup.get_file(rel_path, domain=domain)
                continue

            if file_hash in seen_hashes:
                continue

            p = backup.get_file(rel_path, domain=domain)
            if p:
                seen_hashes.add(file_hash)
                candidates.append(p)
                backup.get_file(rel_path + "-wal", domain=domain)
                backup.get_file(rel_path + "-shm", domain=domain)

        return candidates

    def _read_db(self, db_path: str, seen_ids: set) -> list:
        """Read all call records from a single DB, skipping IDs already seen."""
        rows_out = []
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = TRUE")
        conn.execute("PRAGMA synchronous = OFF")
        conn.execute("PRAGMA cache_size = -10000")
        conn.execute("PRAGMA temp_store = MEMORY")
        cursor = conn.cursor()
        try:
            cursor.execute("PRAGMA table_info(ZCALLRECORD)")
            columns = [info[1] for info in cursor.fetchall()]
            service_col = "ZSERVICE_PROVIDER" if "ZSERVICE_PROVIDER" in columns else "NULL"
            video_col = "ZIS_VIDEO" if "ZIS_VIDEO" in columns else "NULL"
            name_col = "ZNAME" if "ZNAME" in columns else "NULL"
            rows = cursor.execute(f"""
                SELECT Z_PK, ZADDRESS AS address, {name_col} AS name,
                       ZDATE AS date, ZDURATION AS duration,
                       ZCALLTYPE AS call_type, ZORIGINATED AS originated,
                       ZANSWERED AS answered,
                       {service_col} AS service_provider,
                       {video_col} AS is_video
                FROM ZCALLRECORD ORDER BY ZDATE DESC
            """).fetchall()
        except sqlite3.Error:
            try:
                rows = cursor.execute("""
                    SELECT ROWID AS Z_PK, address, NULL AS name, date, duration,
                           flags AS call_type, read AS answered,
                           NULL AS originated, NULL AS service_provider, NULL AS is_video
                    FROM call ORDER BY date DESC
                """).fetchall()
            except sqlite3.Error:
                conn.close()
                return rows_out
        conn.close()

        for row in rows:
            pk = row["Z_PK"]
            if pk in seen_ids:
                continue
            seen_ids.add(pk)
            rows_out.append(row)
        return rows_out

    def _read_facetime_from_sms(self, backup, contacts: dict) -> list:
        """Read FaceTime calls from sms.db (item_type 1=video, 2=audio)."""
        db_path = backup.get_file("Library/SMS/sms.db", domain="HomeDomain")
        if not db_path:
            return []

        backup.get_file("Library/SMS/sms.db-wal", domain="HomeDomain")
        backup.get_file("Library/SMS/sms.db-shm", domain="HomeDomain")

        rows_out = []
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only = TRUE")

            sample = conn.execute(
                "SELECT date FROM message WHERE item_type IN (1,2) LIMIT 1"
            ).fetchone()
            uses_ns = sample and float(sample[0]) > 1_000_000_000_000 if sample else False

            rows = conn.execute("""
                SELECT m.ROWID, m.date, m.is_from_me, m.item_type,
                       h.id AS address
                FROM message m
                LEFT JOIN handle h ON m.handle_id = h.ROWID
                WHERE m.item_type IN (1, 2)
                ORDER BY m.date DESC
            """).fetchall()
            conn.close()

            for row in rows:
                raw_ts = float(row["date"] or 0)
                if not raw_ts:
                    continue
                apple_ts = raw_ts / 1_000_000_000 if uses_ns else raw_ts
                unix_ts = apple_ts + APPLE_EPOCH_OFFSET
                iso_date = datetime.fromtimestamp(unix_ts, timezone.utc).isoformat()

                address = row["address"] or ""
                contact_name = resolve_contact(address, contacts) or address or "Unknown"
                direction = "outgoing" if row["is_from_me"] else "incoming"
                app_name = "FaceTime Video" if row["item_type"] == 1 else "FaceTime Audio"

                rows_out.append({
                    "_unix_ts": unix_ts,
                    "call_id": f"ft_{row['ROWID']}",
                    "address": address,
                    "contact_name": contact_name,
                    "date": iso_date,
                    "duration": 0,
                    "direction": direction,
                    "status": "answered",
                    "app": app_name,
                })
        except Exception:
            pass
        return rows_out

    def _read_voicemails_as_calls(self, backup, contacts: dict) -> list:
        """Read voicemail.db and return each entry as a synthetic call record."""
        db_path = backup.get_file("Library/Voicemail/voicemail.db", domain="HomeDomain")
        if not db_path:
            return []

        rows_out = []
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only = TRUE")
            rows = conn.execute("""
                SELECT ROWID, sender, date, duration
                FROM voicemail
                WHERE (trashed_date = 0 OR trashed_date IS NULL)
            """).fetchall()
            conn.close()

            for row in rows:
                sender = row["sender"] or ""
                unix_ts = int(row["date"]) if row["date"] else 0
                if not unix_ts:
                    continue
                iso_date = datetime.fromtimestamp(unix_ts, timezone.utc).isoformat()
                contact_name = resolve_contact(sender, contacts) or sender or "Unknown"
                rows_out.append({
                    "_unix_ts": unix_ts,
                    "_apple_ts": unix_ts - APPLE_EPOCH_OFFSET,
                    "call_id": f"vm_{row['ROWID']}",
                    "address": sender,
                    "contact_name": contact_name,
                    "date": iso_date,
                    "duration": row["duration"] or 0,
                    "direction": "incoming",
                    "status": "missed",
                    "app": "Phone",
                })
        except Exception:
            pass
        return rows_out

    def list_calls(
        self,
        backup,
        contacts: dict,
        offset: int = 0,
        limit: int = 200,
    ) -> dict:
        """List call history records, supplemented with FaceTime and voicemail records."""
        db_paths = self._find_db_paths(backup)

        all_rows = []
        seen_ids: set = set()
        errors = []
        for db_path in db_paths:
            try:
                all_rows.extend(self._read_db(db_path, seen_ids))
            except Exception as e:
                errors.append(str(e))

        call_records = []
        call_fingerprints: set = set()

        for row in all_rows:
            address = _clean_text(row["address"])
            name = _clean_text(row["name"])
            contact_name = resolve_contact(address, contacts) or name or address or "Unknown"
            originated = row["originated"]

            # sqlite3.Row has no .get(); index columns directly.
            if originated is not None:
                direction = "outgoing" if originated else "incoming"
            else:
                direction = "outgoing" if row["call_type"] == 5 else "incoming"

            status = _call_status(direction, row["answered"], row["duration"])

            provider = row["service_provider"]
            app_name = "Phone"
            if provider:
                p_lower = provider.lower()
                if "facetime" in p_lower:
                    app_name = "FaceTime Video" if row["is_video"] else "FaceTime Audio"
                elif "whatsapp" in p_lower:
                    app_name = "WhatsApp"
                elif "skype" in p_lower:
                    app_name = "Skype"
                elif "messenger" in p_lower:
                    app_name = "Messenger"
                elif "telegram" in p_lower:
                    app_name = "Telegram"
                elif "viber" in p_lower:
                    app_name = "Viber"
                elif "signal" in p_lower:
                    app_name = "Signal"
                elif "instagram" in p_lower:
                    app_name = "Instagram"
                elif "telephony" not in p_lower:
                    app_name = provider

            apple_ts = row["date"] or 0
            unix_ts = apple_ts + APPLE_EPOCH_OFFSET
            clean_num = clean_phone_number(address)
            call_fingerprints.add((clean_num or address.lower(), int(unix_ts) // 60))

            call_records.append({
                "_unix_ts": unix_ts,
                "call_id": row["Z_PK"],
                "address": address,
                "contact_name": contact_name,
                "date": apple_to_iso(apple_ts),
                "duration": row["duration"] or 0,
                "direction": direction,
                "status": status,
                "app": app_name,
            })

        def _merge(extra_records):
            for rec in extra_records:
                clean_num = clean_phone_number(rec["address"])
                key = (clean_num or rec["address"].lower(), int(rec["_unix_ts"]) // 60)
                if key not in call_fingerprints:
                    call_fingerprints.add(key)
                    call_records.append(rec)

        _merge(self._read_voicemails_as_calls(backup, contacts))
        _merge(self._read_facetime_from_sms(backup, contacts))

        if not call_records:
            msg = errors[0] if errors else (
                "Call history not found. Apple requires backups to be encrypted to include "
                "call logs. Please enable 'Encrypt local backup' in iTunes/Finder."
            )
            return {"calls": [], "error": msg}

        call_records.sort(key=lambda r: r["_unix_ts"] or 0, reverse=True)
        total = len(call_records)
        page = call_records[offset:offset + limit]
        calls = [{k: v for k, v in r.items() if not k.startswith("_")} for r in page]

        return {
            "calls": calls,
            "total": total,
            "offset": offset,
            "limit": limit,
        }
