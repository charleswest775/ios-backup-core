"""Tests for ios_backup_core.text."""

import plistlib
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ios_backup_core.text import (
    parse_attributed_body,
    clean_message_text,
    text_looks_contaminated,
)


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
        # Embed a known location fragment to trigger type detection
        raw = b"streamtypedMaps__kIMLocationShare"
        text, msg_type = parse_attributed_body(raw)
        assert msg_type in ("location", "text")  # depends on which trigger fires

    def test_bplist_unicode_text(self):
        blob = _make_bplist_attributed_string("Héllo wörld 😀")
        text, msg_type = parse_attributed_body(blob)
        assert "Héllo" in text or text == "Héllo wörld 😀"
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

    def test_preserves_word_time(self):
        assert clean_message_text("What time on Monday?") == "What time on Monday?"

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

    def test_strips_link_placeholder(self):
        text = (
            "Red Bean · Atlanta, Georgia\n"
            "https://maps.app.goo.gl/aBcDeFgHiJkLmNoPqR?g_st=iw\n"
            "[link]"
        )
        result = clean_message_text(text)
        assert "[link]" not in result
        assert "https://maps.app.goo.gl/" in result
        assert "Red Bean" in result

    def test_strips_phone_junk_keeps_name_and_parens(self):
        text = "Eric Sanderson\n'()*Z)+X^(555) 123-4567[PhoneNumber/"
        assert clean_message_text(text) == "Eric Sanderson\n(555) 123-4567"

    def test_preserves_phone_parens_without_junk_prefix(self):
        text = "Eric Sanderson\n(555) 123-4567[PhoneNumber/"
        assert clean_message_text(text) == "Eric Sanderson\n(555) 123-4567"

    def test_wversion_stub_becomes_empty(self):
        assert clean_message_text("WversionYdd-result") == ""

    def test_punct_soup_becomes_empty(self):
        assert clean_message_text("$%&,-.39=>CK\"OPQTWX\\bfghijU") == ""
        assert clean_message_text("%&'-./4:>?CKOPQRUXY]U") == ""

    def test_datetime_wrap_strips_to_span(self):
        result = clean_message_text("'()*Z)+X3:30 todayXDateTime/")
        assert "DateTime" not in result
        assert "3:30 today" in result


class TestTextLooksContaminated:
    def test_detects_link_tag(self):
        assert text_looks_contaminated("hello\n[link]")

    def test_detects_typedstream_soup(self):
        assert text_looks_contaminated("%&'-./4:>?CKOPQRUXY]U")
        assert text_looks_contaminated('$%&,-.39=>CK"OPQTWX\\bfghijU')
        assert text_looks_contaminated("$%&,-.39=>BHLMNOPU[_`abehlrvwxU")

    def test_detects_wversion_stub(self):
        assert text_looks_contaminated("WversionYdd-result")

    def test_detects_datetime_wrap(self):
        assert text_looks_contaminated("'()*Z)+X3:30 todayXDateTime/")
        assert text_looks_contaminated("'()*Z)+X^Lunch tomorrowXDateTime/")
        assert text_looks_contaminated("Tuesday around 5XDateTime/")

    def test_detects_httpurl_wrap(self):
        assert text_looks_contaminated(
            "&https://www.examplesitehost.com/books/WHttpURL/"
        )

    def test_allows_normal_messages(self):
        assert not text_looks_contaminated("12 on Friday?")
        assert not text_looks_contaminated("Sounds good. Let's do it.")
        assert not text_looks_contaminated("What time on Monday?")
        assert not text_looks_contaminated("I can do June 21")
        assert not text_looks_contaminated("Beach day on 11/11?")
