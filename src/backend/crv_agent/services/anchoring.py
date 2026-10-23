"""Re-anchor worker claims on the exact text shown to the worker, before validation.

The extractor sometimes copies a quote with other whitespace or typography, cites a passage ID
other than the one holding the quote, joins several shown lines into one quote, or sets a
calculation index on a plain value. Each repair only replaces the quote with an exact span of
shown text (or narrows it to one shown line that prints the value) and rebinds the evidence IDs
to passages that contain it. ``claim_errors`` still decides acceptance, unchanged.
"""

import re
import unicodedata
from collections.abc import Sequence

from crv_agent.domain.evidence import Claim, Evidence
from crv_agent.services.evidence_pack import exposed_text
from crv_agent.services.verification import value_stated

_PUNCTUATION = str.maketrans({
    "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u00ab": '"', "\u00bb": '"',
    "\u2013": "-", "\u2014": "-", "\u2011": "-", "\u2212": "-", "\u00ad": None,
})
_FRAGMENTS = re.compile(r"\s*(?:\n+|…|\.\.\.)\s*")
MIN_FRAGMENT = 8


def _normalized(text: str) -> tuple[str, list[int]]:
    """Normalized text (NFKC, ASCII punctuation, single spaces) and each character's origin."""
    chars: list[str] = []
    origin: list[int] = []
    for index, char in enumerate(text):
        for folded in unicodedata.normalize("NFKC", char).translate(_PUNCTUATION):
            if folded.isspace():
                if chars and chars[-1] != " ":
                    chars.append(" ")
                    origin.append(index)
            else:
                chars.append(folded)
                origin.append(index)
    return "".join(chars), origin


def locate(quote: str, text: str) -> str | None:
    """The exact span of ``text`` that equals ``quote`` once both are normalized."""
    if quote and quote in text:
        return quote
    needle = _normalized(quote)[0].strip()
    haystack, origin = _normalized(text)
    at = haystack.find(needle) if needle else -1
    if at < 0:
        return None
    return text[origin[at]:origin[at + len(needle) - 1] + 1]


def anchor_claim(claim: Claim, shown: Sequence[Evidence]) -> Claim:
    """The claim quoted exactly from, and bound to, the passages that state it."""
    if claim.calculation_index is not None:
        # Worker claims are never derived; derived values come from the calculator only.
        claim = claim.model_copy(update={"calculation_index": None})
    texts = [(item.id, text) for item in shown if not item.generated for text in exposed_text(item)]
    candidates = [claim.quote]
    if claim.value is not None:
        candidates += [part for part in _FRAGMENTS.split(claim.quote) if len(part) >= MIN_FRAGMENT]
    for candidate in candidates:
        found = [(pid, span) for pid, text in texts if (span := locate(candidate, text))]
        if not found:
            continue
        span = found[0][1]
        if claim.value is not None and not value_stated(span, claim.value, claim.qualifier):
            continue
        holders = [pid for pid, other in found if other == span]
        ids = [cid for cid in claim.evidence_ids if cid in holders] or holders[:1]
        return claim.model_copy(update={"quote": span, "evidence_ids": ids})
    return claim
