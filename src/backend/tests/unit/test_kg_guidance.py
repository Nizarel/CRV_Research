"""KG-guided retrieval: released observations steer one extra facet, never become claims."""

import json
from dataclasses import replace
from uuid import uuid4

from fastapi.testclient import TestClient

from crv_agent.api.app import create_app
from crv_agent.domain.planning import Entity, ObservationRef
from crv_agent.services.kg_guidance import kg_facet
from tests.fakes import FakeKnowledge, FakeModels, FakeWorker
from tests.unit.test_graph_api import services


def ref(value: str, document: str, study: str | None = "Pilote 2015") -> ObservationRef:
    return ObservationRef(
        property="recovery", element="Cu", stream=None, value_text=value, unit="%",
        status="candidate", subject="Tizert", study=study, period="2015",
        content_hash=document * 64, source_paths=[], page=7, quote=f"Récupération {value}",
    )


def test_kg_facet_lists_values_round_robin_across_documents() -> None:
    built = kg_facet([ref("87,5 %", "a"), ref("88 %", "a"), ref("79 %", "b"), ref("x", "c")])
    assert built is not None
    facet, hashes = built
    assert hashes == ["a" * 64, "b" * 64, "c" * 64]
    assert facet.question.startswith(
        "Locate and quote these reported results in their source documents: "
        "recovery Cu 87,5 % (Pilote 2015, 2015); recovery Cu 79 % (Pilote 2015, 2015)",
    )
    assert kg_facet([]) is None
    assert kg_facet([ref("1 %", "a").model_copy(update={"content_hash": None})]) is None


class Steered(FakeKnowledge):
    async def resolve(self, text: str, release: str) -> list[Entity]:
        return [Entity(iri="urn:crv:site:tizert", name="Tizert", kind="Site", status="observed")]

    async def observations(
        self, entities: list[str], properties: list[str] | None, release: str,
    ) -> list[ObservationRef]:
        assert entities == ["urn:crv:site:tizert"]
        return [ref("87,5 %", "a")]


class Recording(FakeWorker):
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str]]] = []

    async def run(self, question, release, hashes, budget):  # type: ignore[no-untyped-def]
        self.calls.append((question.split("\n", 1)[0], list(hashes)))
        return await super().run(question, release, hashes, budget)


def _ask(enabled: bool, intent: str) -> list[tuple[str, list[str]]]:
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


def test_kg_results_add_one_scoped_facet_only_when_enabled_for_synthesis() -> None:
    steered = _ask(True, "synthesis")
    assert len(steered) == 2
    question, hashes = steered[1]
    assert question.startswith("Locate and quote these reported results")
    assert hashes == ["a" * 64]
    assert len(_ask(False, "synthesis")) == 1
    assert len(_ask(True, "lookup")) == 1
