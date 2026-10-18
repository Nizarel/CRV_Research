"""Knowledge store against PostgreSQL 16 + Apache AGE 1.6.0: import, release, AGE, traversal."""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from tizertmaint_pipeline.adapters.postgres.knowledge import PostgresKnowledgeStore
from tizertmaint_pipeline.domain.errors import IntegrityError
from tizertmaint_pipeline.domain.knowledge import parse_registry

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 14, 8, 0, tzinfo=UTC)
HASH = "b" * 64
MILL = "urn:tizert:equipment:mill"
AREA = "urn:tizert:processarea:6220"
GROUP = "urn:tizert:processgroup:grinding"
DOC = "urn:tizert:documentreference:pid"
TAGS = [f"urn:tizert:instrumenttag:t{n:02d}" for n in range(30)]
KG_TABLES = (
    "kg.alias, kg.alias_set,"
    " kg.release_pointer, kg.release_graph, kg.edge_projection, kg.vertex_projection,"
    " kg.release_member, kg.release, kg.assertion_evidence, kg.assertion_status, kg.assertion,"
    " kg.entity_evidence, kg.evidence, kg.identifier, kg.entity_record, kg.entity, kg.run"
)


def _registry_path() -> Path:
    configured = os.environ.get("TIZ_TEST_KG_REGISTRY")
    if configured:
        return Path(configured)
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "data/knowledge/runs/2026-10-07/registry.jsonl"
        if candidate.exists():
            return candidate
    return Path("registry-not-found")


def _prov(row: int) -> list[dict[str, Any]]:
    return [
        {
            "id": f"urn:tizert:evidence:row{row}",
            "row_number": row,
            "source_hash": HASH,
            "source_name": "Tag_Register.csv",
        }
    ]


def _entity(iri: str, kind: str, identifier: str, row: int) -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "id": iri,
        "kind": kind,
        "identifier": identifier,
        "label": identifier,
        "attributes": {"Source": "test"},
        "review_status": "reported",
        "provenance": _prov(row),
    }


def _assertion(iri: str, subject: str, predicate: str, obj: str, row: int) -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "id": iri,
        "kind": "Assertion",
        "subject": subject,
        "predicate": predicate,
        "object": obj,
        "method": "register_import",
        "review_status": "reported",
        "provenance": _prov(row),
    }


def _registry(doc_subject: str = MILL) -> bytes:
    records = [
        _entity(MILL, "Equipment", "6220-ML-2241", 1),
        _entity(AREA, "ProcessArea", "6220", 2),
        _entity(GROUP, "ProcessGroup", "Grinding", 3),
        _entity(DOC, "DocumentReference", "SP0904-6220-D-PIDS-001.pdf", 4),
        _assertion("urn:tizert:assertion:area", MILL, "reportedInArea", AREA, 5),
        _assertion("urn:tizert:assertion:group", AREA, "inProcessGroup", GROUP, 6),
        _assertion("urn:tizert:assertion:doc", doc_subject, "referencesDocument", DOC, 7),
    ]
    for number, tag in enumerate(TAGS):
        records.append(_entity(tag, "InstrumentTag", f"6220-TT-{number:04d}", 100 + number))
        records.append(_assertion(f"urn:tizert:assertion:tag{number}", tag, "tagOf", MILL, 200))
    return "\n".join(json.dumps(record) for record in records).encode()


@pytest.fixture
async def store(pg_pool: Any) -> AsyncIterator[PostgresKnowledgeStore]:
    async def reset() -> None:
        async with pg_pool.acquire() as connection:
            graphs = await connection.fetch(
                "SELECT name FROM ag_catalog.ag_graph WHERE name::text LIKE 'kgr\\_%'"
            )
            for row in graphs:
                await connection.execute(
                    "SELECT ag_catalog.drop_graph($1::name, true)", row["name"]
                )
            await connection.execute(f"TRUNCATE {KG_TABLES}")

    await reset()
    yield PostgresKnowledgeStore(pg_pool)
    await reset()


async def test_import_is_idempotent_and_keeps_provenance(
    store: PostgresKnowledgeStore, pg_pool: Any
) -> None:
    batch = parse_registry(_registry())
    first = await store.import_registry(batch, started_at=NOW, now=NOW)
    again = await store.import_registry(batch, started_at=NOW, now=NOW)
    assert (first.run_created, first.entities, first.assertions, first.statuses) == (
        True,
        34,
        33,
        33,
    )
    assert (again.run_created, again.entities, again.assertions, again.statuses) == (
        False,
        0,
        0,
        0,
    )
    async with pg_pool.acquire() as connection:
        lookup = await connection.fetchval(
            "SELECT entity_iri FROM kg.identifier"
            " WHERE scheme = 'equipment_tag' AND value_normalized = '6220-ML-2241'"
        )
        support = await connection.fetchval(
            "SELECT e.locator ->> 'row_number' FROM kg.assertion_evidence AS l"
            " JOIN kg.evidence AS e ON e.iri = l.evidence_iri"
            " WHERE l.assertion_iri = 'urn:tizert:assertion:area'"
        )
    assert (lookup, support) == (MILL, "5")


