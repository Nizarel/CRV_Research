import re

_RANGE = re.compile(r"bytes=(\d*)-(\d*)")


class UnsatisfiableRange(ValueError):
    pass


def byte_range(header: str | None, size: int) -> tuple[int, int] | None:
    """Half-open byte span for a single ``Range: bytes=`` request; None serves the whole file.

    Malformed or multi-range headers are ignored (RFC 9110 allows serving the full body).
    """
    match = _RANGE.fullmatch((header or "").strip())
    if match is None:
        return None
    first, last = match.groups()
    if first == "" and last == "":
        return None
    if first == "":
        length = int(last)
        if length == 0:
            raise UnsatisfiableRange
        return max(size - length, 0), size
    start = int(first)
    end = size if last == "" else min(int(last) + 1, size)
    if start >= size or end <= start:
        raise UnsatisfiableRange
    return start, end
