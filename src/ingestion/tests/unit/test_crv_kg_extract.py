"""KG-2 extraction inputs and the per-document extractor (fake model client)."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

from ingest_pipeline.adapters.azure_ai.study_extractor import StudyExtractor, render
from ingest_pipeline.adapters.postgres.crv_kg_inputs import page_lines
from ingest_pipeline.domain.crv_kg.study import StudyDocument, ground, study_pages
from ingest_pipeline.domain.evidence import BlockKind, EvidenceBlock, EvidencePage, EvidenceUnit

HASH = "a" * 64
DOCUMENT = StudyDocument(
    content_hash=HASH,
    source_path="Geometallurgy Cu/Dossier Cu/Pilote Tizert 2015.pdf",
    page_count=20,
    pages={1: "Pilote de flottation Tizert 2015", 19: "Récupération Cu : 87,5 %"},
)


def test_study_pages_are_the_first_six_and_the_last_three() -> None:
    assert study_pages(20) == (1, 2, 3, 4, 5, 6, 18, 19, 20)
    assert study_pages(4) == (1, 2, 3, 4)
    assert study_pages(0) == ()


def test_page_lines_keep_only_selected_pages_in_reading_order() -> None:
    blocks = tuple(
        EvidenceBlock(evidence_id=f"e{page}", kind=BlockKind.PARAGRAPH, text=text, pages=(page,))
        for page, text in ((1, "Couverture\nPilote"), (9, "Annexe"), (19, "Conclusion"))
    )
    unit = EvidenceUnit(
        unit_id="u", plan_id="p", content_hash=HASH, route="native_pdf_text",
        extraction_method="native_text", normalizer="test", blocks=blocks,
        pages=tuple(EvidencePage(page=page) for page in (1, 9, 19)),
    )
    assert [(line.page, line.text) for line in page_lines([unit], study_pages(20))] == [
        (1, "Couverture"), (1, "Pilote"), (19, "Conclusion"),
    ]


def test_render_marks_every_page_and_names_the_source() -> None:
    text = render(DOCUMENT)
    assert text.startswith("SOURCE: Pilote Tizert 2015.pdf\n\n[page 1]\nPilote")
    assert "[page 19]\nRécupération Cu : 87,5 %" in text
    assert render(DOCUMENT, limit=10).endswith("[page 1]\nP")


class FakeClient:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.calls: list[dict[str, Any]] = []
        self.payload = payload
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        content = json.dumps(self.payload)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


def test_extractor_sends_a_strict_schema_and_its_output_is_grounded() -> None:
    locator = {"page": 19, "quote": "Récupération Cu : 87,5 %"}
    payload = {
        "studies": [{
            "label": "Pilote Tizert", "scale": "pilot", "period": "2015", "facility": None,
            "commissioned_by": None, "partners": [], "commodities": ["Cu"],
            "processes": ["flotation"], "samples": [], "reagents": [],
            "follows_or_validates": [],
            "locator": {"page": 1, "quote": "Pilote de flottation Tizert 2015"},
        }],
        "observations": [{
            "study": 0, "sample": None, "site": "Tizert", "property": "recovery",
            "element": "Cu", "stream": None, "basis": None, "value_text": "87,5 %",
            "unit": "%", "qualifier": None, "locator": locator,
        }],
    }
    client = FakeClient(payload)
    extraction = asyncio.run(StudyExtractor(client, deployment="gpt").extract(DOCUMENT))
    [call] = client.calls
    assert call["response_format"]["json_schema"]["strict"] is True
    assert call["model"] == "gpt"
    assert call["reasoning_effort"] == "low"
    grounded = ground(extraction, DOCUMENT.pages)
    assert grounded.rejected == []
    assert [str(o.value_num) for o in grounded.observations] == ["87.5"]
