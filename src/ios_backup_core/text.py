"""
Attributed body parsing and message text cleanup.

Extracted verbatim from messages.py. All parsing logic is battle-tested
against real iPhone backups — do not rewrite.
"""

import plistlib
import re
from typing import Optional

# ---------------------------------------------------------------------------
# Pre-compiled regex patterns (used both in parse_attributed_body and
# clean_message_text — share a single compiled instance for performance)
# ---------------------------------------------------------------------------
_RE_KIMMSG = re.compile(r'__kIM\w+')
_RE_UUID = re.compile(
    r'\$?[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}',
    re.IGNORECASE,
)
_RE_MEDIA_FILE = re.compile(
    r'[\d_A-Fa-f\-]+(\.fullsizerender)*\.(jpeg|jpg|heic|heif|png|gif|mov|mp4|m4a|caf|pdf|doc|docx)',
    re.IGNORECASE,
)
_RE_JUNK_START = re.compile(r'^[ \n"\uFFFD\uFFFC]+')
_RE_JUNK_END = re.compile(r'[ \n"\uFFFD\uFFFC]+$')
_RE_APPLE_CONST = re.compile(r'^k[A-Z][A-Z0-9\-_]{10,}')

# Balloon / data-detector leftovers that spill into the sms.db text column.
# Validated against imessage-exporter vs openextract HTML extracts.
# NOTE: short tags like Time/Date always require a TypedStream prefix or
# trailing slash so we never strip the word "time" from normal sentences.
_RE_DATA_DETECTOR = re.compile(
    r'\[link\]'
    r'|\[?(?:PhoneNumber|PostalAddress|Address|CalendarEvent|FlightInformation)/[^\]\n]*\]?'
    r'|WversionYdd-result'
    r'|XDateTime/'
    r'|TTime/'
    r'|TDate/'
    r'|\\TimeDuration\.?'
    r'|\\DateDuration\.?'
    r'|\^PhysicalAmount/'
    r'|WHttpURL/'
    r'|XAuthCode\.?'
    r'|(?:DateTime|TimeDuration|DateDuration|PhysicalAmount|HttpURL|AuthCode)/',
)

# Junk characters that can appear before a phone number like "(555) 123-4567".
# Only match when followed by '(digits)', so the number's opening '(' is kept.
# e.g. "'()*Z)+X^(555) 123-4567" → "(555) 123-4567"
_RE_PHONE_TYPEDSTREAM_PREFIX = re.compile(
    r"(^|[\s\n])['\"()*+^ZX]+(?=\(\d{2,3}\))",
)

# Junk prefix before a detected date/time/URL span in the SMS text column.
# e.g. "'()*Z)+X3:30 todayXDateTime/" — the real message is usually in attributedBody
_RE_TYPEDSTREAM_DD_PREFIX = re.compile(
    r"^'\(\)\*Z\)\+X[\\\^\[\]]?"
    r"|^\&'\(\)Z\(\*X[\\\^\[\]]?",
)

# ---------------------------------------------------------------------------
# Bundle ID → message type mapping (authoritative column check first)
# ---------------------------------------------------------------------------
_BUNDLE_ID_MAP: list[tuple[str, str]] = [
    ('URLBalloonProvider',              'link'),
    ('Maps',                            'location'),
    ('maps.iMessage',                   'location'),
    ('LocationShare',                   'location'),
    ('findmy',                          'location'),
    ('FindMy',                          'location'),
    ('com.apple.pay',                   'payment'),
    ('PassbookUI',                      'payment'),
    ('DigitalTouch',                    'digital_touch'),
    ('Handwriting',                     'handwriting'),
    ('Fitness',                         'fitness'),
    ('GameCenter',                      'game'),
    ('GameKit',                         'game'),
    # Generic iMessage extension balloon — unknown app share
    ('MSMessageExtensionBalloonPlugin', 'app'),
]


def _type_from_bundle_id(bundle_id: Optional[str]) -> Optional[str]:
    """Map a balloon_bundle_id to a message_type, or None if not set.

    Returns 'app' for any non-empty bundle_id that isn't specifically
    recognised — this prevents garbled extension payload data from
    leaking through as user-visible text.
    """
    if not bundle_id:
        return None
    for fragment, msg_type in _BUNDLE_ID_MAP:
        if fragment in bundle_id:
            return msg_type
    return "app"


