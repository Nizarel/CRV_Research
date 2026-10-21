from statistics import mean


def gate_failures(
    measurements: dict[str, list[float | bool | None]], first_answer_p95: float | None,
) -> list[str]:
    thresholds = {
        "citation_contract_valid": 1.0, "source_match": 0.8, "numeric_exact": 0.9,
        "abstention_correct": 1.0, "conflict_flagged": 0.8,
        "key_fact_recall_A": 0.8, "key_fact_recall_BC": 0.6,
        "calculator_used": 1.0, "catalogue_used": 1.0,
    }
    failed = []
    for name, threshold in thresholds.items():
        values = [v for v in measurements.get(name, []) if v is not None]
        if not values:
            failed.append(f"{name}: not measured")
        elif mean(values) < threshold:
            failed.append(f"{name}: below {threshold}")
    if first_answer_p95 is None or first_answer_p95 > 25:
        failed.append("first_answer_seconds_p95: not measured or above 25 seconds")
    return failed
