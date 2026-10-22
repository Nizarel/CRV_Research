from decimal import Decimal

from crv_agent.domain.evidence import Calculation, CalculationRequest, Claim

_UNITS = {
    "kWh/t": ("energy_mass", Decimal(1)),
    "kWh/sht": ("energy_mass", Decimal("1.1023113109243879")),
    "g/t": ("mass_ratio", Decimal(1)),
    "kg/t": ("mass_ratio", Decimal(1000)),
    "%": ("ratio", Decimal("0.01")),
    "fraction": ("ratio", Decimal(1)),
}

def canonical_value(value: Decimal, unit: str | None) -> tuple[str, Decimal]:
    known = _UNITS.get(unit or "")
    return (known[0], value * known[1]) if known else (unit or "", value)


def calculate(request: CalculationRequest, claims: list[Claim]) -> Calculation:
    if not request.input_claims or any(
        index < 0 or index >= len(claims) for index in request.input_claims
    ):
        raise ValueError("Calculation must reference existing input claims")
    inputs = [claims[index] for index in request.input_claims]
    if any(claim.value is None or not claim.evidence_ids for claim in inputs):
        raise ValueError("Calculation inputs require cited numeric values")
    if any(claim.qualifier for claim in inputs):
        raise ValueError("Calculations on qualified/approximate values require explicit review")
    values = [claim.value for claim in inputs if claim.value is not None]
    if request.operation == "convert" and len(values) == 1:
        source = _UNITS.get(inputs[0].unit or "")
        target = _UNITS.get(request.target_unit or "")
        if not source or not target or source[0] != target[0]:
            raise ValueError("Unknown or dimensionally incompatible units")
        value = values[0] * source[1] / target[1]
        unit = request.target_unit or ""
    elif request.operation in {"ratio", "recovery"} and len(values) == 2:
        if inputs[0].unit != inputs[1].unit or values[1] == 0:
            raise ValueError("Ratio requires identical units and a nonzero denominator")
        value = values[0] / values[1]
        unit = "fraction"
        if request.operation == "recovery":
            value *= 100
            unit = "%"
    elif request.operation == "sum" and len({claim.unit for claim in inputs}) == 1:
        value, unit = sum(values, Decimal(0)), inputs[0].unit or ""
    elif request.operation == "weighted_recovery" and len(values) == 4:
        if (
            not inputs[0].unit or inputs[0].unit != inputs[2].unit
            or not inputs[1].unit or inputs[1].unit != inputs[3].unit
            or values[2] == 0 or values[3] == 0
        ):
            raise ValueError("Weighted recovery needs matching mass/grade units and nonzero feed")
        value = values[0] * values[1] / (values[2] * values[3]) * 100
        unit = "%"
    else:
        raise ValueError("Unsupported calculation shape or operation")
    return Calculation(
        operation=request.operation, input_claims=request.input_claims,
        value=value, unit=unit,
        evidence_ids=sorted({cid for claim in inputs for cid in claim.evidence_ids}),
    )
