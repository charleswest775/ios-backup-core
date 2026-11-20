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
- **Old backups (iOS 9 and earlier)** — reads `Manifest.mbdb` and the flat file layout iTunes used before iOS 10, decrypts their older keybag, and reads the iOS 6–9 database layouts (sms.db, call_history.db, notes.sqlite). Encrypted iOS 10.0 and 10.1 backups (Manifest.db, but that older keybag) open too
- **Extractors** for Messages, Calls, Notes, Browser History (Safari + Firefox), Voicemail, Photos, WhatsApp, Podcasts (the shows you follow, as OPML for another podcast app, and your listening history; episode audio is never in an iPhone backup), Voice Memos (recordings with their folders, Recently Deleted and any transcript the iPhone made; a recording kept only in iCloud is listed, but its audio is never in a backup), Calendar (your calendars and subscriptions, events with their invitees and alerts, repeating events as one rule each, as .ics for another calendar app; the birthdays in the Birthdays calendar come from Contacts and aren't stored as events, and files attached to events aren't read)

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
podcasts = reader.podcasts()
history = reader.podcast_episodes(listened_only=True)
memos = reader.voice_memos()
calendars = reader.calendars()
events = reader.calendar_events()
```

To hand the shows you follow to another podcast app:

```python
from ios_backup_core.extractors.podcasts import podcasts_to_opml

opml = podcasts_to_opml(reader.podcasts()["podcasts"], title="My podcasts")
```

To move a calendar into Calendar, Outlook or Google Calendar:

```python
import re
from ios_backup_core.extractors.calendar_events import events_to_ics

calendar = reader.calendars()["calendars"][0]
events = reader.calendar_events(calendar_id=calendar["id"])["events"]
# Titles come from the backup (a subscribed calendar's from whoever publishes it),
# so they can hold "/" or "..": make a safe file name before using one.
name = re.sub(r"[^\w .-]+", "_", calendar["title"] or "").strip(" .") or "Calendar"
with open(f"{name}.ics", "w", encoding="utf-8", newline="") as f:
    f.write(events_to_ics(events, [calendar]))
```

## License

MIT
