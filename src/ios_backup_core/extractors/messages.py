"""
Message extraction from sms.db.

Extracted from openextract/python/messages.py:MessageExtractor.
Changes:
  - Imports replaced with ios_backup_core.* equivalents
  - Export methods (_export_*, get_attachment) removed — UI concern
  - _pragma_cache and _indexed_dbs moved from module-level dicts into
    instance attributes to avoid cross-instance state
  - _count_cache moved into instance
  - _tlog() calls removed — library is silent
  - Backup parameter typed as BackupAccessor protocol
"""

import re
import sqlite3
from typing import Optional

from ios_backup_core.contacts import resolve_contact
from ios_backup_core.schema import detect_message_schema
from ios_backup_core.text import (
    _type_from_bundle_id,
    parse_attributed_body,
    parse_link_payload,
    clean_message_text,
    text_looks_contaminated,
)
from ios_backup_core.timestamps import apple_to_iso, iso_to_apple


class MessageExtractor:
    """Extracts messages and conversations from iOS sms.db."""

    SMS_DB_PATH = "Library/SMS/sms.db"

    def __init__(self):
        # Persistent SQLite connections keyed by db_path
        self._connections: dict[str, sqlite3.Connection] = {}
        # Cache total message counts per (db_path, chat_id)
        self._count_cache: dict[tuple, int] = {}
        # Cache of sms.db column sets, keyed by db_path
        self._pragma_cache: dict[str, dict] = {}
        # Set of db_paths for which we've already created performance indexes
        self._indexed_dbs: set[str] = set()

    def _get_sms_db(self, backup) -> Optional[str]:
        db_path = backup.get_file(self.SMS_DB_PATH, domain="HomeDomain")
        if db_path:
            # Pre-extract WAL/SHM so SQLite merges recent uncommitted writes
            backup.get_file(self.SMS_DB_PATH + "-wal", domain="HomeDomain")
            backup.get_file(self.SMS_DB_PATH + "-shm", domain="HomeDomain")
        return db_path

    def _get_conn(self, db_path: str) -> sqlite3.Connection:
        if db_path not in self._connections:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA synchronous = OFF")
            conn.execute("PRAGMA cache_size = -50000")
            conn.execute("PRAGMA temp_store = MEMORY")
            self._ensure_indexes(conn, db_path)
            conn.execute("PRAGMA query_only = TRUE")
            self._connections[db_path] = conn
        return self._connections[db_path]

    def _ensure_indexes(self, conn, db_path: str) -> None:
        """Create performance indexes on sms.db the first time it is opened.

        Uses IF NOT EXISTS — idempotent. Errors silently ignored for read-only
        filesystems.
        """
        if db_path in self._indexed_dbs:
            return
        try:
            conn.executescript("""
                CREATE INDEX IF NOT EXISTS _oe_cmj_chat
                    ON chat_message_join (chat_id, message_id);
                CREATE INDEX IF NOT EXISTS _oe_msg_date
                    ON message (date);
            """)
            conn.commit()
        except Exception:
            pass
        self._indexed_dbs.add(db_path)

    def list_conversations(self, backup, contacts: dict) -> dict:
        """List all conversations with preview info."""
        db_path = self._get_sms_db(backup)
        if not db_path:
            return {"conversations": [], "error": "sms.db not found"}

        conn = self._get_conn(db_path)
        conversations = []
        try:
            rows = conn.execute("""
                WITH ranked_msgs AS (
                    SELECT
                        cmj.chat_id,
                        COALESCE(m.text, '[Message contents hidden]') AS text,
                        m.date,
                        ROW_NUMBER() OVER (
                            PARTITION BY cmj.chat_id ORDER BY m.date DESC
                        ) AS rn
                    FROM chat_message_join cmj
                    JOIN message m ON m.ROWID = cmj.message_id
                ),
                last_msg AS (
                    SELECT chat_id, text, date FROM ranked_msgs WHERE rn = 1
                ),
                msg_counts AS (
                    SELECT chat_id, COUNT(*) AS message_count
                    FROM chat_message_join
                    GROUP BY chat_id
                )
                SELECT
                    c.ROWID          AS chat_id,
                    c.chat_identifier,
                    c.display_name,
                    c.service_name,
                    mc.message_count,
                    lm.date          AS last_message_date,
                    lm.text          AS last_message_text
                FROM chat c
                JOIN last_msg   lm ON lm.chat_id = c.ROWID
                JOIN msg_counts mc ON mc.chat_id = c.ROWID
                ORDER BY lm.date DESC
            """).fetchall()

            all_chat_ids = [row["chat_id"] for row in rows]
            participants_map: dict[int, list[str]] = {}
            if all_chat_ids:
                placeholders = ",".join("?" * len(all_chat_ids))
                part_rows = conn.execute(f"""
                    SELECT chj.chat_id, h.id
                    FROM chat_handle_join chj
                    JOIN handle h ON h.ROWID = chj.handle_id
                    WHERE chj.chat_id IN ({placeholders})
                """, all_chat_ids).fetchall()
                for pr in part_rows:
                    participants_map.setdefault(pr["chat_id"], []).append(pr["id"])

            for row in rows:
                chat_identifier = row["chat_identifier"] or ""
                display_name = row["display_name"] or ""
                participant_count = len(participants_map.get(row["chat_id"], []))
                is_group = participant_count > 1 or "chat" in chat_identifier.lower()

                if not display_name:
                    if is_group and row["chat_id"] in participants_map:
                        handles = participants_map[row["chat_id"]]
                        resolved = [(resolve_contact(h, contacts), h) for h in handles]
                        resolved.sort(key=lambda x: (not x[0], x[1]))
                        names = [name or handle for name, handle in resolved]
                        if len(names) <= 3:
                            display_name = ", ".join(names)
                        else:
                            display_name = f"{names[0]} + {len(names) - 1}"
                    else:
                        display_name = resolve_contact(chat_identifier, contacts)

                conversations.append({
                    "chat_id": row["chat_id"],
                    "chat_identifier": chat_identifier,
                    "display_name": display_name or chat_identifier,
                    "service": row["service_name"] or "iMessage",
                    "message_count": row["message_count"],
                    "last_message_date": apple_to_iso(row["last_message_date"]),
                    "last_message_preview": (row["last_message_text"] or "")[:100],
                    "is_group": is_group,
                })
        except Exception:
            raise

        return {"conversations": conversations}

    def get_messages(
        self,
        backup,
        chat_id: int,
        contacts: dict,
        offset: int = 0,
        limit: int = 100,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
    ) -> dict:
        """Get paginated messages from a conversation."""
        db_path = self._get_sms_db(backup)
        if not db_path:
            return {"messages": [], "error": "sms.db not found"}

        conn = self._get_conn(db_path)
        messages = []

        try:
            if db_path not in self._pragma_cache:
                self._pragma_cache[db_path] = detect_message_schema(conn)
            schema = self._pragma_cache[db_path]

            has_attributed_body   = schema['has_attributed_body']
            has_payload_data      = schema['has_payload_data']
            has_balloon_bundle_id = schema['has_balloon_bundle_id']
            has_audio_message     = schema['has_audio_message']
            has_item_type         = schema['has_item_type']
            has_share_status      = schema['has_share_status']
            has_share_direction   = schema['has_share_direction']
            has_uncanonicalized   = schema['has_uncanonicalized']

            select_parts = [
                'm.ROWID AS message_id',
                'm.text',
                'm.date',
                'm.is_from_me',
                'm.cache_has_attachments',
                'm.associated_message_type',
                'h.id AS handle_id_str',
            ]
            if has_attributed_body:   select_parts.append('m.attributedBody')
            if has_payload_data:      select_parts.append('m.payload_data')
            if has_balloon_bundle_id: select_parts.append('m.balloon_bundle_id')
            if has_audio_message:     select_parts.append('m.is_audio_message')
            if has_item_type:         select_parts.append('m.item_type')
            if has_share_status:      select_parts.append('m.share_status')
            if has_share_direction:   select_parts.append('m.share_direction')
            if has_uncanonicalized:   select_parts.append('h.uncanonicalized_id')

            date_clauses = []
            date_params: list = [chat_id]
            if date_from or date_to:
                ns = self._pragma_cache[db_path]['uses_nanoseconds']
                apple_from = iso_to_apple(date_from, nanoseconds=ns) if date_from else None
                apple_to_val = iso_to_apple(date_to, nanoseconds=ns) if date_to else None
                if apple_from is not None:
                    date_clauses.append("m.date >= ?")
                    date_params.append(apple_from)
                if apple_to_val is not None:
                    date_clauses.append("m.date <= ?")
                    date_params.append(apple_to_val)
            where_extra = (" AND " + " AND ".join(date_clauses)) if date_clauses else ""

            rows = conn.execute(f"""
                SELECT {', '.join(select_parts)}
                FROM message m
                LEFT JOIN handle h ON h.ROWID = m.handle_id
                WHERE m.ROWID IN (
                    SELECT DISTINCT cmj.message_id
                    FROM chat_message_join cmj
                    WHERE cmj.chat_id = ?{where_extra}
                    ORDER BY cmj.message_id DESC
                    LIMIT ? OFFSET ?
                )
                ORDER BY m.date DESC
            """, (*date_params, limit, offset)).fetchall()

            rows = list(rows)[::-1]

            # Batch-fetch attachment metadata (N+1 → 1 query)
            attachment_ids_needed = [
                row["message_id"] for row in rows if bool(row["cache_has_attachments"])
            ]
            attachments_by_msg: dict[int, list] = {}
            if attachment_ids_needed:
                placeholders = ','.join('?' * len(attachment_ids_needed))
                for att in conn.execute(f"""
                    SELECT
                        maj.message_id,
                        a.ROWID AS attachment_id,
                        a.filename,
                        a.mime_type,
                        a.transfer_name,
                        a.total_bytes
                    FROM attachment a
                    JOIN message_attachment_join maj ON maj.attachment_id = a.ROWID
                    WHERE maj.message_id IN ({placeholders})
                """, attachment_ids_needed).fetchall():
                    transfer_name = att["transfer_name"] or ""
                    filename = att["filename"] or ""
                    if transfer_name.endswith('.pluginPayloadAttachment'):
                        continue
                    if filename.endswith('.pluginPayloadAttachment'):
                        continue
                    mid = att["message_id"]
                    attachments_by_msg.setdefault(mid, []).append({
                        "attachment_id": att["attachment_id"],
                        "filename": filename,
                        "mime_type": att["mime_type"],
                        "transfer_name": transfer_name,
                        "total_bytes": att["total_bytes"],
                    })

            # Pre-fetch the primary contact name for this chat
            chat_contact_name = ""
            chat_handle_row = conn.execute("""
                SELECT h.id FROM handle h
                INNER JOIN chat_handle_join chj ON chj.handle_id = h.ROWID
                WHERE chj.chat_id = ?
                LIMIT 1
            """, (chat_id,)).fetchone()
            if chat_handle_row:
                chat_contact_name = resolve_contact(chat_handle_row[0], contacts) or chat_handle_row[0]

            for row in rows:
                handle = row["handle_id_str"] or ""
                sender_name = resolve_contact(handle, contacts) if handle else ""
                if not sender_name and has_uncanonicalized and row["uncanonicalized_id"]:
                    sender_name = resolve_contact(row["uncanonicalized_id"], contacts)
                if not sender_name:
                    sender_name = handle
                if not sender_name:
                    sender_name = chat_contact_name

                bundle_type = _type_from_bundle_id(row["balloon_bundle_id"] if has_balloon_bundle_id else None)
                is_audio    = bool(row["is_audio_message"]) if has_audio_message else False
                item_type   = (row["item_type"]      or 0)  if has_item_type     else 0
                share_status    = (row["share_status"]    or 0) if has_share_status    else 0
                share_direction = (row["share_direction"] or 0) if has_share_direction else 0

                msg_text = row["text"]
                if bundle_type == "location":
                    msg_type = "hidden"
                elif bundle_type:
                    msg_type = bundle_type
                elif is_audio:
                    msg_type = "audio"
                elif item_type in (3, 4):
                    if item_type == 4 and share_direction == 0 and share_status == 1:
                        msg_type = "location_stopped_by_me"
                    elif item_type == 4 and share_direction == 0:
                        msg_type = "location_started_by_me"
                    elif item_type == 4 and share_direction == 1 and share_status == 1:
                        msg_type = "location_stopped_by_them"
                    elif item_type == 4 and share_direction == 1:
                        msg_type = "location_started_by_them"
                    else:
                        msg_type = "location"
                else:
                    msg_type = "text"

                # Prefer attributedBody when text is missing OR looks contaminated.
                # The text column often keeps TypedStream / data-detector junk
                # ("WversionYdd-result", "'()*Z)+X...XDateTime/", "%&'-./4:>?…")
                # while attributedBody still has the real user-visible string.
                if (
                    has_attributed_body
                    and msg_type == "text"
                    and row["attributedBody"]
                    and (not msg_text or text_looks_contaminated(msg_text))
                ):
                    attr_text, attr_type = parse_attributed_body(row["attributedBody"])
                    cleaned_attr = clean_message_text(attr_text) if attr_text else ""
                    if cleaned_attr and not text_looks_contaminated(cleaned_attr):
                        msg_text = attr_text
                        if attr_type != "text":
                            msg_type = attr_type

                if msg_text:
                    msg_text = clean_message_text(msg_text)

                if msg_type == "hidden":
                    continue

                if not msg_text and msg_type == "text":
                    if bool(row["cache_has_attachments"]):
                        msg_type = "attachment"
                    else:
                        msg_type = "system"
                    msg_text = ""

                link_preview = None
                if msg_type == "link" and has_payload_data and row["payload_data"]:
                    link_preview = parse_link_payload(row["payload_data"]) or None

                real_attachments = attachments_by_msg.get(row["message_id"], [])

                messages.append({
                    "message_id": row["message_id"],
                    "text": msg_text,
                    "message_type": msg_type,
                    "link_preview": link_preview,
                    "date": apple_to_iso(row["date"]),
                    "is_from_me": bool(row["is_from_me"]),
                    "sender": "me" if row["is_from_me"] else sender_name,
                    "sender_handle": handle,
                    "has_attachments": bool(real_attachments),
                    "attachments": real_attachments,
                    "is_reaction": row["associated_message_type"] is not None and row["associated_message_type"] != 0,
                })

            # Total count — use cache when no date filters are applied
            count_key = (db_path, chat_id)
            if not date_from and not date_to and count_key in self._count_cache:
                total = self._count_cache[count_key]
            else:
                total = conn.execute(
                    f"""SELECT COUNT(DISTINCT cmj.message_id) FROM chat_message_join cmj
                        INNER JOIN message m ON m.ROWID = cmj.message_id
                        WHERE cmj.chat_id = ?{where_extra}""",
                    tuple(date_params)
                ).fetchone()[0]
                if not date_from and not date_to:
                    self._count_cache[count_key] = total

        except Exception:
            raise

        return {
            "messages": messages,
            "total": total,
            "next_offset": offset + len(rows),
            "offset": offset,
            "limit": limit,
        }

    def search_messages(
        self,
        backup,
        query: str,
        contacts: dict,
        chat_id: Optional[int] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        limit: int = 500,
    ) -> dict:
        """Search messages by text content, optionally filtered by chat and date range."""
        db_path = self._get_sms_db(backup)
        if not db_path:
            return {"results": []}

        conn = self._get_conn(db_path)

        # Detect nanosecond timestamps for date conversion
        if db_path not in self._pragma_cache:
            self._pragma_cache[db_path] = detect_message_schema(conn)

        results = []
        try:
            sql = """
                SELECT
                    m.ROWID AS message_id,
                    m.text,
                    m.date,
                    m.is_from_me,
                    h.id AS handle_id_str,
                    cmj.chat_id
                FROM message m
                LEFT JOIN handle h ON h.ROWID = m.handle_id
                INNER JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
                WHERE m.text LIKE ?
            """
            params: list = [f"%{query}%"]

            if chat_id:
                sql += " AND cmj.chat_id = ?"
                params.append(chat_id)

            if date_from or date_to:
                ns = self._pragma_cache[db_path]['uses_nanoseconds']
                apple_from = iso_to_apple(date_from, nanoseconds=ns) if date_from else None
                apple_to_val = iso_to_apple(date_to, nanoseconds=ns) if date_to else None
                if apple_from is not None:
                    sql += " AND m.date >= ?"
                    params.append(apple_from)
                if apple_to_val is not None:
                    sql += " AND m.date <= ?"
                    params.append(apple_to_val)

            sql += f" ORDER BY m.date ASC LIMIT {int(limit)}"

            rows = conn.execute(sql, params).fetchall()
            for row in rows:
                handle = row["handle_id_str"] or ""
                results.append({
                    "message_id": row["message_id"],
                    "text": row["text"],
                    "date": apple_to_iso(row["date"]),
                    "is_from_me": bool(row["is_from_me"]),
                    "sender": "me" if row["is_from_me"] else (resolve_contact(handle, contacts) or handle),
                    "chat_id": row["chat_id"],
                })
        except Exception:
            raise

        return {"results": results, "query": query}
