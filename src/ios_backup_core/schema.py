"""
PRAGMA-based iOS SQLite schema detection.

Extracted from messages.py get_messages() lines 510-535.
Probes column existence once and caches the result — schema never changes
for a given backup.
"""

import sqlite3
from typing import Optional

from ios_backup_core.timestamps import db_uses_nanoseconds


def detect_message_schema(conn: sqlite3.Connection) -> dict:
    """Probe sms.db for optional columns. Returns dict of capability flags.

    Extracted verbatim from the inline _pragma_cache population in
    messages.py:get_messages(). Do not alter — column names are authoritative
    for what iOS versions ship in sms.db.

    Returns:
        has_attributed_body   — 'attributedBody' column present (iOS 9+)
        has_payload_data      — 'payload_data' column present (link previews)
        has_balloon_bundle_id — 'balloon_bundle_id' column present (iMessage extensions)
        has_audio_message     — 'is_audio_message' column present
        has_item_type         — 'item_type' column present (FaceTime, location events)
        has_share_status      — 'share_status' column present (location sharing)
        has_share_direction   — 'share_direction' column present (location sharing)
        has_uncanonicalized   — 'uncanonicalized_id' in handle table (iOS 13+)
        uses_nanoseconds      — timestamps stored as nanoseconds (iOS 14+)
    """
    msg_cols = {r[1] for r in conn.execute("PRAGMA table_info(message)").fetchall()}
    handle_cols = {r[1] for r in conn.execute("PRAGMA table_info(handle)").fetchall()}
    return {
        'has_attributed_body':   'attributedBody'     in msg_cols,
        'has_payload_data':      'payload_data'       in msg_cols,
        'has_balloon_bundle_id': 'balloon_bundle_id'  in msg_cols,
        'has_audio_message':     'is_audio_message'   in msg_cols,
        'has_item_type':         'item_type'          in msg_cols,
        'has_share_status':      'share_status'       in msg_cols,
        'has_share_direction':   'share_direction'    in msg_cols,
        'has_uncanonicalized':   'uncanonicalized_id' in handle_cols,
        'uses_nanoseconds':      db_uses_nanoseconds(conn),
    }


def detect_call_schema(conn: sqlite3.Connection) -> dict:
    """Probe CallHistory.storedata for optional columns.

    Returns:
        has_service_provider — 'ZSERVICE_PROVIDER' column present (iOS 10+)
        has_is_video         — 'ZIS_VIDEO' column present (iOS 10+)
        uses_zcallrecord     — True if using ZCALLRECORD table (modern schema)
    """
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}

    if 'ZCALLRECORD' not in tables:
        return {
            'has_service_provider': False,
            'has_is_video': False,
            'uses_zcallrecord': False,
        }

    columns = {r[1] for r in conn.execute("PRAGMA table_info(ZCALLRECORD)").fetchall()}
    return {
        'has_service_provider': 'ZSERVICE_PROVIDER' in columns,
        'has_is_video':         'ZIS_VIDEO'         in columns,
        'uses_zcallrecord':     True,
    }


def build_select(columns: list[str], available: set[str]) -> list[str]:
    """Return the subset of *columns* that exist in *available*.

    Useful for constructing SELECT lists that work across iOS versions
    without querying columns that don't exist on older schemas.

    Example:
        select_cols = build_select(
            ['attributedBody', 'balloon_bundle_id', 'text'],
            msg_cols,
        )
    """
    return [col for col in columns if col in available]


def table_names(conn: sqlite3.Connection) -> set[str]:
    """Return the names of all tables in the database."""
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    """Return the column names of *table*, or an empty set if it doesn't exist."""
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
