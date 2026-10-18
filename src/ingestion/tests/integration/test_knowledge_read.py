"""Release-pinned kg read API (0012) as the agent's reader role sees it.

The last test grades the synthetic RDF evaluation pack (data/Golden/rdf_synthetic) against the
live functions over the full Phase 1 registry: every registry case must pass.
"""

from __future__ import annotations

import json
import os
import re
import statistics
import time
from collections import Counter, defaultdict
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from ingest_pipeline.adapters.postgres.knowledge import PostgresKnowledgeStore
from ingest_pipeline.domain.knowledge import EDGE_LABELS, parse_aliases, parse_registry

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 14, 8, 0, tzinfo=UTC)
HASH = "c" * 64
STATUSES = ("approved", "reported")
REGISTERS = ["registers"]
SITE = "urn:tizert:site:tizert"
GROUP = "urn:tizert:processgroup:grinding"
AREA = "urn:tizert:processarea:6220"
MILL = "urn:tizert:equipment:mill"
PUMP = "urn:tizert:equipment:pump"
STANDBY = "urn:tizert:equipment:standby"
DOC = "urn:tizert:documentreference:pid"
CONCEPT = "urn:tizert:concept:tt"
TAGS = [f"urn:tizert:instrumenttag:t{n:02d}" for n in range(30)]
KG_TABLES = (
    "kg.alias, kg.alias_set, kg.release_pointer, kg.release_graph, kg.edge_projection,"
    " kg.vertex_projection, kg.release_member, kg.release, kg.assertion_evidence,"
    " kg.assertion_status, kg.assertion, kg.entity_evidence, kg.evidence, kg.identifier,"
    " kg.entity_record, kg.entity, kg.run"
)
API = (
    "kg.active_release(text)",
    "kg.resolve(text,text,text[],integer)",
    "kg.lookup(text,text,text[],text[],integer)",
    "kg.entity(text,text,text[])",
    "kg.neighbors(text,text,text[],text,text,integer)",
    "kg.lineage(text,text,text[])",
    "kg.relations_between(text,text,text,text[])",
    "kg.provenance(text,text[],text[])",
    "kg.legend(text,text,text[])",
    "kg.entity_context(text,text,text[],integer)",
    "kg.context(text,text,text[],integer,integer)",
)


def _find(env: str, relative: str) -> Path:
    configured = os.environ.get(env)
    if configured:
        return Path(configured)
    for parent in Path(__file__).resolve().parents:
        if (parent / relative).exists():
            return parent / relative
    return Path(f"{relative}-not-found")


def _record(iri: str, kind: str, identifier: str, row: int, **extra: Any) -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "id": iri,
        "kind": kind,
        "identifier": identifier,
        "label": extra.get("label", identifier),
        "attributes": extra.get("attributes", {"Source": "test"}),
        "review_status": extra.get("status", "reported"),
        "provenance": [
            {
                "id": f"urn:tizert:evidence:row{row}",
                "row_number": row,
                "source_hash": HASH,
                "source_name": "Tag_Register.csv",
            }
        ],
    }


def _link(name: str, subject: str, predicate: str, obj: str, row: int) -> dict[str, Any]:
    record = _record(f"urn:tizert:assertion:{name}", "Assertion", "", row)
    for key in ("identifier", "label", "attributes"):
        record.pop(key)
    return {**record, "subject": subject, "predicate": predicate, "object": obj, "method": "test"}


def _registry(version: str = "v1") -> bytes:
    legend = {"Code / Prefix": "TT", "Name": f"Temperature transmitter {version}"}
    records = [
        _record(SITE, "Site", "Tizert", 1),
        _record(GROUP, "ProcessGroup", "Grinding", 2),
        _record(AREA, "ProcessArea", "6220", 3),
        _record(
            MILL,
            "Equipment",
            "6220-ML-2241",
            4,
            label="Primary Ball Mill",
            attributes={"Function Description": f"grinds ({version})"},
        ),
        _record(PUMP, "Equipment", "6220-PP-2250", 5, label="Mill Discharge Slurry Pump"),
        _record(
            STANDBY, "Equipment", "6220-PP-2251", 6, label="Mill Discharge Slurry Pump Standby"
        ),
        _record(DOC, "DocumentReference", "SP0904-6220-D-PIDS-001.pdf", 7),
        _record(CONCEPT, "Concept", "", 8, attributes=legend, status="candidate"),
        _link("site", GROUP, "inSite", SITE, 10),
        _link("group", AREA, "inProcessGroup", GROUP, 11),
        _link("mill-area", MILL, "reportedInArea", AREA, 12),
        _link("pump-area", PUMP, "reportedInArea", AREA, 13),
        _link("doc", MILL, "referencesDocument", DOC, 14),
        _link("monitor", PUMP, "measuresMonitorsOrControls", TAGS[0], 15),
    ]
    for number, tag in enumerate(TAGS):
        records.append(
            _record(
                tag,
                "InstrumentTag",
                f"6220-TT-{number:04d}",
                100 + number,
                attributes={"Tag Prefix": "TT"},
            )
        )
        records.append(_link(f"tag{number}", tag, "tagOf", MILL, 200 + number))
    return "\n".join(json.dumps(record) for record in records).encode()