async def test_reimport_never_overrides_a_reviewed_status(
    store: PostgresKnowledgeStore, pg_pool: Any
) -> None:
    import asyncpg

    batch = parse_registry(_registry())
    await store.import_registry(batch, started_at=NOW, now=NOW)
    async with pg_pool.acquire() as connection:
        with pytest.raises(asyncpg.CheckViolationError):
            await connection.execute(
                "INSERT INTO kg.assertion_status (assertion_iri, status, run_id, recorded_at)"
                " VALUES ('urn:tizert:assertion:area', 'approved', $1, now())",
                batch.run_id,
            )
        await connection.execute(
            "INSERT INTO kg.assertion_status (assertion_iri, status, decision_id, recorded_at)"
            " VALUES ('urn:tizert:assertion:area', 'rejected', 'decision:1', now())"
        )
    rerun = parse_registry(_registry() + b"\n")
    report = await store.import_registry(rerun, started_at=NOW, now=NOW)
    async with pg_pool.acquire() as connection:
        status = await connection.fetchval(
            "SELECT status FROM kg.assertion_current"
            " WHERE assertion_iri = 'urn:tizert:assertion:area'"
        )
    assert (report.run_created, report.statuses, status) == (True, 0, "rejected")


async def test_a_changed_triple_under_the_same_iri_is_refused(
    store: PostgresKnowledgeStore,
) -> None:
    await store.import_registry(parse_registry(_registry()), started_at=NOW, now=NOW)
    moved = parse_registry(_registry(doc_subject=AREA) + b"\n")
    with pytest.raises(IntegrityError, match="different triple"):
        await store.import_registry(moved, started_at=NOW, now=NOW)


async def test_writer_role_cannot_rewrite_the_record(pg_pool: Any) -> None:
    async with pg_pool.acquire() as connection:
        rows = await connection.fetch(
            "SELECT relation, privilege,"
            " has_table_privilege('tizert_kg_writer', relation, privilege) AS granted"
            " FROM unnest(ARRAY['kg.assertion', 'kg.assertion_status', 'kg.evidence'])"
            " AS relation, unnest(ARRAY['INSERT', 'UPDATE', 'DELETE', 'TRUNCATE']) AS privilege"
        )
        release_writes = await connection.fetchval(
            "SELECT has_table_privilege('tizert_kg_release', 'kg.assertion', 'INSERT')"
        )
    granted = {(row["relation"], row["privilege"]): row["granted"] for row in rows}
    assert all(granted[(relation, "INSERT")] for relation, _ in granted)
    assert not any(value for (_, privilege), value in granted.items() if privilege != "INSERT")
    assert release_writes is False


async def test_release_load_and_traversal_parity(
    store: PostgresKnowledgeStore, pg_pool: Any
) -> None:
    await store.import_registry(parse_registry(_registry()), started_at=NOW, now=NOW)
    release = await store.build_release(statuses=("approved", "reported"), started_at=NOW, now=NOW)
    same = await store.build_release(statuses=("approved", "reported"), started_at=NOW, now=NOW)
    assert (release.vertex_count, release.edge_count, release.created) == (34, 33, True)
    assert (same.release_id, same.created) == (release.release_id, False)

    graph = await store.load_age(release.release_id, now=NOW)
    assert graph == release.graph_name
    assert await store.load_age(release.release_id, now=NOW) == graph

    scopes = ("registers",)
    via_sql = await store.equipment_context(release.release_id, MILL, scopes=scopes, via="sql")
    via_age = await store.equipment_context(release.release_id, MILL, scopes=scopes, via="age")
    assert via_age == via_sql
    relations = {(n.relation, n.direction) for n in via_sql}
    assert relations == {
        ("REPORTED_IN_AREA", "out"),
        ("REFERENCES_DOCUMENT", "out"),
        ("TAG_OF", "in"),
    }
    tags = [n for n in via_sql if n.relation == "TAG_OF"]
    assert len(tags) == 25  # 30 tags, capped per relation after visibility filtering
    assert [n.identifier for n in tags] == sorted(n.identifier for n in tags)
    assert all(n.status == "reported" and n.assertion_iris for n in via_sql)

    assert await store.equipment_context(release.release_id, MILL, scopes=("hr",), via="age") == []
    assert await store.equipment_context(release.release_id, AREA, scopes=scopes, via="sql") == []
    assert await store.equipment_context(release.release_id, MILL, scopes=(), via="sql") == []

    await store.activate(release.release_id, environment="pilot", now=NOW)
    async with pg_pool.acquire() as connection:
        active = await connection.fetchrow(
            "SELECT release_id, graph_name FROM kg.release_active WHERE environment = 'pilot'"
        )
    assert (active["release_id"], active["graph_name"]) == (release.release_id, graph)


async def test_full_phase1_registry_round_trip(store: PostgresKnowledgeStore, pg_pool: Any) -> None:
    path = _registry_path()
    if not path.exists():
        pytest.skip(f"{path} is not available in this environment")
    batch = parse_registry(path.read_bytes())
    report = await store.import_registry(batch, started_at=NOW, now=NOW)
    assert (report.entities, report.assertions) == (8661, 25086)
    release = await store.build_release(statuses=("approved", "reported"), started_at=NOW, now=NOW)
    graph = await store.load_age(release.release_id, now=NOW)
    async with pg_pool.acquire() as connection:
        conveyor = await connection.fetchval(
            "SELECT entity_iri FROM kg.identifier"
            " WHERE scheme = 'equipment_tag' AND value_normalized = '6026-CV-1005'"
        )
        loaded = await connection.fetchrow(
            "SELECT vertex_count, edge_count FROM kg.release_graph WHERE graph_name = $1", graph
        )
    assert conveyor is not None
    assert (loaded["vertex_count"], loaded["edge_count"]) == (
        release.vertex_count,
        release.edge_count,
    )
    scopes = ("registers",)
    via_sql = await store.equipment_context(release.release_id, conveyor, scopes=scopes, via="sql")
    via_age = await store.equipment_context(release.release_id, conveyor, scopes=scopes, via="age")
    assert {n.relation for n in via_sql} >= {"REPORTED_IN_AREA", "TAG_OF"}
    assert via_age == via_sql
