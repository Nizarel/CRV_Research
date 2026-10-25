import re

from crv_agent.domain.answers import AnswerDraft
from crv_agent.domain.evidence import Calculation, Claim, Evidence
from crv_agent.services.evidence_pack import exposed_text

_NUMBER = re.compile(
    r"(?<!\w)(?P<qualifier><=|>=|≤|≥|<|>|~|≈)?\s*(?P<number>[+-]?\d+(?:[.,]\d+)?)",
)
_GROUPED = re.compile(r"(?<=\d)[ \u00a0\u202f'’](?=\d{3}(?!\d))")
_SYMBOLS = {"≤": "<=", "≥": ">=", "≈": "~"}
# Qualifiers printed as words just before the number (French and English reports).
_WORDS = {
    qualifier: re.compile(
        rf"\b(?:{'|'.join(words)})(?:\s+(?:de|d'|à|a|les|of|than|to))?\s*$", re.IGNORECASE,
    )
    for qualifier, words in {
        ">=": ("au moins", "au minimum", "pas moins", "at least", "minimum"),
        "<=": ("au plus", "au maximum", "pas plus", r"jusqu['’]\s?à", r"ne dépasse(?:nt)? pas",
               r"n['’](?:a|ont) pas dépassé", "at most", "maximum", "up to"),
        ">": (r"(?<!au )(?<!pas )plus", r"supérieure?s?", r"superieure?s?", "au-delà",
              "dépassant", r"(?<!ne )dépasse(?:nt)?", r"au[- ]dessus",
              "more", "over", "above", "greater", "exceeding"),
        "<": (r"(?<!au )(?<!pas )moins", r"inférieure?s?", r"inferieure?s?", "en dessous",
              r"au[- ]dessous", "less", "below", "under"),
    }.items()
}
# Counts written as words ("trois campagnes"); "un"/"one" is too often an article.
_COUNTS = {
    2: "deux|two", 3: "trois|three", 4: "quatre|four", 5: "cinq|five", 6: "six",
    7: "sept|seven", 8: "huit|eight", 9: "neuf|nine", 10: "dix|ten", 11: "onze|eleven",
    12: "douze|twelve",
}


def value_stated(quote: str, value: object, qualifier: str | None) -> bool:
    """Whether the quote prints ``value`` with ``qualifier`` (as a symbol or as words).

    Grouped thousands (``8 800``) are read both joined and as separate numbers. An
    approximate value (``~``) is stated by the exact number too: "about X" never says more.
    """
    for text in dict.fromkeys((quote, _GROUPED.sub("", quote))):
        for match in _NUMBER.finditer(text):
            if not _decimal_equal(match["number"].replace(",", "."), str(value)):
                continue
            printed = _SYMBOLS.get(match["qualifier"], match["qualifier"])
            if printed == qualifier or (
                printed is None and qualifier is not None and (
                    qualifier == "~"
                    or _WORDS[qualifier].search(text[max(0, match.start() - 40):match.start()])
                )
            ):
                return True
    return False


def count_stated(quote: str, claim: Claim) -> bool:
    """Whether the quote writes the claim's small count as a word followed by its unit."""
    if claim.qualifier is not None or claim.value is None:
        return False
    words = _COUNTS.get(int(claim.value)) if claim.value == int(claim.value) else None
    if words is None:
        return False
    unit = (claim.unit or "").split()
    follower = rf"\s+{re.escape(unit[0].lower().rstrip('sx'))}" if unit else ""
    return re.search(rf"\b(?:{words})\b{follower}", quote, re.IGNORECASE) is not None


def stated(claim: Claim, quote: str) -> bool:
    """Whether the quote prints the claim's value and qualifier (digits or a count word)."""
    return value_stated(quote, claim.value, claim.qualifier) or count_stated(quote, claim)