# ---------------------------------------------------------------------------
# Attributed-body object scanning
# ---------------------------------------------------------------------------
_BALLOON_TYPE_MAP: list[tuple[str, str]] = [
    ('Maps',                    'location'),
    ('maps.iMessage',           'location'),
    ('LocationShare',           'location'),
    ('__kIMLocationShare',      'location'),
    ('com.apple.pay',           'payment'),
    ('PassbookUI',              'payment'),
    ('DigitalTouch',            'digital_touch'),
    ('Handwriting',             'handwriting'),
    ('Fitness',                 'fitness'),
    ('GameCenter',              'game'),
    ('GameKit',                 'game'),
    ('com.apple.audio',         'audio'),
    ('AudioMessage',            'audio'),
]

_NS_CLASS_NAMES = frozenset([
    "NSString", "NSMutableString", "NSAttributedString",
    "NSMutableAttributedString", "NSObject",
    "NSDictionary", "NSMutableDictionary",
])


def _detect_type_from_objects(objects: list) -> str:
    """Scan bplist $objects for known Apple balloon/system message identifiers."""
    for obj in objects:
        if not isinstance(obj, str):
            continue
        for fragment, msg_type in _BALLOON_TYPE_MAP:
            if fragment in obj:
                return msg_type
    return "text"


# ---------------------------------------------------------------------------
# Core parsing functions — copied verbatim, do not alter logic
# ---------------------------------------------------------------------------

def parse_attributed_body(data: bytes) -> tuple[str, str]:
    """Extract plain text and message type from an NSAttributedString BLOB.

    Returns (text, message_type) where message_type is one of:
      'text', 'location', 'payment', 'audio', 'fitness',
      'game', 'digital_touch', 'handwriting', 'system'

    Copied verbatim from messages.py — battle-tested against real backups.
    """
    if not data:
        return "", "text"

    # 1. Try NSKeyedArchiver (bplist00)
    if data.startswith(b'bplist00'):
        try:
            plist = plistlib.loads(data)
            objects = plist.get("$objects", [])

            # Detect system/service message type first
            msg_type = _detect_type_from_objects(objects)
            if msg_type != "text":
                return "", msg_type

            def _resolve(val):
                if isinstance(val, plistlib.UID):
                    idx = val.data
                    return objects[idx] if idx < len(objects) else None
                return val

            # Primary: follow NSKeyedArchiver structure to the actual NS.string value.
            # $top.root → root NSAttributedString dict → NS.string UID → plain text.
            top = plist.get("$top", {})
            root_ref = top.get("root")
            if root_ref is not None:
                root_obj = _resolve(root_ref)
                if isinstance(root_obj, dict):
                    ns_string_val = _resolve(root_obj.get("NS.string"))
                    if isinstance(ns_string_val, str) and ns_string_val:
                        return ns_string_val, "text"

            # Fallback: longest clean string in $objects (skips internal keys / class names)
            candidate = ""
            for obj in objects:
                if not isinstance(obj, str):
                    continue
                if obj in _NS_CLASS_NAMES:
                    continue
                # Skip NSKeyedArchiver structural strings
                if obj.startswith('$') or obj == '$null':
                    continue
                # Skip NS/CF class name strings (e.g. "NSFont", "WNSValue", "CFString")
                if re.match(r'^W?(NS|CF)[A-Z]', obj):
                    continue
                # Skip GUIDs and file attachment references
                if "kIMFileTransferGUID" in obj or "kIMMessagePart" in obj:
                    continue
                if re.match(
                    r'^\$?[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$',
                    obj, re.IGNORECASE
                ):
                    continue
                if re.search(
                    r'[\d_A-Fa-f\-]+(\.fullsizerender)*\.(jpeg|jpg|heic|heif|png|gif|mov|mp4|m4a|caf|pdf|doc|docx)',
                    obj, re.IGNORECASE
                ):
                    continue
                # Skip Apple internal constant strings (e.g. "kUSD-CAD-AUD-HKD-...")
                if _RE_APPLE_CONST.match(obj):
                    continue
                if len(obj) > len(candidate):
                    candidate = obj
            if candidate:
                return candidate, "text"
        except Exception:
            pass
        # bplist00 data that couldn't be parsed shouldn't be raw-decoded (produces garbage)
        return "", "text"

    # 2. TypedStream / raw binary fallback — also check for system message clues
    try:
        raw = data.decode('utf-8', errors='replace')

        # Quick system-type scan on raw text before cleaning
        for fragment, msg_type in _BALLOON_TYPE_MAP:
            if fragment in raw:
                return "", msg_type

        text = raw
        # Strip TypedStream / NSKeyedArchiver structural noise.
        # ORDER MATTERS: remove full GUIDs and __kIM keys BEFORE $\w+ cleanup,
        # because $\w+ would consume the first GUID segment (e.g. "$19129343")
        # leaving an unrecognisable "-A4D6-…" fragment behind.
        text = re.sub(r'streamtyped', '', text)              # TypedStream magic word
        text = re.sub(r'__kIM\w+', '', text)                 # __kIMFileTransferGUIDAttributeName …
        text = re.sub(r'at_\d+_', '', text)                  # attachment ref prefix "at_0_"
        # Full GUIDs (with or without leading $)
        text = re.sub(
            r'\$?[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}',
            '', text, flags=re.IGNORECASE
        )
        # Partial GUIDs (tail segments left after splitting on control chars)
        text = re.sub(
            r'(?<![.\w])[0-9A-Fa-f]{4,}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{8,}',
            '', text, flags=re.IGNORECASE
        )
        text = re.sub(r'\$\w+', '', text)                    # $classname, $classes, $top …
        text = re.sub(r'W?(NS|CF)[A-Z][A-Za-z]*', '', text) # NSFont, CFString, WNSValue …
        text = re.sub(r'Z?(NS|CF)\.\w+', '', text)          # NS.rangeval, ZNS.special …
        text = re.sub(r'\b[A-Z][a-z]{3,}/', '', text)       # TypedStream class tags: Email/ DateTime/
        text = re.sub(r'mailto:', '', text, flags=re.IGNORECASE)
        text = re.sub(
            r'[\d_A-Fa-f\-]+(\.fullsizerender)*\.(jpeg|jpg|heic|heif|png|gif|mov|mp4|m4a|caf|pdf|doc|docx)',
            '', text, flags=re.IGNORECASE
        )
        for c in _NS_CLASS_NAMES:
            text = text.replace(c, "")

        parts = re.split(r'[\x00-\x08\x0b\x0c\x0e-\x1f]+', text)
        candidate = ""
        for p in parts:
            p = p.strip().replace('\ufffc', '').replace('\ufffd', '').strip()
            # Strip TypedStream string length-prefix artifact: '+' followed by one
            # printable byte that encodes the declared length (e.g. "+I"=73, "+ "=32).
            # Only strip when the declared length closely matches the remaining text.
            m_prefix = re.match(r'^\+([\x20-\x7e])(.*)', p, re.DOTALL)
            if m_prefix:
                declared = ord(m_prefix.group(1))
                remainder = m_prefix.group(2)
                if abs(len(remainder.rstrip()) - declared) <= 3:
                    p = remainder.lstrip()
            # For strings containing an email address, use a regex to extract just
            # the address and discard surrounding TypedStream noise (UEmail/, type bytes).
            # Use lowercase-only TLD ([a-z]{2,}) so uppercase TypedStream type bytes
            # (e.g. the 'U' in 'comUEmail') are not consumed as part of the TLD.
            if '@' in p:
                m_email = re.search(r'[\w._%+\-]+@[\w.\-]+\.[a-z]{2,}', p)
                p = m_email.group(0) if m_email else ''
            if p and len(p) > len(candidate) and len(p) > 3:
                candidate = p

        return candidate, "text"
    except Exception:
        pass

    return "", "text"


