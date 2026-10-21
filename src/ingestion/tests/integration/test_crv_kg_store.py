"""crv_kg on PostgreSQL 16 + Apache AGE 1.6: import, supersede, release, AGE load, read API."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from ingest_pipeline.adapters.postgres.crv_kg import CrvKgStore
from ingest_pipeline.domain.crv_kg.build import DocumentInput, Placement, build_run, document_iri
from ingest_pipeline.domain.crv_kg.cover import CoverLine
from ingest_pipeline.domain.crv_kg.gazetteer import parse_gazetteer
from ingest_pipeline.domain.crv_kg.vocab import DEFAULT_RELEASE_STATUSES

pytestmark = pytest.mark.integration

GAZETTEER = parse_gazetteer(
    (Path(__file__).resolve().parents[2] / "ingest_pipeline" / "data" / "crv-gazetteer.json")
    .read_bytes()
)
NOW = datetime(2026, 10, 21, 12, tzinfo=UTC)
ROOT = "AI for Science_ Geology & Chemistry"
BLEIDA = f"{ROOT}/Augmentation AI Processus R&D/GED_ A alimenter/Documents Activité/BLEIDA"
EIE = f"{ROOT}/Phytoremediation/Données EIE et autres/EIE Tizert"
RESEARCH = ["crv:research", "crv:geometallurgy", "crv:environment"]
CRV_KG_TABLES = (
    "crv_kg.release_pointer", "crv_kg.release_graph", "crv_kg.edge_projection",
    "crv_kg.vertex_projection", "crv_kg.release_member", "crv_kg.release",
    "crv_kg.assertion_evidence", "crv_kg.assertion_status", "crv_kg.assertion",
    "crv_kg.entity_evidence", "crv_kg.evidence", "crv_kg.identifier", "crv_kg.entity_record",
    "crv_kg.entity", "crv_kg.run",
)


def _doc(digest: str, path: str, cover: str = "", header: tuple[str, ...] = ()) -> DocumentInput:
    lines = tuple(CoverLine(text, 1, f"ev:{digest}:{n}") for n, text in
                  enumerate(cover.splitlines()))
    return DocumentInput(digest * 64, f"evd:{digest}", 4, (Placement(path),), header, lines)


DOCS = {
    "a": _doc("a", f"{BLEIDA}/Rapport de pilotage du Far West de bleida- 05-2016-.pdf",
              cover="Emetteur : Centre de Recherche REMINEX\nProjet : Appui à Bleida Cuivre",
              header=("CR/AF/BL/RP/10/17",)),
    "b": _doc("b", f"{BLEIDA}/Rapport de pilotage du Far West de bleida- 05-2016.pdf"),
    "c": _doc("c", f"{EIE}/Chapter 4_E Project Description_08122021_R1.pdf"),
    "d": _doc("d", f"{EIE}/Chapter 4_E Project Description_14122021_R2.pdf"),
}


@pytest.fixture
async def store(pg_pool: Any) -> CrvKgStore:
    async with pg_pool.acquire() as connection:
        graphs = await connection.fetch("SELECT graph_name FROM crv_kg.release_graph")
        for row in graphs:
            await connection.execute(
                "SELECT ag_catalog.drop_graph($1::name, true)", row["graph_name"]
            )
        await connection.execute(f"TRUNCATE {', '.join(CRV_KG_TABLES)} CASCADE")
    return CrvKgStore(pg_pool)


async def _import(store: CrvKgStore, keys: str, at: datetime = NOW) -> Any:
    run, report = build_run([DOCS[key] for key in keys], GAZETTEER, today=date(2026, 10, 21))
    assert report["validation_problem_count"] == 0
    return await store.import_run(run, started_at=at, now=at)


async def test_import_is_idempotent_and_later_builds_supersede(store: CrvKgStore) -> None:
    first = await _import(store, "abcd")
    assert first.run_created
    assert first.entities > 0
    assert first.assertions > 0
    again = await _import(store, "abcd", NOW + timedelta(minutes=1))
    assert not again.run_created
    later = await _import(store, "acd", NOW + timedelta(hours=1))
    assert later.run_created
    assert later.superseded_entities >= 1  # document b is no longer reported
    assert later.superseded_assertions >= 1
    projection = await store.current_projection(statuses=DEFAULT_RELEASE_STATUSES)
    iris = {vertex.iri for vertex in projection.vertices}
    assert document_iri("a" * 64) in iris
    assert document_iri("b" * 64) not in iris
    assert all(vertex.scope_keys for vertex in projection.vertices)


async def test_release_loads_into_age_and_traverses(store: CrvKgStore, pg_pool: Any) -> None:
    await _import(store, "abcd")
    release = await store.build_release(
        statuses=DEFAULT_RELEASE_STATUSES, started_at=NOW, now=NOW
    )
    same = await store.build_release(statuses=DEFAULT_RELEASE_STATUSES, started_at=NOW, now=NOW)
    assert release.created
    assert (same.release_id, same.created) == (release.release_id, False)
    graph = await store.load_age(release.release_id, now=NOW)
    assert graph == release.graph_name
    assert await store.load_age(release.release_id, now=NOW) == graph
    counts = await store.cypher(graph, "MATCH (d:Document) RETURN count(d)", columns=("n",))
    assert counts == [{"n": "4"}]
    rows = await store.cypher(
        graph, "MATCH (new:Document)-[:SUPERSEDES]->(old:Document) RETURN new.iri, old.iri",
        columns=("new", "old"),
    )
    assert [(row["new"].strip('"'), row["old"].strip('"')) for row in rows] == [
        (document_iri("d" * 64), document_iri("c" * 64))
    ]
    sites = await store.cypher(
        graph,
        "MATCH (:Document)-[:ABOUT_SITE]->(s:Site)-[:OPERATED_BY]->(o:Organisation)"
        " RETURN DISTINCT s.iri, o.iri",
        columns=("site", "operator"),
    )
    assert {(row["site"].strip('"'), row["operator"].strip('"')) for row in sites} == {
        ("urn:crv:site:bleida", "urn:crv:org:somifer"),  # BLEIDA folder and code segment BL
        ("urn:crv:site:tizert", "urn:crv:org:maa"),  # EIE Tizert folder
    }
    await store.activate(release.release_id, environment="dev", now=NOW)
    async with pg_pool.acquire() as connection:
        active = await connection.fetchrow("SELECT * FROM crv_kg.active_release('dev')")
    assert (active["release_id"], active["graph_name"]) == (release.release_id, graph)


async def _as_reader(pg_pool: Any, query: str, *args: Any) -> list[Any]:
    async with pg_pool.acquire() as connection, connection.transaction():
        await connection.execute("SET LOCAL ROLE crv_kg_reader")
        return list(await connection.fetch(query, *args))


async def test_read_api_filters_by_scope_and_needs_execute_only(
    store: CrvKgStore, pg_pool: Any
) -> None:
    await _import(store, "abcd")
    release = (await store.build_release(
        statuses=DEFAULT_RELEASE_STATUSES, started_at=NOW, now=NOW
    )).release_id
    a, b, c, d = (document_iri(key * 64) for key in "abcd")

    found = await _as_reader(
        pg_pool, "SELECT * FROM crv_kg.find_documents($1, $2, $3)",
        release, RESEARCH, ["urn:crv:site:bleida"],
    )
    assert {row["iri"] for row in found} == {a, b}
    assert {row["content_hash"] for row in found} == {"a" * 64, "b" * 64}
    linked = next(row for row in found if row["iri"] == a)["linked"]
    assert {"relation": "ISSUED_BY", "iri": "urn:crv:org:reminex"} in [
        {key: item[key] for key in ("relation", "iri")} for item in _json(linked)
    ]
    environment_only = await _as_reader(
        pg_pool, "SELECT * FROM crv_kg.find_documents($1, $2, $3)",
        release, ["crv:environment"], ["urn:crv:site:bleida"],
    )
    assert environment_only == []
    by_year = await _as_reader(
        pg_pool, "SELECT iri FROM crv_kg.find_documents($1, $2, p_from_year => 2021)",
        release, RESEARCH,
    )
    assert {row["iri"] for row in by_year} == {c, d}

    resolved = await _as_reader(
        pg_pool, "SELECT iri, kind, method FROM crv_kg.resolve($1, $2, $3)",
        release, "Essais de Bleida, rapport CR-AF-BL-RP-10-17", RESEARCH,
    )
    assert {(row["iri"], row["method"]) for row in resolved} >= {
        ("urn:crv:site:bleida", "alias"), ("urn:crv:code:cr-af-bl-rp-10-17", "code")
    }
    assert await _as_reader(
        pg_pool, "SELECT * FROM crv_kg.resolve($1, $2, $3)", release, "Bleida", []
    ) == []

    lineage = await _as_reader(
        pg_pool, "SELECT relation, direction, iri FROM crv_kg.document_lineage($1, $2, $3)",
        release, d, RESEARCH,
    )
    relations = {(row["relation"], row["direction"], row["iri"]) for row in lineage}
    assert ("SUPERSEDES", "out", c) in relations
    assert ("SAME_SET", "sibling", c) in relations
    duplicate = await _as_reader(
        pg_pool, "SELECT relation, direction, iri FROM crv_kg.document_lineage($1, $2, $3)",
        release, a, RESEARCH,
    )
    assert ("DUPLICATE_OF", "out", b) in {
        (row["relation"], row["direction"], row["iri"]) for row in duplicate
    }

    entity = await _as_reader(
        pg_pool, "SELECT * FROM crv_kg.entity($1, $2, $3)", release, "urn:crv:site:bleida",
        ["crv:research"],
    )
    evidence = _json(entity[0]["evidence"])
    assert evidence
    assert all(item["locator_type"] != "gazetteer" for item in evidence)
    neighbours = await _as_reader(
        pg_pool, "SELECT relation, direction FROM crv_kg.neighbors($1, $2, $3)",
        release, "urn:crv:site:bleida", RESEARCH,
    )
    assert ("ABOUT_SITE", "in") in {(row["relation"], row["direction"]) for row in neighbours}

    async with pg_pool.acquire() as connection, connection.transaction():
        await connection.execute("SET LOCAL ROLE crv_kg_reader")
        with pytest.raises(Exception, match="permission denied"):
            await connection.fetch("SELECT * FROM crv_kg.entity")


def _json(value: Any) -> Any:
    import json

    return json.loads(value) if isinstance(value, str) else value


async def test_writer_and_release_roles_hold_only_their_privileges(pg_pool: Any) -> None:
    async with pg_pool.acquire() as connection:
        rows = await connection.fetch(
            "SELECT relation, privilege,"
            " has_table_privilege('crv_kg_writer', relation, privilege) AS writer,"
            " has_table_privilege('crv_kg_release', relation, privilege) AS release"
            " FROM unnest(ARRAY['crv_kg.assertion', 'crv_kg.release', 'crv_kg.entity_record',"
            " 'ingest.evidence_document']) AS relation,"
            " unnest(ARRAY['INSERT', 'UPDATE', 'DELETE']) AS privilege"
        )
    granted = {(row["relation"], row["privilege"], row["writer"], row["release"]) for row in rows}
    assert ("crv_kg.assertion", "INSERT", True, False) in granted
    assert ("crv_kg.release", "INSERT", False, True) in granted
    assert not any(row["writer"] or row["release"] for row in rows
                   if row["privilege"] in ("UPDATE", "DELETE"))
    assert ("ingest.evidence_document", "INSERT", False, False) in granted
