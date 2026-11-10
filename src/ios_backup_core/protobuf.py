"""
Protobuf wire-format decoding.

extract_protobuf_strings() collects loose strings. It was extracted from
notes.py:NoteExtractor._extract_pb_strings() with its logic unchanged
(battle-tested against Notes gzip+proto blobs).

parse_fields() and its helpers decode field by field, for blobs where the field
number says what a value means (WhatsApp's replies, calls and reactions).
"""


def extract_protobuf_strings(data: bytes, depth: int = 0) -> list[str]:
    """Walk a protobuf blob and collect all UTF-8 string field values.

    Recursively follows length-delimited (wire type 2) fields so nested
    messages are also searched. Non-UTF-8 chunks are treated as nested
    protobuf messages and recursed into.

    Args:
        data:  Raw protobuf bytes (already decompressed if gzip-wrapped).
        depth: Recursion guard — stops at depth 8 to avoid runaway recursion.

    Returns:
        List of decoded UTF-8 strings containing meaningful alphabetic content
        (at least 3 alpha characters after stripping whitespace).
    """
    if depth > 8:
        return []

    strings: list[str] = []
    pos = 0
    data_len = len(data)

    while pos < data_len:
        # Decode tag varint
        tag = 0
        shift = 0
        valid = False
        while pos < data_len:
            b = data[pos]; pos += 1  # noqa: E702
            tag |= (b & 0x7F) << shift
            if not (b & 0x80):
                valid = True
                break
            shift += 7
            if shift >= 64:
                return strings
        if not valid:
            break

        wire_type = tag & 0x7

        if wire_type == 0:  # varint — skip
            while pos < data_len:
                b = data[pos]; pos += 1  # noqa: E702
                if not (b & 0x80):
                    break
        elif wire_type == 1:  # 64-bit — skip
            pos += 8
        elif wire_type == 2:  # length-delimited
            length = 0
            shift = 0
            valid = False
            while pos < data_len:
                b = data[pos]; pos += 1  # noqa: E702
                length |= (b & 0x7F) << shift
                if not (b & 0x80):
                    valid = True
                    break
                shift += 7
                if shift >= 64:
                    return strings
            if not valid or length < 0 or pos + length > data_len:
                break

            chunk = data[pos:pos + length]
            pos += length

            try:
                text = chunk.decode('utf-8')
                # Keep if it contains meaningful alphabetic content
                stripped = text.strip()
                if stripped and sum(1 for c in stripped if c.isalpha()) > 2:
                    strings.append(text)
                else:
                    # Short/non-alpha chunk may be a nested message — recurse
                    strings.extend(extract_protobuf_strings(chunk, depth + 1))
            except UnicodeDecodeError:
                # Binary data — try as nested protobuf
                strings.extend(extract_protobuf_strings(chunk, depth + 1))
        elif wire_type == 5:  # 32-bit — skip
            pos += 4
        else:
            break

    return strings


# ── Field-aware decoding ─────────────────────────────────────────────────────
#
# extract_protobuf_strings() above throws away field numbers and every number,
# which is fine for Notes text but not for blobs where the field says what a
# value means (WhatsApp's reply, call and reaction records). parse_fields()
# keeps both. It never guesses whether a length-delimited value is a string or
# a nested message: callers descend into the fields they know are messages.

WIRE_VARINT = 0
WIRE_FIXED64 = 1
WIRE_LEN = 2
WIRE_FIXED32 = 5


class ProtobufError(ValueError):
    """The bytes are not a well-formed protobuf message."""


def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while True:
        if pos >= len(data):
            raise ProtobufError("truncated varint")
        b = data[pos]
        pos += 1
        value |= (b & 0x7F) << shift
        if not (b & 0x80):
            return value, pos
        shift += 7
        if shift >= 70:
            raise ProtobufError("varint too long")


def parse_fields(data: bytes) -> list[tuple[int, int, object]]:
    """Decode one protobuf message into ``(field_number, wire_type, value)`` tuples, in order.

    Varints come back as ``int``, length-delimited values as ``bytes`` (decode or
    parse them yourself), fixed32/fixed64 as their raw little-endian ``bytes``.
    Repeated fields appear once per occurrence.

    Raises ProtobufError on anything malformed (truncation, field number 0, the
    deprecated group wire types 3/4, or wire types 6/7), so a blob that is not
    protobuf at all is rejected rather than half-read.
    """
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise ProtobufError("not bytes")
    data = bytes(data)
    fields: list[tuple[int, int, object]] = []
    pos = 0
    end = len(data)
    while pos < end:
        tag, pos = _read_varint(data, pos)
        number, wire = tag >> 3, tag & 0x7
        if number == 0:
            raise ProtobufError("field number 0")
        if wire == WIRE_VARINT:
            value, pos = _read_varint(data, pos)
        elif wire == WIRE_LEN:
            length, pos = _read_varint(data, pos)
            if pos + length > end:
                raise ProtobufError("length past end")
            value = data[pos:pos + length]
            pos += length
        elif wire == WIRE_FIXED64:
            if pos + 8 > end:
                raise ProtobufError("truncated fixed64")
            value = data[pos:pos + 8]
            pos += 8
        elif wire == WIRE_FIXED32:
            if pos + 4 > end:
                raise ProtobufError("truncated fixed32")
            value = data[pos:pos + 4]
            pos += 4
        else:
            raise ProtobufError(f"unsupported wire type {wire}")
        fields.append((number, wire, value))
    return fields


def first_field(fields: list[tuple[int, int, object]], number: int, wire: int | None = None):
    """The value of the first occurrence of ``number`` (optionally of one wire type), or None."""
    for n, w, v in fields:
        if n == number and (wire is None or w == wire):
            return v
    return None


def all_fields(fields: list[tuple[int, int, object]], number: int, wire: int | None = None) -> list:
    """Every value of ``number`` (optionally of one wire type), in order."""
    return [v for n, w, v in fields if n == number and (wire is None or w == wire)]


def sub_message(fields: list[tuple[int, int, object]], number: int) -> list[tuple[int, int, object]] | None:
    """Parse the first length-delimited ``number`` as a nested message; None when absent or malformed."""
    raw = first_field(fields, number, WIRE_LEN)
    if raw is None:
        return None
    try:
        return parse_fields(raw)
    except ProtobufError:
        return None


def field_text(fields: list[tuple[int, int, object]], number: int) -> str | None:
    """The first length-delimited ``number`` decoded as UTF-8, or None when absent or not text."""
    raw = first_field(fields, number, WIRE_LEN)
    if raw is None:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def field_int(fields: list[tuple[int, int, object]], number: int) -> int | None:
    """The first varint ``number``, or None."""
    return first_field(fields, number, WIRE_VARINT)