@pytest.fixture
async def store(pg_pool: Any) -> AsyncIterator[PostgresKnowledgeStore]:
    async def reset() -> None:
        async with pg_pool.acquire() as connection:
            await connection.execute(f"TRUNCATE {KG_TABLES}")

    await reset()
    yield PostgresKnowledgeStore(pg_pool)
    await reset()


async def _release(store: PostgresKnowledgeStore, registry: bytes, at: datetime = NOW) -> str:
    await store.import_registry(parse_registry(registry), started_at=at, now=at)
    release = await store.build_release(statuses=STATUSES, started_at=at, now=at)
    return release.release_id


async def _as_reader(pg_pool: Any, sql: str, *args: Any) -> list[dict[str, Any]]:
    async with pg_pool.acquire() as connection, connection.transaction():
        await connection.execute("SET LOCAL ROLE tizert_kg_reader")
        return [dict(row) for row in await connection.fetch(sql, *args)]


async def test_reader_role_can_only_execute_the_api(
    store: PostgresKnowledgeStore, pg_pool: Any
) -> None:
    import asyncpg

    release = await _release(store, _registry())
    rows = await _as_reader(
        pg_pool, "SELECT * FROM kg.resolve($1, $2, $3)", release, "6220-ML-2241", REGISTERS
    )
    assert [row["iri"] for row in rows] == [MILL]
    async with pg_pool.acquire() as connection:
        async with connection.transaction():
            await connection.execute("SET LOCAL ROLE tizert_kg_reader")
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await connection.fetch("SELECT iri FROM kg.vertex_projection LIMIT 1")
        definers = await connection.fetch(
            "SELECT p.oid::regprocedure::text AS signature, p.proconfig,"
            " has_function_privilege('tizert_kg_reader', p.oid, 'EXECUTE') AS reader,"
            " EXISTS (SELECT 1 FROM aclexplode(coalesce(p.proacl, acldefault('f', p.proowner)))"
            " AS acl WHERE acl.grantee = 0) AS public"
            " FROM pg_proc AS p WHERE p.pronamespace = 'kg'::regnamespace AND p.prosecdef"
        )
    assert {row["signature"] for row in definers} == set(API)
    assert all(row["reader"] and not row["public"] for row in definers)
    assert all(row["proconfig"] == ["search_path=pg_catalog, pg_temp"] for row in definers)


