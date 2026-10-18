"""Build the bake-off sample from the source-review evidence (offline, deterministic).

The sample is the reviewed PFD plus the corpus documents of the 2026-10-08 source review, so
every scored page has human-reviewed context. Documents too large for the spend cap are
excluded with a reason rather than silently truncated. The worst case assumes every page of
every document goes to Content Understanding in every CU arm.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Any

from ...config import PolicyRegistry
from .model import Exclusion, Sample, SampleDocument

PILOT_PREFIX = "_pilot/cu-bakeoff-v1/"
PFD_TUNING_PAGES = (3, 4, 5, 6, 7)
TUNING_DOCUMENTS = frozenset({"C02", "C07"})
MAX_DOCUMENT_PAGES = 100


def cu_arms(policies: PolicyRegistry) -> tuple[str, ...]:
    return tuple(
        name
        for name, entry in policies.active().items()
        if entry.policy.cu_profiles or entry.policy.cu_content_profiles or entry.policy.escalation
    )


def build_sample(
    review: Mapping[str, Any],
    policies: PolicyRegistry,
    *,
    caps: Mapping[str, int],
    max_document_pages: int = MAX_DOCUMENT_PAGES,
    pilot_prefix: str = PILOT_PREFIX,
) -> Sample:
    documents: list[SampleDocument] = []
    excluded: list[Exclusion] = []
    for source in review["sources"]:
        source_id = str(source["id"])
        pages = int(source["page_count"])
        if pages > max_document_pages:
            excluded.append(
                Exclusion(
                    id=source_id,
                    reason=f"{pages} pages exceeds the per-document cap of {max_document_pages}",
                )
            )
            continue
        path = str(source["path"])
        if source.get("root") == "workspace":
            document = SampleDocument(
                id=source_id,
                blob_name=pilot_prefix + PurePosixPath(path).name,
                sha256=source["sha256"],
                page_count=pages,
                split="mixed",
                tuning_pages=tuple(page for page in PFD_TUNING_PAGES if page <= pages),
                upload_from=path,
                observed_type=source.get("observed_type") or "Process flow diagram bundle",
            )
        else:
            document = SampleDocument(
                id=source_id,
                blob_name=path,
                sha256=source["sha256"],
                page_count=pages,
                split="tuning" if source_id in TUNING_DOCUMENTS else "held_out",
                observed_type=source.get("observed_type"),
            )
        documents.append(document)
    arms = tuple(policies.active())
    paid = cu_arms(policies)
    total = sum(document.page_count for document in documents)
    estimate = {
        "documents": len(documents),
        "pages": total,
        "cu_arms": len(paid),
        "cu_pages_upper_bound": total * len(paid),
    }
    if estimate["cu_pages_upper_bound"] > caps["cu_pages"]:
        raise ValueError(
            f"Worst case {estimate['cu_pages_upper_bound']} CU pages exceeds the cap "
            f"{caps['cu_pages']}; lower max_document_pages or remove an arm"
        )
    return Sample(
        arms=arms,
        cu_arms=paid,
        caps=dict(caps),
        documents=tuple(documents),
        excluded=tuple(excluded),
        estimate=estimate,
    )


def blob_list(sample: Sample) -> str:
    return "".join(f"{document.blob_name}\n" for document in sample.documents)
