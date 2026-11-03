# ios-backup-core

Cross-platform iPhone backup extraction and data normalization library.

Extracted and consolidated from [OpenExtract](https://github.com/openextract/openextract).

## Features

- **Unified timestamp handling** — Apple epoch, nanosecond auto-detection, WebKit/Firefox conversions
- **Attributed body parsing** — NSKeyedArchiver (bplist00) and TypedStream fallback paths
- **Contact resolution** — phone normalization, +1 prefix toggling, email case-insensitive match
- **PRAGMA-based schema detection** — probes sms.db for optional columns present only on newer iOS versions
- **Protobuf wire-format walker** — extracts strings from Notes gzip+proto blobs
- **BackupAccessor protocol** — pluggable backup source; ships with `LocalBackupAccessor` for on-disk backups (encrypted via iphone-backup-decrypt, or unencrypted)
- **Extractors** for Messages, Calls, Notes, Browser History (Safari, Chrome, Edge, Brave, Firefox), Voicemail, Photos

## Install

```bash
pip install ios-backup-core
```

For encrypted backup support:

```bash
pip install "ios-backup-core[decrypt]"
```

## Quick start

```python
from ios_backup_core import BackupReader

reader = BackupReader.from_path("/path/to/backup", password="optional")
contacts = reader.contacts()
messages = reader.messages(chat_id=1)
calls = reader.calls()
notes = reader.notes()
```

## License

MIT