async def test_resolution_tags_aliases_and_names(
    store: PostgresKnowledgeStore, pg_pool: Any
) -> None:
    release = await _release(store, _registry())
    aliases = parse_aliases(
        json.dumps(
            [
                {"alias": "SAG", "tag": "6210-ML-2175", "label": "SAG Mill", "note": "PFD"},
                {"alias": "ball mill", "tag": "6220ml2241"},
            ]
        ).encode()
    )
    first = await store.import_aliases(aliases, started_at=NOW, now=NOW)
    again = await store.import_aliases(aliases, started_at=NOW, now=NOW)
    assert (first.created, again.created, again.run_id) == (True, False, first.run_id)

    async def resolve(text: str, scopes: list[str] = REGISTERS) -> list[tuple[Any, ...]]:
        rows = await _as_reader(
            pg_pool, "SELECT * FROM kg.resolve($1, $2, $3)", release, text, scopes
        )
        return [(r["method"], r["identifier"], r["iri"], r["ambiguous"]) for r in rows]

    assert await resolve("voir 6220tt0003, puis 6220-ML-2241 et 9999-ZZ-9000") == [
        ("tag", "6220-TT-0003", TAGS[3], False),
        ("tag", "6220-ML-2241", MILL, False),
        ("tag", "9999-ZZ-9000", None, False),
    ]
    # A tag mention wins over an alias naming the same tag; unregistered alias targets stay.
    assert await resolve("le ball mill 6220-ML-2241 et le SAG") == [
        ("tag", "6220-ML-2241", MILL, False),
        ("alias", "6210-ML-2175", None, False),
    ]
    sag = await _as_reader(pg_pool, "SELECT * FROM kg.resolve($1, 'SAG', $2)", release, REGISTERS)
    assert (sag[0]["name"], sag[0]["alias_note"]) == ("SAG Mill", "PFD")
    assert await resolve("la mill discharge slurry pump") == [("name", "6220-PP-2250", PUMP, False)]
    assert await resolve("Mill discharge slurry pump standby") == [
        ("name", "6220-PP-2250", PUMP, True),
        ("name", "6220-PP-2251", STANDBY, True),
    ]
    assert await resolve("6220-ML-2241", ["hr"]) == [("tag", "6220-ML-2241", None, False)]

    emptied = await store.import_aliases(parse_aliases(b"[]"), started_at=NOW, now=NOW)
    assert emptied.created
    assert await resolve("le SAG") == []
    restored = await store.import_aliases(aliases, started_at=NOW, now=NOW)
    assert restored.created
    assert restored.run_id != first.run_id
    assert await resolve("le SAG") == [("alias", "6210-ML-2175", None, False)]

    lookup = await _as_reader(
        pg_pool,
        "SELECT iri FROM kg.lookup($1, $2, $3, $4)",
        release,
        " 6220 ",
        REGISTERS,
        ["ProcessArea"],
    )
    assert [row["iri"] for row in lookup] == [AREA]


async def test_bounded_reads_filter_every_hop(store: PostgresKnowledgeStore, pg_pool: Any) -> None:
    release = await _release(store, _registry())
    async with pg_pool.acquire() as connection:
        await connection.execute(
            "UPDATE kg.vertex_projection SET scope_keys = '{hr}'"
            " WHERE release_id = $1 AND iri = $2",
            release,
            TAGS[5],
        )

    children = await _as_reader(
        pg_pool,
        "SELECT * FROM kg.neighbors($1, $2, $3, 'TAG_OF', 'in', 50)",
        release,
        MILL,
        REGISTERS,
    )
    assert len(children) == 29
    assert {row["total"] for row in children} == {29}
    assert TAGS[5] not in {row["iri"] for row in children}
    first = await _as_reader(
        pg_pool, "SELECT * FROM kg.neighbors($1, $2, $3, p_limit => 2)", release, MILL, REGISTERS
    )
    assert [(r["relation"], r["direction"], r["identifier"], r["total"]) for r in first] == [
        ("REFERENCES_DOCUMENT", "out", "SP0904-6220-D-PIDS-001.pdf", 1),
        ("REPORTED_IN_AREA", "out", "6220", 1),
        ("TAG_OF", "in", "6220-TT-0000", 29),
        ("TAG_OF", "in", "6220-TT-0001", 29),
    ]

    async def lineage(iri: str, scopes: list[str]) -> list[tuple[Any, ...]]:
        rows = await _as_reader(
            pg_pool, "SELECT * FROM kg.lineage($1, $2, $3)", release, iri, scopes
        )
        return [(r["depth"], r["relation"], r["identifier"], r["status"]) for r in rows]

    assert await lineage(TAGS[0], REGISTERS) == [
        (1, "TAG_OF", "6220-ML-2241", "reported"),
        (2, "REPORTED_IN_AREA", "6220", "reported"),
        (3, "IN_PROCESS_GROUP", "Grinding", "reported"),
        (4, "IN_SITE", "Tizert", "reported"),
    ]
    assert await lineage(TAGS[5], REGISTERS) == []
    assert await lineage(TAGS[5], ["hr"]) == []  # its TAG_OF edge is a register fact
    assert len(await lineage(TAGS[5], ["hr", "registers"])) == 4

    between = await _as_reader(
        pg_pool,
        "SELECT * FROM kg.relations_between($1, $2, $3, $4)",
        release,
        TAGS[0],
        PUMP,
        REGISTERS,
    )
    assert [(r["relation"], r["direction"]) for r in between] == [
        ("MEASURES_MONITORS_OR_CONTROLS", "in")
    ]
    none = await _as_reader(
        pg_pool,
        "SELECT * FROM kg.relations_between($1, $2, $3, $4)",
        release,
        MILL,
        PUMP,
        REGISTERS,
    )
    assert none == []

    provenance = await _as_reader(
        pg_pool,
        "SELECT * FROM kg.provenance($1, $2, $3)",
        release,
        ["urn:tizert:assertion:tag0", "urn:tizert:assertion:tag5", "urn:tizert:assertion:nope"],
        REGISTERS,
    )
    assert [
        (r["assertion_iri"], r["source_name"], r["source_row"], r["status"]) for r in provenance
    ] == [("urn:tizert:assertion:tag0", "Tag_Register.csv", 200, "reported")]