def parse_link_payload(data: bytes) -> dict:
    """Extract URL, title, summary and site name from a URLBalloonProvider payload_data blob.

    Copied verbatim from messages.py.
    """
    if not data:
        return {}
    try:
        plist = plistlib.loads(bytes(data))
        objs = plist.get("$objects", [])

        def resolve(val):
            if isinstance(val, plistlib.UID):
                return objs[val.data]
            return val

        root = resolve(objs[1])
        if not isinstance(root, dict) or "richLinkMetadata" not in root:
            return {}

        meta = resolve(root["richLinkMetadata"])
        if not isinstance(meta, dict):
            return {}

        result: dict = {}

        # Resolve NSURL → string via NS.relative
        for key in ("originalURL", "URL"):
            if key in meta:
                url_obj = resolve(meta[key])
                if isinstance(url_obj, dict):
                    rel = resolve(url_obj.get("NS.relative", ""))
                    if rel and isinstance(rel, str):
                        result["url"] = rel
                        break
                elif isinstance(url_obj, str):
                    result["url"] = url_obj
                    break

        for key in ("title", "summary", "siteName"):
            val = resolve(meta.get(key, ""))
            if val and isinstance(val, str):
                result[key.replace("N", "n").replace("S", "s") if key == "siteName" else key] = val

        return result
    except Exception:
        return {}


