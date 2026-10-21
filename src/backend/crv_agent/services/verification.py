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
    return sorted(set(errors))
