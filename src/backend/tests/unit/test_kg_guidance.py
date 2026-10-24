"""KG-guided retrieval: released studies choose the documents of one strict search."""

import json
from dataclasses import replace
from uuid import uuid4

from fastapi.testclient import TestClient

from crv_agent.api.app import create_app
from crv_agent.domain.planning import Entity, StudyRef
from crv_agent.services.kg_guidance import MAX_DOCUMENTS, kg_documents
from tests.fakes import FakeKnowledge, FakeModels, FakeWorker
from tests.unit.test_graph_api import services


def study(document: str | None, period: str = "2015") -> StudyRef:
    return StudyRef(
        iri=f"urn:crv:study:{document}/0", label="Pilote", scale="pilot", period=period,
        facility=None, status="candidate", content_hash=document and document * 64,
        source_paths=[], pages=[3],
    )


def test_kg_documents_keep_period_order_and_spread_over_the_cap() -> None:
    assert kg_documents([study("a"), study("b"), study("a"), study(None)]) == [
        "a" * 64, "b" * 64,
    ]
    assert kg_documents([]) == []
    many = [study(f"{n:x}", str(2000 + n)) for n in range(15)]
    chosen = kg_documents(many)
    assert len(chosen) == MAX_DOCUMENTS == len(set(chosen))
    assert (chosen[0], chosen[-1]) == ("0" * 64, "e" * 64)
    assert chosen == sorted(chosen, key=lambda h: int(h[0], 16))


class Steered(FakeKnowledge):
    async def resolve(self, text: str, release: str) -> list[Entity]:
        return [Entity(iri="urn:crv:site:tizert", name="Tizert", kind="Site", status="observed")]

    async def studies(self, entities: list[str], release: str) -> list[StudyRef]:
        assert entities == ["urn:crv:site:tizert"]
        return [study("a"), study("b", "2018")]


class Recording(FakeWorker):
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str], bool]] = []

    async def run(  # type: ignore[no-untyped-def]
        self, question, release, hashes, budget, *, strict=False,
    ):
        self.calls.append((question.split("\n", 1)[0], list(hashes), strict))
        return await super().run(question, release, hashes, budget)


def _ask(enabled: bool, intent: str) -> list[tuple[str, list[str], bool]]:
    worker = Recording()
    deps = replace(services(FakeModels(intent=intent)), knowledge=Steered(), worker=worker,
                   kg_results=enabled)
    with TestClient(create_app(deps)) as client:
        thread = client.post("/api/v1/conversations").json()
        response = client.post("/api/v1/chat", json={
            "thread_id": thread["id"], "request_id": str(uuid4()), "message": "Tizert pilots?",
        })
        assert "event: answer" in response.text
        answer = next(
            json.loads(block.splitlines()[2].removeprefix("data: "))
            for block in response.text.strip().split("\n\n")
            if block.splitlines()[1] == "event: answer"
        )
    assert all("KG" not in claim["text"] for claim in answer["claims"])
    return worker.calls


def test_kg_results_add_one_strict_facet_on_study_documents_for_synthesis() -> None:
    steered = _ask(True, "synthesis")
    assert len(steered) == 2
    chosen, planned = steered  # the KG facet starts first
    assert planned[2] is False
    assert chosen == ("Tizert pilots?", ["a" * 64, "b" * 64], True)
    assert len(_ask(False, "synthesis")) == 1
    assert len(_ask(True, "lookup")) == 1
