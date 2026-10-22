import re

from crv_agent.domain.answers import AnswerDraft
from crv_agent.domain.evidence import Calculation, Claim, Evidence
from crv_agent.services.evidence_pack import exposed_text

_NUMBER = re.compile(
    r"(?<!\w)(?P<qualifier><=|>=|≤|≥|<|>|~|≈)?\s*(?P<number>[+-]?\d+(?:[.,]\d+)?)",
)


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
        numbers = [
            (match["number"].replace(",", "."), {
                "≤": "<=", "≥": ">=", "≈": "~",
            }.get(match["qualifier"], match["qualifier"]))
            for match in _NUMBER.finditer(claim.quote)
        ]
        if not any(
            _decimal_equal(number, str(claim.value)) and qualifier == claim.qualifier
            for number, qualifier in numbers
        ):
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