def has_data_detector_junk(text: str) -> bool:
    """True when text still has tags like DateTime/, [link], or WversionYdd-result."""
    if not text:
        return False
    if text.strip() == "WversionYdd-result":
        return True
    if _RE_TYPEDSTREAM_DD_PREFIX.search(text):
        return True
    return bool(_RE_DATA_DETECTOR.search(text))


def _looks_like_punct_soup(text: str) -> bool:
    """True if text is mostly punctuation/symbols with no spaces — not a real message."""
    s = text.strip()
    if len(s) < 8 or " " in s or "\n" in s:
        return False
    if "://" in s or "@" in s:
        return False
    ascii_punct = sum(1 for ch in s if ord(ch) < 128 and not ch.isalnum() and not ch.isspace())
    alnum = sum(1 for ch in s if ch.isalnum())
    return ascii_punct >= 5 and ascii_punct >= max(1, alnum) * 0.35


def text_looks_contaminated(text: str) -> bool:
    """True when the SMS text column looks corrupted and attributedBody is safer.

    Examples of bad text-column values: ``%&'-./4:>?CKOPQRUXY]U``,
    ``WversionYdd-result``. The real message is often still in attributedBody
    (e.g. ``12 on Friday?``, ``6pm``).
    """
    if not text:
        return False
    if has_data_detector_junk(text):
        return True
    if _RE_PHONE_TYPEDSTREAM_PREFIX.search(text):
        return True
    return _looks_like_punct_soup(text)


def clean_message_text(text: str) -> str:
    """Clean raw message text: strip object replacement chars, Apple internal
    identifiers, UUIDs, media filenames, junk characters, TypedStream
    string-length prefix artifacts, and data-detector leftovers.

    Note: if the text column is only junk (``WversionYdd-result``,
    ``$%&,-.39=>…``), this cannot recover the real message. Callers should
    check ``text_looks_contaminated`` and use attributedBody instead.
    """
    if not text:
        return text

    # Entire-column stubs / soup — not recoverable from this column.
    if text.strip() == "WversionYdd-result" or _looks_like_punct_soup(text):
        return ""

    # Strip object replacement / replacement characters and trim
    text = text.replace('\ufffc', '').replace('\ufffd', '').strip()
    text = _RE_KIMMSG.sub('', text)
    text = _RE_UUID.sub('', text)
    text = _RE_MEDIA_FILE.sub('', text)

    # Strip TypedStream data-detector wrappers before generic detector cleanup
    text = _RE_TYPEDSTREAM_DD_PREFIX.sub('', text)
    text = _RE_DATA_DETECTOR.sub('', text)

    # Remove junk wrapper quotes, spaces, or newlines left from stripping
    text = _RE_JUNK_START.sub('', text)
    text = _RE_JUNK_END.sub('', text)
    text = text.strip()

    # Strip TypedStream string-length prefix artifact from raw text column.
    # Format: '+' followed by one printable ASCII byte whose ordinal encodes
    # the declared string length, e.g. "+*I'll call you later" where
    # '*'=chr(42) declares length 42.
    # Apple stores NSString lengths in different units depending on context:
    #   - Python len()       : Unicode codepoints
    #   - UTF-8 byte count   : e.g. ASCII chars with a few CJK/emoji bumps
    #   - UTF-16 code units  : non-BMP emoji (😘) each cost 2 units here
    # Checking all three representations with a ±8 byte tolerance catches
    # the full range of real-world messages (emoji, accented chars, etc.)
    # while keeping false-positive risk low (ordinary "+word" text would
    # need to be within 8 chars of the ASCII value of the letter after +).
    _m = re.match(r'^\+([\x20-\x7e])(.*)', text, re.DOTALL)
    if _m:
        _declared = ord(_m.group(1))
        _remainder = _m.group(2).lstrip()
        _rs = _remainder.rstrip()
        _lens = (
            len(_rs),
            len(_rs.encode('utf-8')),
            len(_rs.encode('utf-16-le')) // 2,
        )
        if any(abs(l - _declared) <= 8 for l in _lens):
            text = _remainder

    # TypedStream residue that sits immediately before an embedded phone number
    text = _RE_PHONE_TYPEDSTREAM_PREFIX.sub(r'\1', text)

    # Collapse whitespace left after stripping tags (keep intentional newlines)
    text = re.sub(r'[ \t]+\n', '\n', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = text.strip()

    if text and _looks_like_punct_soup(text):
        return ""

    return text
