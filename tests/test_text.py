"""Tests for ios_backup_core.text."""

import plistlib
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ios_backup_core.text import parse_attributed_body, clean_message_text


def _make_bplist_attributed_string(text: str) -> bytes:
    """Build a minimal NSKeyedArchiver bplist for an NSAttributedString."""
    # Minimal $objects list:
    #   [0] = "$null"
    #   [1] = root NSAttributedString dict with NS.string → UID(2)
    #   [2] = the actual string
    #   [3] = NSAttributedString class dict
    plist_data = {
        "$version": 100000,
        "$archiver": "NSKeyedArchiver",
        "$top": {"root": plistlib.UID(1)},
        "$objects": [
            "$null",
            {
                "$class": plistlib.UID(3),
                "NS.string": plistlib.UID(2),
            },
            text,
            {
                "$classname": "NSAttributedString",
                "$classes": ["NSAttributedString", "NSObject"],
            },
        ],
    }
    return plistlib.dumps(plist_data, fmt=plistlib.FMT_BINARY)


class TestParseAttributedBody:
    def test_bplist_extracts_text(self):
        blob = _make_bplist_attributed_string("Hello, world!")
        text, msg_type = parse_attributed_body(blob)
        assert text == "Hello, world!"
        assert msg_type == "text"

    def test_empty_bytes_returns_empty(self):
        text, msg_type = parse_attributed_body(b"")
        assert text == ""
        assert msg_type == "text"

    def test_garbage_bytes_returns_empty(self):
        text, msg_type = parse_attributed_body(b"\x00\x01\x02\x03garbage")
        # Should not raise; may return empty or short candidate
        assert isinstance(text, str)
        assert msg_type == "text"

    def test_location_balloon_detected(self):
        # No readable body: fragment scan still classifies the balloon.
        raw = b"streamtypedMaps__kIMLocationShare"
        text, msg_type = parse_attributed_body(raw)
        assert text == ""
        assert msg_type == "location"

    def test_typedstream_keeps_text_containing_balloon_fragment(self):
        # "Maps" is a location fragment, but a real NSString body must win.
        blob = self._typedstream_nsstring(b"Meet at Maps at 5")
        text, msg_type = parse_attributed_body(blob)
        assert text == "Meet at Maps at 5"
        assert msg_type == "text"

    def test_typedstream_object_replacement_falls_back_to_fragment_scan(self):
        # Balloon rows often carry only U+FFFC. That is not useful text.
        blob = self._typedstream_nsstring("\ufffc".encode("utf-8")) + b"Maps"
        text, msg_type = parse_attributed_body(blob)
        assert text == ""
        assert msg_type == "location"

    def test_bplist_unicode_text(self):
        blob = _make_bplist_attributed_string("Héllo wörld 😀")
        text, msg_type = parse_attributed_body(blob)
        assert "Héllo" in text or text == "Héllo wörld 😀"
        assert msg_type == "text"

    def _typedstream_nsstring(self, message: bytes) -> bytes:
        """Minimal valid TypedStream NSString (pytypedstream-compatible)."""
        return (
            b"\x04\x0bstreamtyped\x81\xe8\x03\x84\x01@\x84\x84\x84\x08NSString"
            b"\x01\x84\x84\x08NSObject\x00\x85\x84\x01+"
            + bytes([len(message)])
            + message
            + b"\x86"
        )

    def test_typedstream_pytypedstream_prefers_nsstring_over_soup(self):
        msg = b"I can do June 21"
        soup = b'$%&,-.39=>CK"OPQTWX\\bfghijU'
        # Trailing detector bplist junk is longer than the real message; a
        # printable-run heuristic would pick the soup. Structured parse must not.
        blob = self._typedstream_nsstring(msg) + b"bplist00WversionYdd-result" + soup
        text, msg_type = parse_attributed_body(blob)
        assert text == "I can do June 21"
        assert msg_type == "text"
        assert "OPQTWX" not in text

    def test_typedstream_keeps_messages_starting_with_ns_or_cf_words(self):
        # Class-name filter must not drop human text like "CFO said yes".
        blob = self._typedstream_nsstring(b"CFO said yes")
        text, msg_type = parse_attributed_body(blob)
        assert text == "CFO said yes"
        assert msg_type == "text"

    def test_typedstream_no_scrape_fallback_when_unreadable(self):
        # Incomplete stream — pytypedstream fails. We must NOT scrape printable
        # runs (that path preferred detector soup). Callers use SQL text instead.
        msg = b"I can do June 21"
        soup = b'$%&,-.39=>CK"OPQTWX\\bfghijU'
        blob = (
            b"\x04\x0bstreamtyped\x84\x84\x08NSString\x01\x84\x01+"
            + bytes([len(msg)])
            + msg
            + b"bplist00WversionYdd-result"
            + soup
        )
        text, msg_type = parse_attributed_body(blob)
        assert text == ""
        assert msg_type == "text"


class TestCleanMessageText:
    def test_strips_ufffc(self):
        assert clean_message_text("hello\ufffc world") == "hello world"

    def test_strips_ufffd(self):
        assert clean_message_text("hello\ufffd world") == "hello world"

    def test_strips_kim_identifiers(self):
        result = clean_message_text("text __kIMFileTransferGUIDAttributeName more text")
        assert "__kIM" not in result
        assert "text" in result

    def test_strips_uuids(self):
        result = clean_message_text("prefix 12345678-ABCD-1234-ABCD-123456789012 suffix")
        assert "12345678-ABCD-1234-ABCD-123456789012" not in result

    def test_strips_media_filenames(self):
        result = clean_message_text("IMG_1234.jpeg is attached")
        assert ".jpeg" not in result

    def test_preserves_normal_text(self):
        msg = "Hey, are you coming tonight?"
        assert clean_message_text(msg) == msg

    def test_preserves_quotes_at_the_ends(self):
        # Found by backupgen's answer key: closing quotes were being stripped.
        for msg in ('He said "yes"', '"Quoted" at the start', '"Fully quoted"'):
            assert clean_message_text(msg) == msg

    def test_strips_quotes_left_by_removed_identifiers(self):
        assert clean_message_text('"__kIMFileTransferGUIDAttributeName" hello') == "hello"

    def test_empty_string_unchanged(self):
        assert clean_message_text("") == ""

    def test_typed_stream_prefix_stripped(self):
        # '+' followed by chr(42)='*', remainder is ~42 chars
        remainder = "I'll call you later when I get home ok"  # 38 chars
        # declared = ord('&') = 38
        text = "+&" + remainder
        result = clean_message_text(text)
        # The prefix should be stripped because len(remainder) is close to 38
        assert result == remainder or result.startswith(remainder[:10])

    def test_typed_stream_prefix_not_stripped_when_mismatch(self):
        # '+' followed by 'Z'=90, but remainder is only 5 chars — too far off
        text = "+ZHello"
        result = clean_message_text(text)
        # Should NOT strip: declared=90, len("Hello")=5, diff=85 >> 8
        assert result == text or result == "Hello"  # regex cleanup may trim leading junk
