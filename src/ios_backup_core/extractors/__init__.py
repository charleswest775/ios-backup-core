"""Extractor modules for each iOS data source."""

from ios_backup_core.extractors.messages import MessageExtractor
from ios_backup_core.extractors.calls import CallExtractor
from ios_backup_core.extractors.notes import NoteExtractor
from ios_backup_core.extractors.browser_history import BrowserHistoryExtractor
from ios_backup_core.extractors.voicemail import VoicemailExtractor
from ios_backup_core.extractors.photos import PhotoExtractor
from ios_backup_core.extractors.whatsapp import WhatsAppExtractor
from ios_backup_core.extractors.podcasts import PodcastExtractor
from ios_backup_core.extractors.voice_memos import VoiceMemoExtractor
from ios_backup_core.extractors.calendar_events import CalendarExtractor

__all__ = [
    "MessageExtractor",
    "CallExtractor",
    "NoteExtractor",
    "BrowserHistoryExtractor",
    "VoicemailExtractor",
    "PhotoExtractor",
    "WhatsAppExtractor",
    "PodcastExtractor",
    "VoiceMemoExtractor",
    "CalendarExtractor",
]
