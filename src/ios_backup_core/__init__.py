"""ios-backup-core: iPhone backup extraction and data normalization."""

from ios_backup_core.backup import BackupReader, BackupAccessor, LocalBackupAccessor
from ios_backup_core.timestamps import (
    apple_to_iso,
    iso_to_apple,
    detect_timestamp_format,
    db_uses_nanoseconds,
    APPLE_EPOCH,
    APPLE_EPOCH_OFFSET,
    NANOSECOND_THRESHOLD,
)
from ios_backup_core.text import (
    parse_attributed_body,
    clean_message_text,
    text_looks_contaminated,
)
from ios_backup_core.contacts import normalize_phone, resolve_contact, ContactResolver

__version__ = "0.1.0"

__all__ = [
    # High-level API
    "BackupReader",
    # Backup access
    "BackupAccessor",
    "LocalBackupAccessor",
    # Timestamps
    "apple_to_iso",
    "iso_to_apple",
    "detect_timestamp_format",
    "db_uses_nanoseconds",
    "APPLE_EPOCH",
    "APPLE_EPOCH_OFFSET",
    "NANOSECOND_THRESHOLD",
    # Text parsing
    "parse_attributed_body",
    "clean_message_text",
    "text_looks_contaminated",
    # Contacts
    "normalize_phone",
    "resolve_contact",
    "ContactResolver",
]