async def test_attributes_and_legend_are_read_as_of_the_release(
    store: PostgresKnowledgeStore, pg_pool: Any
) -> None:
    first = await _release(store, _registry("v1"))
    later = NOW + timedelta(hours=1)
    second = await _release(store, _registry("v2"), at=later)
    assert second != first  # same graph, new records: a new release, never a silent reuse

    async def card(release: str) -> tuple[Any, ...]:
        entity = await _as_reader(
            pg_pool, "SELECT * FROM kg.entity($1, $2, $3)", release, MILL, REGISTERS
        )
        legend = await _as_reader(
            pg_pool, "SELECT * FROM kg.legend($1, ' tt ', $2)", release, REGISTERS
        )
        return (
            entity[0]["attributes"],
            [source["row_number"] for source in entity[0]["sources"]],
            [(row["name"], row["status"], row["iri"]) for row in legend],
        )

    assert await card(first) == (
        {"Function Description": "grinds (v1)"},
        [4],
        [("Temperature transmitter v1", "candidate", CONCEPT)],
    )
    assert (await card(second))[2] == [("Temperature transmitter v2", "candidate", CONCEPT)]
    assert (
        await _as_reader(pg_pool, "SELECT * FROM kg.entity($1, $2, $3)", first, MILL, ["hr"]) == []
    )
    assert await _as_reader(pg_pool, "SELECT * FROM kg.legend($1, 'TT', $2)", first, ["hr"]) == []

    await store.activate(second, environment="pilot", now=later)
    active = await _as_reader(pg_pool, "SELECT * FROM kg.active_release('pilot')")
    assert [row["release_id"] for row in active] == [second]


async def test_context_bundles_resolution_and_grounding(
    store: PostgresKnowledgeStore, pg_pool: Any
) -> None:
    release = await _release(store, _registry())
    rows = await _as_reader(
        pg_pool,
        "SELECT kg.context($1, $2, $3, p_children => 2) AS context",
        release,
        "6220-TT-0003 sur 6220-ML-2241 ou 9999-ZZ-9000 ?",
        REGISTERS,
    )
    context = rows[0]["context"]
    tag, mill, unknown = context["matches"]
    assert (context["release_id"], context["ambiguous"]) == (release, False)
    assert tag["context"]["parent"]["identifier"] == "6220-ML-2241"
    assert [(a["identifier"], a["groups"]) for a in tag["context"]["areas"]] == [
        ("6220", [{"identifier": "Grinding", "name": "Grinding"}])
    ]
    assert [entry["name"] for entry in tag["context"]["legend"]] == ["Temperature transmitter v1"]
    assert mill["context"]["parent"] is None
    assert mill["context"]["children"] == {
        "total": 30,
        "items": [
            {"iri": TAGS[0], "identifier": "6220-TT-0000", "name": "6220-TT-0000"},
            {"iri": TAGS[1], "identifier": "6220-TT-0001", "name": "6220-TT-0001"},
        ],
    }
    assert [d["identifier"] for d in mill["context"]["documents"]] == ["SP0904-6220-D-PIDS-001.pdf"]
    assert mill["context"]["sources"][0]["row_number"] == 4
    assert (unknown["identifier"], unknown["iri"], unknown["context"]) == (
        "9999-ZZ-9000",
        None,
        None,
    )
    hidden = await _as_reader(
        pg_pool, "SELECT kg.entity_context($1, $2, $3) AS context", release, MILL, ["hr"]
    )
    assert hidden[0]["context"] is None


