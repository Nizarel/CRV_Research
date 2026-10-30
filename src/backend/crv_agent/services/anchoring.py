"""Re-anchor worker claims on the exact text shown to the worker, before validation.

The extractor sometimes copies a quote with other whitespace, case or typography (or table cell
separators), copies the JSON escapes of the passage payload (a literal ``\\n``), cites a passage
ID other than the one holding the quote, joins several shown lines into one quote or elides text
with "[...]", or sets a calculation index on a plain value. Each repair only replaces the quote
with an exact span of shown text (or narrows it to one shown line that prints the value) and
rebinds the evidence IDs to passages that contain it. A date whose year no cited passage prints
(a filename year) is cleared rather than failing the claim, and so is a qualifier (``>``, ``~``)
the quoted span does not print when it prints the bare value.
A quote in the leading fragment of a chunk that continues a sentence from the preceding page
also cites that preceding chunk, so the subject named before the page break stays checkable.
``claim_errors`` still decides acceptance, unchanged.
"""

import re
import unicodedata
from collections.abc import Sequence

from crv_agent.domain.evidence import Claim, Evidence
from crv_agent.services.evidence_pack import exposed_spans, exposed_text
from crv_agent.services.verification import stated

_PUNCTUATION = str.maketrans({
    "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u00ab": '"', "\u00bb": '"',
    "\u2013": "-", "\u2014": "-", "\u2011": "-", "\u2212": "-", "\u00ad": None,
})
_FRAGMENTS = re.compile(r"\s*(?:\n+|\[\s*(?:…|\.\.\.)\s*\]|…|\.\.\.)\s*")
_ELLIPSIS = re.compile(r"\s*(?:\[\s*(?:…|\.\.\.)\s*\]|…|\.\.\.)\s*")
_OCR_MARKS = re.compile(r":(?:un)?selected:")
_ESCAPE = re.compile(r"\\(u[0-9a-fA-F]{4}|[nrt\"\\/])")
_ESCAPES = {"n": "\n", "r": "\r", "t": "\t", '"': '"', "\\": "\\", "/": "/"}
_YEAR = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")
_SENTENCE_END = re.compile(r"[.!?](?=\s|$)|\n\s*\n")
MIN_FRAGMENT = 8
# Longest source span an elided ("[...]") quote may be widened to.
MAX_STITCH = 800


def _normalized(text: str) -> tuple[str, list[int]]:
    """Normalized text (NFKC, lower case, ASCII punctuation, table pipes and OCR marks as single
    spaces) and each character's origin."""
    chars: list[str] = []
    origin: list[int] = []
    for index, char in enumerate(_OCR_MARKS.sub(lambda m: " " * len(m[0]), text)):
        for folded in unicodedata.normalize("NFKC", char).translate(_PUNCTUATION).lower():
            if folded.isspace() or folded == "|":
                if chars and chars[-1] != " ":
                    chars.append(" ")
                    origin.append(index)
            else:
                chars.append(folded)
                origin.append(index)
    return "".join(chars), origin


def unescape(text: str) -> str:
    """JSON escape sequences copied literally from the JSON passage payload, decoded."""
    def replace(match: re.Match[str]) -> str:
        code = match[1]
        return chr(int(code[1:], 16)) if code[0] == "u" else _ESCAPES[code]

    return _ESCAPE.sub(replace, text)


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


def stitch(quote: str, text: str) -> str | None:
    """The exact span of ``text`` holding a quote's elided fragments, in order and nearby."""
    parts = [_normalized(part)[0].strip() for part in _ELLIPSIS.split(quote)]
    if len(parts) < 2 or any(len(part) < MIN_FRAGMENT for part in parts):
        return None
    haystack, origin = _normalized(text)
    start = haystack.find(parts[0])
    while start >= 0:
        end = start + len(parts[0])
        for part in parts[1:]:
            at = haystack.find(part, end)
            if at < 0 or at + len(part) - start > MAX_STITCH:
                break
            end = at + len(part)
        else:
            return text[origin[start]:origin[end - 1] + 1]
        start = haystack.find(parts[0], start + 1)
    return None


