import json
from collections.abc import Mapping
from typing import Any

from crv_agent.domain.evidence import Claim, Evidence
from crv_agent.services.evidence_pack import exposed_text


def event(name: str, payload: dict[str, object], sequence: int, run_id: str) -> str:
    return f"id: {sequence}\nevent: {name}\ndata: " + json.dumps(
        {"run_id": run_id, "sequence": sequence, **payload}, ensure_ascii=False,
    ) + "\n\n"


def diagnostics(research: Mapping[str, Any]) -> dict[str, object]:
    """Evaluation-only payload: every extracted claim and all passage text shown to workers."""
    claims = [Claim.model_validate(c) for c in research.get("claims") or []]
    evidence = [Evidence.model_validate(e) for e in research.get("evidence") or []]
    return {
        "ledger": [claim.model_dump(mode="json") for claim in claims],
        "exposed": [
            {
                "id": item.id, "content_hash": item.content_hash, "title": item.title,
                "source": item.source_path or item.source_uri, "pages": item.pages,
                "text": "\n…\n".join(exposed_text(item)),
            } for item in evidence
        ],
    }
