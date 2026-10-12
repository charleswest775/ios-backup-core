"""
Attributed body parsing and message text cleanup.

``attributedBody`` TypedStream blobs are deserialized with ``pytypedstream``;
bplist NSKeyedArchiver uses ``plistlib``.
"""

import plistlib
import re
from typing import Optional

from typedstream.stream import TypedStreamReader

# ---------------------------------------------------------------------------
# Pre-compiled regex patterns for clean_message_text
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


def _parse_typedstream_pytypedstream(data: bytes) -> Optional[str]:
    """Read a TypedStream blob with pytypedstream and return the message text.

    Uses the first string payload from the stream (the body). Later strings are
    usually internal names such as ``__kIM...``, not user-visible text.
    """
    try:
        for event in TypedStreamReader.from_data(data):
            if type(event) is not bytes:
                continue
            try:
                text = event.decode("utf-8")
            except UnicodeDecodeError:
                text = event.decode("utf-8", "replace")
            if not text or text.startswith("__kIM"):
                continue
            # Skip Objective-C class names (NSString, WNSValue, …). Require no
            # whitespace so real messages like "CFO said yes" are kept.
            if text in _NS_CLASS_NAMES or (
                " " not in text and re.match(r"^W?(NS|CF)[A-Z]", text)
            ):
                continue
            return text
    except Exception:
        return None
    return None


# ---------------------------------------------------------------------------
# Core parsing functions
# ---------------------------------------------------------------------------

def parse_attributed_body(data: bytes) -> tuple[str, str]:
    """Extract plain text and message type from an NSAttributedString BLOB.

    Parsing is attempted via binary plist (NSKeyedArchiver) first, then TypedStream via
    pytypedstream. 

    Returns (text, message_type) where message_type is one of:
      'text', 'location', 'payment', 'audio', 'fitness',
      'game', 'digital_touch', 'handwriting', 'system'.
      
    If parsing fails return `("", "text")`.
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
        except Exception:
            pass
        # bplist00 data that couldn't be parsed shouldn't be raw-decoded (produces garbage)
        return "", "text"

    # 2. TypedStream via pytypedstream
    try:
        raw = data.decode('utf-8', errors='replace')
        for fragment, msg_type in _BALLOON_TYPE_MAP:
            if fragment in raw:
                return "", msg_type

        archived = _parse_typedstream_pytypedstream(data)
        if archived is not None:
            return archived, "text"
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


def clean_message_text(text: str) -> str:
    """Clean raw message text: strip object replacement chars, Apple internal
    identifiers, UUIDs, media filenames, junk characters, and TypedStream
    string-length prefix artifacts.

    Extracted from the inline cleanup block in messages.py:get_messages().
    Logic is unchanged — do not rewrite.
    """
    if not text:
        return text

    # Strip object replacement / replacement characters and trim
    text = text.replace('\ufffc', '').replace('\ufffd', '').strip()
    before = text
    text = _RE_KIMMSG.sub('', text)
    text = _RE_UUID.sub('', text)
    text = _RE_MEDIA_FILE.sub('', text)

    # Remove junk wrapper quotes left from stripping identifiers. Only when
    # something was stripped: otherwise a message that simply starts or ends
    # with a quotation mark (He said "yes") would lose it.
    if text != before:
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

    return text
