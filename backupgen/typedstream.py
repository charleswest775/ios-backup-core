"""Encode Messages' ``attributedBody`` column: an NSAttributedString in Apple's typedstream format.

The layout is the one real iOS 15–17 databases use for a plain text message (a
single run carrying ``__kIMMessagePartAttributeName = 0``). Only two numbers
vary with the text: its UTF-8 byte length and its UTF-16 length, both written
as typedstream integers.
"""

from __future__ import annotations

import struct

_HEAD = bytes.fromhex(
    "040b73747265616d747970656481e803840140848484124e534174747269627574656453747269"
    "6e67008484084e534f626a656374008592848484084e53537472696e67019484012b"
)
_RUN = bytes.fromhex("8684026949") + b"\x01"
_TAIL = bytes.fromhex(
    "928484840c4e5344696374696f6e61727900948401690192849696"  # NSDictionary, one entry
    "1d5f5f6b494d4d657373616765506172744174747269627574654e616d65"  # "__kIMMessagePartAttributeName"
    "8692848484084e534e756d626572008484074e5356616c7565009484012a84999900"  # NSNumber 0
    "868686"
)


def _integer(value: int) -> bytes:
    """typedstream integer: one byte below 0x80, else a tagged little-endian short or int."""
    if 0 <= value < 0x80:
        return bytes([value])
    if value < 0x8000:
        return b"\x81" + struct.pack("<h", value)
    return b"\x82" + struct.pack("<i", value)


def attributed_body(text: str) -> bytes:
    data = text.encode("utf-8")
    utf16_length = len(text.encode("utf-16-le")) // 2
    return _HEAD + _integer(len(data)) + data + _RUN + _integer(utf16_length) + _TAIL