# --- Synthetic RDF evaluation pack against the full registry -------------------------------

ONTOLOGY = "urn:tizert:ontology:"
LABEL_OF = {f"{ONTOLOGY}{predicate}": label for predicate, label in EDGE_LABELS.items()}
PREDICATE_OF = {label: urn for urn, label in LABEL_OF.items()}
TAG_IN_TEXT = re.compile(r"(?<![A-Z0-9])([0-9]{4})-?([A-Z]{1,5})-?([0-9]{3,4}[A-Z]?)(?![A-Z0-9])")
# A qualified power figure ("75 kW"); the register only describes equipment in words.
TECHNICAL = re.compile(r"\d+(?:[.,]\d+)?\s*(?:kw|mw|hp)\b", re.IGNORECASE)


class Gate:
    """The agent's calls, as the reader role, with per-function timings."""

    def __init__(self, connection: Any, release: str) -> None:
        self._connection = connection
        self._release = release
        self.timings: dict[str, list[float]] = defaultdict(list)

    async def _fetch(self, function: str, sql: str, *args: Any) -> list[dict[str, Any]]:
        started = time.perf_counter()
        rows = await self._connection.fetch(sql, self._release, *args)
        self.timings[function].append((time.perf_counter() - started) * 1000)
        return [dict(row) for row in rows]

    async def resolve(self, text: str) -> list[dict[str, Any]]:
        return await self._fetch("resolve", "SELECT * FROM kg.resolve($1, $2, $3)", text, REGISTERS)

    async def lookup(self, value: str, kind: str) -> list[dict[str, Any]]:
        return await self._fetch(
            "lookup", "SELECT * FROM kg.lookup($1, $2, $3, $4)", value, REGISTERS, [kind]
        )

    async def entity(self, iri: str) -> list[dict[str, Any]]:
        return await self._fetch("entity", "SELECT * FROM kg.entity($1, $2, $3)", iri, REGISTERS)

    async def neighbors(
        self, iri: str, relation: str, direction: str, limit: int = 25
    ) -> list[dict[str, Any]]:
        return await self._fetch(
            "neighbors",
            "SELECT * FROM kg.neighbors($1, $2, $3, $4, $5, $6)",
            iri,
            REGISTERS,
            relation,
            direction,
            limit,
        )

    async def lineage(self, iri: str) -> list[dict[str, Any]]:
        return await self._fetch("lineage", "SELECT * FROM kg.lineage($1, $2, $3)", iri, REGISTERS)

    async def context(self, text: str) -> list[dict[str, Any]]:
        return await self._fetch("context", "SELECT kg.context($1, $2, $3)", text, REGISTERS)

    async def between(self, a: str, b: str) -> list[dict[str, Any]]:
        return await self._fetch(
            "relations_between",
            "SELECT * FROM kg.relations_between($1, $2, $3, $4)",
            a,
            b,
            REGISTERS,
        )

    async def provenance(self, iris: list[str]) -> list[dict[str, Any]]:
        return await self._fetch(
            "provenance", "SELECT * FROM kg.provenance($1, $2, $3)", iris, REGISTERS
        )

    async def legend(self, prefix: str) -> list[dict[str, Any]]:
        return await self._fetch("legend", "SELECT * FROM kg.legend($1, $2, $3)", prefix, REGISTERS)


def _kind(iri: str) -> str:
    return iri.split(":")[2]


def _row(value: Any) -> int | None:
    return None if value in (None, "") else int(value)


