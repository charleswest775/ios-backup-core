"""Database writers. Each fills one harvested schema from a persona snapshot."""

COCOA_EPOCH = 978_307_200  # 2001-01-01 UTC in Unix seconds


def cocoa_seconds(moment) -> float:
    return moment.timestamp() - COCOA_EPOCH


def cocoa_nanoseconds(moment) -> int:
    return int(round((moment.timestamp() - COCOA_EPOCH) * 1_000_000_000))
