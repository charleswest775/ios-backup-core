"""
Dataclasses for normalized output records.

These are the types returned by extractors when callers want structured
objects rather than raw dicts.
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Message:
    message_id: int
    text: str
    message_type: str          # 'text', 'audio', 'location', 'payment', etc.
    date: Optional[str]        # ISO 8601
    is_from_me: bool
    sender: str                # "me" or contact name/handle
    sender_handle: str         # raw phone/email handle
    has_attachments: bool
    attachments: list = field(default_factory=list)
    link_preview: Optional[dict] = None
    is_reaction: bool = False
    chat_id: Optional[int] = None
    event_text: Optional[str] = None  # e.g. "Karin added Theo to the conversation"


@dataclass
class Call:
    call_id: object            # int (ZCALLRECORD) or str (ft_/vm_ prefixed)
    address: str               # raw phone/email
    contact_name: str
    date: Optional[str]        # ISO 8601
    duration: Optional[float]  # seconds
    direction: str             # 'incoming' | 'outgoing'
    status: str                # 'answered' | 'missed'
    app: str                   # 'Phone', 'FaceTime Video', 'WhatsApp', etc.


@dataclass
class Note:
    note_id: int
    title: str
    body: str
    created: Optional[str]     # ISO 8601
    modified: Optional[str]    # ISO 8601


@dataclass
class BrowserVisit:
    visit_id: str
    url: str
    title: str
    domain: str
    visit_date: Optional[str]  # ISO 8601
    browser: str               # 'safari' | 'firefox'
    visit_count: Optional[int]


@dataclass
class Contact:
    id: int
    first_name: str
    last_name: str
    display_name: str
    organization: Optional[str]
    phones: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    note: Optional[str] = None
