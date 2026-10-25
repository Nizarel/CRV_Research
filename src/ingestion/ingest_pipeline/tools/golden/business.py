"""Import stakeholder CSV references without changing their text or the legacy benchmark."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
from collections import Counter
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ...domain.crv_kg.vocab import fold
from .dataset import GoldenQuestion, KeyFact, anchors_of, split_facts

HEADERS = ("Question", "\u00c9l\u00e9ments de r\u00e9ponse attendus")
ExternalPolicy = Literal["not_requested", "allowed_with_disclosure", "required"]


class ToolRule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    all_of: tuple[str, ...] = ()
    any_of: tuple[str, ...] = ()
    forbidden: tuple[str, ...] = ()


class BusinessExpectation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str = "business-v1"
    source_sha256: str
    source_file: str
    record: int = Field(ge=1)
    line_start: int = Field(ge=2)
    line_end: int = Field(ge=2)
    behavior: str = "answer"
    external_knowledge: ExternalPolicy = "not_requested"
    tools: ToolRule = Field(default_factory=ToolRule)
    review_status: Literal["proposed", "reviewed"] = "proposed"
    review_notes: tuple[str, ...] = ()


class BusinessQuestion(GoldenQuestion):
    evaluation: BusinessExpectation


def propose(question: str, expected: str) -> tuple[str, str, ExternalPolicy, str, ToolRule]:
    text, reference = fold(question), fold(expected)
    category, behavior = "A", "answer"
    external: ExternalPolicy = "not_requested"
    tools = ToolRule()
    if text.startswith(("compare", "retrace", "synthetise")):
        category = "B"
    if text.startswith(("calcule", "verifie le calcul")):
        category, behavior, tools = "G", "calculate", ToolRule(all_of=("calculate",))
    if reference.startswith(("hors corpus", "hors perimetre", "information hors corpus")):
        category, behavior = "F", "acknowledge_unavailable"
    if reference.startswith("premisse a corriger"):
        category, behavior = "F", "correct_premise"
    if reference.startswith(("information incomplete", "information non tranchee")):
        category, behavior = "F", "qualify_incomplete"
    if reference.startswith("connaissance generale"):
        behavior, external = "distinguish_external", "required"
    elif "clairement presentee comme externe" in reference or "signalee comme externe" in reference:
        external = "allowed_with_disclosure"
    if "signaler cette incoherence" in reference:
        category, behavior = "D", "flag_conflict"
    return category, behavior, external, reference, tools


def import_csv(path: Path, *, start: int = 77) -> tuple[list[BusinessQuestion], dict[str, object]]:
    if start < 77:
        raise ValueError("Extension IDs must start after the original Q1-Q76 benchmark")
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    reader = csv.reader(io.StringIO(raw.decode("utf-8-sig"), newline=""), strict=True)
    if next(reader, None) != list(HEADERS):
        raise ValueError(f"Expected exactly these CSV headers: {HEADERS!r}")
    questions: list[BusinessQuestion] = []
    previous_line = reader.line_num
    for ordinal, row in enumerate(reader, 1):
        first_line, previous_line = previous_line + 1, reader.line_num
        if len(row) != 2 or any(not value.strip() for value in row):
            raise ValueError(f"Missing or extra CSV cells at record {ordinal}")
        question, expected = row
        category, behavior, external, reference, tools = propose(question, expected)
        notes = []
        if "quatre lots" in reference and "lot 3" in reference and "lot 4" not in reference:
            notes.append(
                "Reference names four lots but describes only three; business review needed"
            )
        if "pnn pn pa" in reference and "ou rapport" in reference:
            notes.append("Reference combines a difference and ratio; business review needed")
        number = start + ordinal - 1
        questions.append(
            BusinessQuestion(
                id=f"Q{number}",
                number=number,
                question=question,
                expected=expected,
                category=category,
                split="dev",
                key_facts=tuple(
                    KeyFact(text=part, anchors=anchors_of(part)) for part in split_facts(expected)
                )
                or (KeyFact(text=expected, anchors=anchors_of(expected)),),
                must_abstain=behavior == "acknowledge_unavailable" and external == "not_requested",
                must_flag_conflict=behavior == "flag_conflict",
                evaluation=BusinessExpectation(
                    source_sha256=digest,
                    source_file=path.name,
                    record=ordinal,
                    line_start=first_line,
                    line_end=reader.line_num,
                    behavior=behavior,
                    external_knowledge=external,
                    tools=tools,
                    review_notes=tuple(notes),
                ),
            )
        )
    if not questions:
        raise ValueError("The business CSV contains no questions")
    counts = Counter(item.question for item in questions)
    return questions, {
        "source_sha256": digest,
        "questions": len(questions),
        "reference_elements": sum(len(item.key_facts) for item in questions),
        "duplicate_questions": [text for text, count in counts.items() if count > 1],
        "proposed_categories": dict(Counter(item.category for item in questions)),
        "proposed_behaviors": dict(Counter(item.evaluation.behavior for item in questions)),
        "reference_review": [item.id for item in questions if item.evaluation.review_notes],
    }


def write_import(source: Path, output: Path, review: Path, *, start: int = 77) -> dict[str, object]:
    questions, audit = import_csv(source, start=start)
    if (
        source.resolve() in {output.resolve(), review.resolve()}
        or output.resolve() == review.resolve()
    ):
        raise ValueError("Source, dataset and review output paths must be different")
    if output.exists() or review.exists():
        raise ValueError("Refusing to overwrite a frozen dataset or review file")
    output.parent.mkdir(parents=True, exist_ok=True)
    review.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        "".join(item.model_dump_json() + "\n" for item in questions), encoding="utf-8"
    )
    with review.open("w", encoding="utf-8-sig", newline="") as stream:
        fields = [
            "id",
            "record",
            "line_start",
            "line_end",
            "category",
            "behavior",
            "external_knowledge",
            "review_status",
            "review_notes",
            "question",
            "expected",
        ]
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for item in questions:
            metadata = item.evaluation.model_dump()
            writer.writerow(
                {
                    **{key: metadata[key] for key in fields if key in metadata},
                    "id": item.id,
                    "category": item.category,
                    "question": item.question,
                    "expected": item.expected,
                    "review_notes": " | ".join(item.evaluation.review_notes),
                }
            )
    return audit


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--review", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(write_import(args.csv, args.out, args.review), ensure_ascii=True))


if __name__ == "__main__":
    main()