def anchor_claim(claim: Claim, shown: Sequence[Evidence]) -> Claim:
    """The claim quoted exactly from, and bound to, the passages that state it."""
    claim = _continuation(_anchor(claim, shown), shown)
    years = _YEAR.findall(claim.date or "")
    texts = [
        text for item in shown if item.id in claim.evidence_ids and not item.generated
        for text in exposed_text(item)
    ]
    if any(not any(year in text for text in texts) for year in years):
        return claim.model_copy(update={"date": None})
    return claim


def _printed(claim: Claim, span: str) -> Claim | None:
    """The claim as ``span`` prints it: unchanged, or without a qualifier the span does not
    print when it prints the bare value; None when the span does not state the value."""
    if claim.value is None or stated(claim, span):
        return claim
    bare = claim.model_copy(update={"qualifier": None})
    return bare if claim.qualifier is not None and stated(bare, span) else None


def _anchor(claim: Claim, shown: Sequence[Evidence]) -> Claim:
    if claim.calculation_index is not None:
        # Worker claims are never derived; derived values come from the calculator only.
        claim = claim.model_copy(update={"calculation_index": None})
    texts = [(item.id, text) for item in shown if not item.generated for text in exposed_text(item)]
    quotes = list(dict.fromkeys((claim.quote, unescape(claim.quote))))
    candidates = list(quotes)
    if claim.value is not None:
        candidates += [
            part for quote in quotes for part in _FRAGMENTS.split(quote)
            if len(part) >= MIN_FRAGMENT
        ]
    for candidate in candidates:
        found = [(pid, span) for pid, text in texts if (span := locate(candidate, text))]
        if not found:
            continue
        span = found[0][1]
        printed = _printed(claim, span)
        if printed is None:
            continue
        holders = [pid for pid, other in found if other == span]
        ids = [cid for cid in claim.evidence_ids if cid in holders] or holders[:1]
        return printed.model_copy(update={"quote": span, "evidence_ids": ids})
    stitched = [
        (pid, span, printed) for pid, text in texts for quote in quotes
        if (span := stitch(quote, text)) and (printed := _printed(claim, span))
    ]
    if stitched:
        pid, span, printed = next(
            (item for item in stitched if item[0] in claim.evidence_ids), stitched[0],
        )
        return printed.model_copy(update={"quote": span, "evidence_ids": [pid]})
    return claim


def _in_leading_fragment(item: Evidence, quote: str) -> bool:
    """Whether the quote starts in a shown lowercase chunk opening, before its first sentence."""
    at = item.content.find(quote) if quote else -1
    if at < 0 or not item.content.lstrip()[:1].islower():
        return False
    if not any(start == 0 for start, _ in exposed_spans(item)):
        return False
    end = _SENTENCE_END.search(item.content)
    return end is None or at < end.start()


def _continuation(claim: Claim, shown: Sequence[Evidence]) -> Claim:
    """The claim also citing the unique same-document chunk whose unfinished last sentence
    the quoted chunk's leading lowercase fragment completes on the same or next page."""
    by_id = {item.id: item for item in shown if not item.generated}
    holders = [
        by_id[cid] for cid in claim.evidence_ids
        if cid in by_id and _in_leading_fragment(by_id[cid], claim.quote)
    ]
    if len(holders) != 1 or not holders[0].pages:
        return claim
    holder = holders[0]
    first_page = min(holder.pages)
    previous = [
        item for item in by_id.values()
        if item.id not in claim.evidence_ids and item.content_hash == holder.content_hash
        and item.pages and max(item.pages) in {first_page - 1, first_page}
        and any(end == len(item.content) for _, end in exposed_spans(item))
        and (tail := item.content.rstrip()) and (tail[-1].isalpha() or tail[-1] == ",")
    ]
    if len(previous) != 1:
        return claim
    ids = list(claim.evidence_ids)
    ids.insert(ids.index(holder.id), previous[0].id)
    return claim.model_copy(update={"evidence_ids": ids})