async def _grade(gate: Gate, case: dict[str, Any]) -> list[str]:
    family = case["family"]
    ids: dict[str, str] = case.get("entity_identifiers") or {}
    iris: dict[str, list[str]] = defaultdict(list)
    for iri in ids:
        iris[_kind(iri)].append(iri)
    oracle = case.get("oracle_expected_values")
    expected = case.get("expected_assertions") or []
    problems: list[str] = []

    resolved = await gate.resolve(case["question_fr"])
    by_identifier = {row["identifier"]: row for row in resolved}
    iri_of = {identifier: iri for iri, identifier in ids.items()}
    for match in TAG_IN_TEXT.finditer(case["question_fr"].upper()):
        tag = "-".join(match.groups())
        if tag not in iri_of and family != "unknown_tag":
            continue
        row = by_identifier.get(tag)
        want = iri_of.get(tag)
        if row is None or row["iri"] != want:
            problems.append(f"resolve {tag}: {row and row['iri']} != {want}")

    if expected:
        rows = await gate.provenance([a["assertion_id"] for a in expected])
        support: dict[str, set[tuple[Any, ...]]] = defaultdict(set)
        triple: dict[str, tuple[Any, ...]] = {}
        for row in rows:
            support[row["assertion_iri"]].add(
                (row["evidence_iri"], row["source_name"], row["source_hash"], row["source_row"])
            )
            triple[row["assertion_iri"]] = (
                row["subject_iri"],
                ONTOLOGY + row["predicate"],
                row["object_iri"],
                row["status"],
            )
        for assertion in expected:
            name = assertion["assertion_id"]
            want = (
                assertion["subject"],
                assertion["predicate"],
                assertion["object"],
                assertion["review_status"],
            )
            if triple.get(name) != want:
                problems.append(f"assertion {name}: {triple.get(name)} != {want}")
            sources = {
                (p["evidence_id"], p["source_name"], p["source_hash"], _row(p.get("row_number")))
                for p in assertion["provenance"]
            }
            if support[name] != sources:
                problems.append(f"provenance {name}: {sorted(support[name])} != {sorted(sources)}")

    for iri in case.get("expected_entity_ids") or []:
        if _kind(iri) != "concept" and len(await gate.entity(iri)) != 1:
            problems.append(f"entity {iri} not visible")

    def same(got: list[Any], want: Any, what: str, ordered: bool = False) -> None:
        if (got if ordered else sorted(got)) != (want if ordered else sorted(want or [])):
            problems.append(f"{what}: {got} != {want}")

    if family == "instrument_parent":
        rows = await gate.lineage(iris["instrumenttag"][0])
        same([r["identifier"] for r in rows if r["depth"] == 1], oracle, "parent")
    elif family in ("equipment_area", "cross_prefix_area"):
        rows = await gate.lineage(iris["equipment"][0])
        same([r["identifier"] for r in rows if r["relation"] == "REPORTED_IN_AREA"], oracle, "area")
    elif family == "instrument_area_two_hop":
        rows = await gate.lineage(iris["instrumenttag"][0])
        same([r["identifier"] for r in rows if r["relation"] == "REPORTED_IN_AREA"], oracle, "area")
    elif family == "instrument_group_three_hop":
        rows = await gate.lineage(iris["instrumenttag"][0])
        same([r["name"] for r in rows if r["relation"] == "IN_PROCESS_GROUP"], oracle, "group")
    elif family == "area_process_group":
        area = iris["processarea"][0]
        found = await gate.lookup(ids[area], "ProcessArea")
        same([r["iri"] for r in found], [area], "area lookup")
        rows = await gate.neighbors(area, "IN_PROCESS_GROUP", "out")
        same([r["name"] for r in rows], oracle, "group")
    elif family == "assertion_provenance":
        first = expected[0]
        links = await gate.between(first["subject"], first["object"])
        backing = [
            iri
            for link in links
            if link["direction"] == "out" and link["relation"] == LABEL_OF[first["predicate"]]
            for iri in link["assertion_iris"]
        ]
        rows = await gate.provenance(backing)
        same([f"{r['source_name']}#{r['source_row']}" for r in rows], oracle, "provenance")
    elif family == "bounded_area_members":
        area = expected[0]["object"]
        code = (await gate.entity(area))[0]["identifier"]
        same([r["iri"] for r in await gate.lookup(code, "ProcessArea")], [area], "area lookup")
        rows = await gate.neighbors(area, "REPORTED_IN_AREA", "in", 5)
        same([r["identifier"] for r in rows], oracle, "members", ordered=True)
    elif family == "bounded_equipment_children":
        rows = await gate.neighbors(expected[0]["object"], "TAG_OF", "in", 5)
        same([r["identifier"] for r in rows], oracle, "children", ordered=True)
    elif family == "document_reference":
        rows = await gate.neighbors(iris["equipment"][0], "REFERENCES_DOCUMENT", "out")
        same([r["identifier"] for r in rows], oracle, "documents")
    elif family in ("actuation_relation", "monitoring_relation"):
        links = await gate.between(iris["equipment"][0], iris["instrumenttag"][0])
        same(
            [PREDICATE_OF[r["relation"]] for r in links if r["direction"] == "out"], oracle, "link"
        )
    elif family == "legend_lookup":
        rows = await gate.legend(case["legend_prefix"])
        same([r["iri"] for r in rows], case["expected_entity_ids"], "legend concepts")
        names = {r["name"] for r in rows}
        if not set(case["expected_legend_names"]) <= names:
            problems.append(f"legend names {sorted(names)}")
        same(
            [json.dumps(r["attributes"], sort_keys=True) for r in rows],
            [json.dumps(json.loads(value), sort_keys=True) for value in oracle],
            "legend records",
        )
    elif family in ("normalized_entity_resolution", "ontology_document_bridge"):
        want = case.get("expected_canonical_tag") or oracle[0]
        tags = [r for r in resolved if r["method"] == "tag"]
        if not tags or tags[0]["identifier"] != want or tags[0]["iri"] != iri_of.get(want):
            problems.append(f"canonical {tags[:1]} != {want}")
    elif family == "curated_alias_resolution":
        aliases = [r for r in resolved if r["method"] == "alias"]
        if [(r["identifier"], r["iri"]) for r in aliases] != [
            (case["expected_canonical_tag"], None)
        ]:
            problems.append(f"alias {aliases}")
    elif family == "unknown_tag":
        if not resolved or any(row["iri"] for row in resolved):
            problems.append(f"unknown tag resolved {resolved}")
    elif family == "no_invented_process_flow":
        first, second = iris["equipment"][:2]
        if links := await gate.between(first, second):
            problems.append(f"invented link {links}")
    elif family == "unsupported_technical_value":
        attributes = (await gate.entity(iris["equipment"][0]))[0]["attributes"]
        if any(TECHNICAL.search(f"{key} {value}") for key, value in attributes.items()):
            problems.append("register carries a technical value")
    elif family != "reported_not_approved":
        raise AssertionError(f"ungraded family {family}")
    return problems