def claim_errors(
    claim: Claim, evidence: dict[str, Evidence], calculations: list[Calculation] | None = None,
) -> list[str]:
    selected = [evidence[cid] for cid in claim.evidence_ids if cid in evidence]
    errors = []
    if not selected or len(selected) != len(claim.evidence_ids):
        errors.append("Claim references missing evidence")
    if not claim.quote.strip() or not any(
        claim.quote in text for item in selected if not item.generated
        for text in exposed_text(item)
    ):
        errors.append("Claim requires an exact quotation from source text shown to the model")
    if claim.calculation_index is not None:
        index = claim.calculation_index
        if calculations is None or index < 0 or index >= len(calculations):
            errors.append("Claim references missing deterministic calculation")
        else:
            computed = calculations[index]
            if (
                claim.value != computed.value or claim.unit != computed.unit
                or set(claim.evidence_ids) != set(computed.evidence_ids)
            ):
                errors.append("Derived claim differs from the deterministic calculation")
    elif claim.value is not None:
        if not stated(claim, claim.quote):
            errors.append("Claim numeric value/qualifier does not occur in its quotation")
    if any(not item.pages and not item.locators for item in selected):
        errors.append("Claim evidence has no original locator")
    return errors


def _decimal_equal(left: str, right: str) -> bool:
    from decimal import Decimal

    return Decimal(left) == Decimal(right)


def verify_draft(
    draft: AnswerDraft, evidence: list[Evidence], release: str,
    calculations: list[Calculation] | None = None,
) -> list[str]:
    by_id = {item.id: item for item in evidence if item.release == release}
    errors = [
        error for claim in draft.claims for error in claim_errors(claim, by_id, calculations)
    ]
    if not draft.insufficient and not draft.claims:
        errors.append("A factual answer requires claims")
    markers = set(re.findall(r"\[([^\[\]]+)\]", draft.content))
    if markers - by_id.keys():
        errors.append("Answer contains unknown citation markers")
    cited = {cid for claim in draft.claims for cid in claim.evidence_ids}
    if cited - markers:
        errors.append("Every claim needs a visible citation marker")
    if markers - cited:
        # Citations must resolve to returned claims, not just to some retrieved evidence.
        errors.append("Answer cites evidence not bound to a selected claim")
    if loose := ungrounded_numbers(draft.content, draft.claims, calculations or []):
        errors.append(UNGROUNDED + ", ".join(loose))
    return sorted(set(errors))


UNGROUNDED = "Answer states numbers not in cited claims or deterministic calculations: "
# Measurement-like numbers: decimals or percentages. Plain integers (years, counts, sample
# names, page numbers) are not checked; they rarely carry derived arithmetic.
_MEASURE = re.compile(r"(?<![\w.,])(\d+[.,]\d+|\d+(?=\s?%))(?![\d])")


def ungrounded_numbers(
    content: str, claims: list[Claim], calculations: list[Calculation],
) -> list[str]:
    """Decimals/percentages in the answer that no selected claim or calculation supports.

    The language model never does arithmetic: derived values must come from the
    deterministic calculator. A stated number matches a supported one exactly or as its
    rounding to the stated precision.
    """
    from decimal import Decimal

    text = re.sub(r"\[[^\[\]]+\]", " ", content)
    supported: list[Decimal] = [c.value for c in claims if c.value is not None]
    supported += [calculation.value for calculation in calculations]
    for claim in claims:
        supported += [
            Decimal(m["number"].replace(",", ".")) for m in _NUMBER.finditer(claim.quote)
        ]
    loose = []
    for match in _MEASURE.finditer(text):
        raw = match.group(1)
        stated = Decimal(raw.replace(",", "."))
        places = len(raw.replace(",", ".").partition(".")[2])
        tolerance = Decimal(5) / Decimal(10) ** (places + 1)
        if not any(abs(stated - value) <= tolerance for value in supported):
            loose.append(raw)
    return list(dict.fromkeys(loose))
