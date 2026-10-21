from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict


class KeyFact(BaseModel):
    text: str
    anchors: list[str] = []


class NumericCheck(BaseModel):
    value: str
    tolerance: str = "0"
    unit: str


class GoldenQuestion(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    question: str
    category: str
    split: Literal["dev", "heldout"]
    key_facts: list[KeyFact]
    expected_sources: list[str] = []
    must_abstain: bool = False
    must_flag_conflict: bool = False
    numeric_checks: list[NumericCheck] = []


def load_questions(path: Path, split: Literal["dev", "heldout"]) -> list[GoldenQuestion]:
    questions = [
        GoldenQuestion.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    if len({question.id for question in questions}) != len(questions):
        raise ValueError("Golden question IDs must be unique")
    return [question for question in questions if question.split == split]
