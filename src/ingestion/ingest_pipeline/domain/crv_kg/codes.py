"""CRV report codes split into segments: ``CR/<s2>/<s3>/<type>/<seq>/<yy>`` and variants.

Seen in the corpus: ``CR/AR/MC/RP/24/10`` (5 segments), ``CR/AA/RP/001/24`` (no s3),
``CR-AR-I-01.00-05`` (procedure), ``CR-AR-PE-01-15`` (no type). Segment meanings (``AR``,
``AF``; site codes such as ``TZ``) are unconfirmed and resolved through the gazetteer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..crv import report_codes

DOCUMENT_TYPES = frozenset({"RP", "PV", "I", "P", "PR", "NT", "CR", "FT", "ET", "PS", "MO"})
_SPLIT = re.compile(r"[-/]")


@dataclass(frozen=True)
class ReportCode:
    code: str
    """Normalised: upper case, ``/`` separators."""
    segments: tuple[str, ...]
    s2: str | None
    s3: str | None
    doc_type: str | None
    seq: str | None
    yy: int | None

    @property
    def year(self) -> int | None:
        if self.yy is None:
            return None
        return 1900 + self.yy if self.yy >= 50 else 2000 + self.yy


def parse_code(raw: str) -> ReportCode | None:
    """A printed code, or None unless it has CR, a 2-letter activity and at least 2 more parts."""

    found = report_codes(raw.replace("_", "-").replace(" ", ""))
    if not found:
        return None
    segments = tuple(part for part in _SPLIT.split(found[0])[1:] if part)
    if len(segments) < 3 or not re.fullmatch(r"[A-Z]{2}", segments[0]):
        return None
    code = "CR/" + "/".join(segments)
    yy = int(segments[-1]) if re.fullmatch(r"\d{2}", segments[-1]) else None
    body = segments[1:-1] if yy is not None else segments[1:]
    type_index = next((i for i, part in enumerate(body) if part in DOCUMENT_TYPES), None)
    if type_index is not None:
        doc_type = body[type_index]
        s3 = body[0] if type_index >= 1 else None
        rest = body[type_index + 1:]
    else:
        doc_type = None
        s3 = body[0] if len(body) >= 2 else None
        rest = body[1:] if len(body) >= 2 else body
    seq = rest[0] if rest else None
    return ReportCode(code, segments, segments[0], s3, doc_type, seq, yy)


def codes_in(text: str) -> list[ReportCode]:
    parsed = (parse_code(code) for code in report_codes(text))
    return list({code.code: code for code in parsed if code is not None}.values())
