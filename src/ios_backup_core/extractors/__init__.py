"""Extractor modules for each iOS data source."""

from ios_backup_core.extractors.messages import MessageExtractor
from ios_backup_core.extractors.calls import CallExtractor
from ios_backup_core.extractors.notes import NoteExtractor
from ios_backup_core.extractors.browser_history import BrowserHistoryExtractor
from ios_backup_core.extractors.voicemail import VoicemailExtractor
from ios_backup_core.extractors.photos import PhotoExtractor
from ios_backup_core.extractors.whatsapp import WhatsAppExtractor

__all__ = [
    "MessageExtractor",
    "CallExtractor",
    "NoteExtractor",
    "BrowserHistoryExtractor",
    "VoicemailExtractor",
    "PhotoExtractor",
    "WhatsAppExtractor",
]
