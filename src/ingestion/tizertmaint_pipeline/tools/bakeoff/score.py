"""Score arm evidence against the gold set (pure functions, no I/O).

Printed-text fidelity counts only source text: a value found only in a generated description
is reported separately and never counts as extracted. Field checks apply to CU arms only.
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence

from ...domain.evidence import EvidenceBlock, EvidenceKind, EvidenceUnit
from .model import (
    ArmScore,
    DocumentEvidence,
    FieldValue,
    GoldItem,
    ItemResult,
    PageEvidence,
    Sample,
    Usage,
)

_DASHES = str.maketrans(dict.fromkeys("\u2010\u2011\u2012\u2013\u2014\u2212", "-"))
_SPACE = re.compile(r"\s+")
CU_METHOD = "cu_analyze"


def canonical(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).translate(_DASHES)
    return _SPACE.sub(" ", text).strip()


def contains(haystack: str | None, needle: str) -> bool:
    """The value appears on its own: not inside a longer identifier or decimal number."""

    if not haystack:
        return False
    pattern = (
        r"(?<![0-9A-Za-z])(?<![0-9][.,])"
        + re.escape(canonical(needle))
        + r"(?![0-9A-Za-z])(?![.,][0-9])"
    )
    return re.search(pattern, canonical(haystack)) is not None


def block_text(block: EvidenceBlock) -> str:
    if block.table is None:
        return block.text
    cells = " | ".join(cell.text for cell in block.table.cells if cell.text)
    return "\n".join(part for part in (block.text, block.table.caption or "", cells) if part)


def page_evidence(units: Iterable[EvidenceUnit]) -> dict[int, PageEvidence]:
    source: dict[int, list[str]] = defaultdict(list)
    generated: dict[int, list[str]] = defaultdict(list)
    fields: dict[int, list[FieldValue]] = defaultdict(list)
    routes: dict[int, set[str]] = defaultdict(set)
    for unit in units:
        unit_pages = tuple(page.page for page in unit.pages)
        for block in unit.blocks:
            # Page furniture stays in: drawing title blocks are often classified as footers.
            target = generated if block.evidence_kind is not EvidenceKind.SOURCE_TEXT else source
            for page in block.pages or unit_pages:
                target[page].append(block_text(block))
                routes[page].add(unit.extraction_method)
        for observation in unit.fields:
            for page in observation.pages or unit_pages:
                fields[page].append(
                    FieldValue(
                        field=observation.field,
                        path=observation.path,
                        method=observation.method,
                        value=observation.value,
                        raw_text=observation.raw_text,
                        confidence=observation.confidence,
                    )
                )
                routes[page].add(unit.extraction_method)
    pages = set(source) | set(generated) | set(fields)
    return {
        page: PageEvidence(
            source_text="\n".join(source.get(page, [])),
            generated_text="\n".join(generated.get(page, [])),
            fields=tuple(fields.get(page, [])),
            routes=tuple(sorted(routes.get(page, set()))),
        )
        for page in sorted(pages)
    }


def _field_hit(value: FieldValue, item: GoldItem) -> bool:
    return any(
        contains(str(value.value) if value.value is not None else None, form)
        or contains(value.raw_text, form)
        for form in item.forms
    )


def _where(page: PageEvidence, item: GoldItem) -> tuple[str, ...]:
    found: list[str] = []
    if any(contains(page.source_text, form) for form in item.forms):
        found.append("source")
    if any(contains(page.generated_text, form) for form in item.forms):
        found.append("generated")
    if any(_field_hit(value, item) for value in page.fields):
        found.append("field")
    return tuple(found)


def score_item(
    item: GoldItem, document: DocumentEvidence | None, sample: Sample, *, cu_arm: bool
) -> ItemResult:
    split = sample.document(item.doc).split_of(item.page)
    base = {
        "id": item.id,
        "doc": item.doc,
        "page": item.page,
        "check": item.check,
        "category": item.category,
        "split": split,
    }
    if item.check == "field" and not cu_arm:
        return ItemResult(**base, outcome="not_applicable")
    page = document.pages.get(item.page) if document is not None and document.found else None
    if page is None:
        return ItemResult(**base, outcome="missing_evidence")
    if item.check == "field" and CU_METHOD not in page.routes:
        # The arm kept this page on a native or DI route: no analyzer fields by design.
        return ItemResult(**base, outcome="not_applicable")
    where = _where(page, item)
    if item.check == "literal":
        passed = "source" in where
    elif item.check == "absent":
        passed = not where
    else:
        passed = any(value.field == item.field and _field_hit(value, item) for value in page.fields)
    return ItemResult(**base, outcome="pass" if passed else "fail", where=where)


def _rate(results: Sequence[ItemResult]) -> float | None:
    scored = [result for result in results if result.outcome != "not_applicable"]
    if not scored:
        return None
    return round(sum(result.outcome == "pass" for result in scored) / len(scored), 4)


def metrics(results: Sequence[ItemResult]) -> dict[str, dict[str, float | int | None]]:
    out: dict[str, dict[str, float | int | None]] = {}
    for split in ("all", "tuning", "held_out"):
        chosen = [result for result in results if split == "all" or result.split == split]
        by_check = {
            check: [result for result in chosen if result.check == check]
            for check in ("literal", "absent", "field")
        }
        literal = by_check["literal"]
        out[split] = {
            "items": len(chosen),
            "literal_recall": _rate(literal),
            "field_recall": _rate(by_check["field"]),
            "absent_pass_rate": _rate(by_check["absent"]),
            "missing_evidence": sum(result.outcome == "missing_evidence" for result in chosen),
            "literal_only_generated": sum(
                result.outcome == "fail" and "generated" in result.where for result in literal
            ),
        }
    return out


def score_arm(
    arm: str,
    gold: Sequence[GoldItem],
    evidence: Mapping[str, DocumentEvidence],
    sample: Sample,
    usage: Sequence[Usage] = (),
    jobs: Mapping[str, int] | None = None,
) -> ArmScore:
    cu_arm = arm in sample.cu_arms
    results = [
        score_item(item, evidence.get(item.doc), sample, cu_arm=cu_arm)
        for item in gold
        if item.status != "rejected"
    ]
    return ArmScore(
        arm=arm,
        documents=len(sample.documents),
        documents_found=sum(document.found for document in evidence.values()),
        documents_complete=sum(document.complete for document in evidence.values()),
        usage=tuple(usage),
        jobs=dict(jobs or {}),
        metrics=metrics(results),
        items=tuple(results),
    )