def _percentile(values: list[float], share: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(share * (len(ordered) - 1)))]


async def test_rdf_synthetic_registry_cases_pass_on_the_live_functions(
    store: PostgresKnowledgeStore, pg_pool: Any
) -> None:
    registry = _find("TIZ_TEST_KG_REGISTRY", "data/knowledge/runs/2026-10-07/registry.jsonl")
    cases_path = _find("TIZ_TEST_RDF_CASES", "data/Golden/rdf_synthetic/questions.jsonl")
    aliases_path = _find("TIZ_TEST_KG_ALIASES", "data/knowledge/agent/aliases.json")
    missing = [path for path in (registry, cases_path, aliases_path) if not path.exists()]
    if missing:
        pytest.skip(f"evaluation inputs not available: {missing}")
    release = await _release(store, registry.read_bytes())
    await store.import_aliases(parse_aliases(aliases_path.read_bytes()), started_at=NOW, now=NOW)
    cases = [
        json.loads(line)
        for line in cases_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    graded = [case for case in cases if case.get("oracle_type") in ("select", "ask")]

    outcomes: Counter[tuple[str, str, bool]] = Counter()
    failures: list[str] = []
    async with pg_pool.acquire() as connection, connection.transaction():
        await connection.execute("SET LOCAL ROLE tizert_kg_reader")
        gate = Gate(connection, release)
        for case in graded:
            problems = await _grade(gate, case)
            await gate.context(case["question_fr"])
            outcomes[(case["family"], case["split"], not problems)] += 1
            failures.extend(f"{case['question_id']} {case['family']}: {p}" for p in problems)

    families = sorted({family for family, _, _ in outcomes})
    summary = {
        family: {
            split: f"{outcomes[(family, split, True)]}/"
            f"{outcomes[(family, split, True)] + outcomes[(family, split, False)]}"
            for split in ("dev", "heldout")
            if outcomes[(family, split, True)] + outcomes[(family, split, False)]
        }
        for family in families
    }
    latency = {
        name: {
            "calls": len(values),
            "p50_ms": round(statistics.median(values), 2),
            "p95_ms": round(_percentile(values, 0.95), 2),
        }
        for name, values in sorted(gate.timings.items())
    }
    print(json.dumps({"cases": len(graded), "families": summary, "latency": latency}, indent=1))
    assert len(graded) == 221
    assert not failures, "\n".join(failures[:40])
