import re
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
# Metric masses only: "ton"/"tons" may be short tons, so they stay distinct units.
_MASS = {
    "t": Decimal(1), "tonne": Decimal(1), "tonnes": Decimal(1), "kt": Decimal(1000),
    "kg": Decimal("0.001"),
}
_GRADE = re.compile(r"%\s*([a-z]{1,2})?")

def canonical_value(value: Decimal, unit: str | None) -> tuple[str, Decimal]:
    known = _UNITS.get(unit or "")
    return (known[0], value * known[1]) if known else (unit or "", value)


def _family(unit: str | None) -> tuple[str, Decimal]:
    text = " ".join((unit or "").casefold().split())
    if text in _MASS:
        return "mass", _MASS[text]
    if grade := _GRADE.fullmatch(text):
        return "%" + (grade[1] or ""), Decimal(1)
    return text, Decimal(1)


def scale_factors(units: list[str | None]) -> list[Decimal] | None:
    """Factors to a common base when units only differ in spelling or scale, else None.

    A bare "%" matches one element grade ("% Cu"), never two different elements.
    """
    families = [_family(unit) for unit in units]
    names = {name for name, _ in families}
    named = names - {"%"}
    if len(named) > 1 or ("%" in names and named and not next(iter(named)).startswith("%")):
        return None
    return [factor for _, factor in families]


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
        factors = scale_factors([inputs[0].unit, inputs[1].unit])
        if factors is None or values[1] == 0:
            raise ValueError("Ratio requires compatible units and a nonzero denominator")
        value = values[0] * factors[0] / (values[1] * factors[1])
        unit = "fraction"
        if request.operation == "recovery":
            value *= 100
            unit = "%"
    elif request.operation == "sum" and (
        factors := scale_factors([claim.unit for claim in inputs])
    ) is not None:
        value = sum((v * f for v, f in zip(values, factors, strict=True)), Decimal(0))
        value, unit = value / factors[0], inputs[0].unit or ""
    elif request.operation == "weighted_recovery" and len(values) == 4:
        masses = scale_factors([inputs[0].unit, inputs[2].unit])
        grades = scale_factors([inputs[1].unit, inputs[3].unit])
        if (
            not inputs[0].unit or not inputs[1].unit or masses is None or grades is None
            or values[2] == 0 or values[3] == 0
        ):
            raise ValueError("Weighted recovery needs matching mass/grade units and nonzero feed")
        value = (
            values[0] * masses[0] * values[1] * grades[0]
            / (values[2] * masses[1] * values[3] * grades[1]) * 100
        )
        unit = "%"
    else:
        raise ValueError("Unsupported calculation shape or operation")
    return Calculation(
        operation=request.operation, input_claims=request.input_claims,
        value=value, unit=unit,
        evidence_ids=sorted({cid for claim in inputs for cid in claim.evidence_ids}),
    )
