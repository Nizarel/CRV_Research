from collections import defaultdict
from decimal import Decimal

from crv_agent.domain.answers import Conflict
from crv_agent.domain.evidence import Claim
from crv_agent.services.calculation import canonical_value


def conflicts(claims: list[Claim]) -> list[Conflict]:
    groups: dict[tuple[str, str, str, str, str], list[int]] = defaultdict(list)
    values: dict[int, tuple[Decimal, str | None]] = {}
    for index, claim in enumerate(claims):
        if claim.quantity and claim.value is not None:
            canonical_unit, value = canonical_value(claim.value, claim.unit)
            values[index] = (value, claim.qualifier)
            groups[(
                claim.subject.casefold(), claim.quantity.casefold(),
                canonical_unit,
                claim.conditions or "", claim.scope or "",
            )].append(index)
    return [
        Conflict(subject=key[0], quantity=key[1], claim_indices=indices)
        for key, indices in groups.items()
        if len({values[i] for i in indices}) > 1
    ]
