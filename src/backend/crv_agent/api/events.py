import json


def event(name: str, payload: dict[str, object], sequence: int, run_id: str) -> str:
    return f"id: {sequence}\nevent: {name}\ndata: " + json.dumps(
        {"run_id": run_id, "sequence": sequence, **payload}, ensure_ascii=False,
    ) + "\n\n"
