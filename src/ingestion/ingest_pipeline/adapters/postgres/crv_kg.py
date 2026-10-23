"""CRV knowledge graph on PostgreSQL: import a KG-1 build run, freeze release projections and
load them into Apache AGE (one graph per release).

A build run is a complete snapshot of what the deterministic tier currently reports: entities
and assertions it no longer reports are recorded as ``superseded`` (unless an expert decision
backs them), so releases only show current facts. Graph and label names are interpolated into
SQL only after matching fixed patterns; every value travels as a bound parameter.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ...domain.crv_kg.model import (
    KgRun,
    ProjectedAssertion,
    ProjectedEntity,
    Projection,
    build_projection,
)
from ...domain.crv_kg.vocab import (
    EXTRACTED_KINDS,
    EXTRACTED_METHODS,
    PROJECTION_VERSION,
    TOOL,
    TOOL_VERSION,
    fold,
)
from ...domain.errors import IntegrityError, InvalidInputError
from .pool import PgConnection, PgPool

_GRAPH = re.compile(r"^crv_research_[0-9a-f]{12}$")
_VERTEX_LABEL = re.compile(r"^[A-Z][A-Za-z]+$")
_EDGE_LABEL = re.compile(r"^[A-Z][A-Z_]+$")
_LATEST_RECORDS = (
    "SELECT DISTINCT ON (entity_iri) entity_iri, label, attributes, review_status"
    " FROM crv_kg.entity_record ORDER BY entity_iri, recorded_at DESC, run_id DESC"
)


@dataclass(frozen=True)
class ImportReport:
    run_id: str
    run_created: bool
    entities: int
    evidence: int
    assertions: int
    statuses: int
    superseded_entities: int
    superseded_assertions: int


@dataclass(frozen=True)
class ReleaseRef:
    release_id: str
    manifest_sha256: str
    graph_name: str
    vertex_count: int
    edge_count: int
    created: bool


def _quoted(name: str, pattern: re.Pattern[str]) -> str:
    if not pattern.match(name):
        raise InvalidInputError(f"Refusing identifier {name!r}")
    return f'"{name}"'


async def _use_age(connection: PgConnection) -> None:
    # Azure preloads AGE (shared_preload_libraries); never run LOAD 'age' there.
    await connection.execute('SET LOCAL search_path = ag_catalog, "$user", public')


def _normalized(scheme: str, value: str) -> str:
    return value.lower() if scheme == "content_hash" else (fold(value) or value.strip().lower())


class CrvKgStore:
    def __init__(self, pool: PgPool) -> None:
        self._pool = pool

    # --- Import ----------------------------------------------------------------------------

    async def import_run(self, run: KgRun, *, started_at: datetime, now: datetime) -> ImportReport:
        """Record a build run; the same inputs are the same run and add nothing."""

        problems = run.validate()
        if problems:
            raise InvalidInputError(f"Refusing an invalid run: {problems[:5]}")
        run_id = run.run_id
        async with self._pool.acquire() as connection, connection.transaction():
            await connection.execute("SELECT pg_advisory_xact_lock(hashtext('crv-kg-import'))")
            created = (
                await connection.fetchval(
                    "INSERT INTO crv_kg.run (run_id, kind, tool, tool_version, input_hash, config,"
                    " started_at, completed_at) VALUES ($1, 'build', $2, $3, $4, $5::jsonb, $6,"
                    " $7) ON CONFLICT (run_id) DO NOTHING RETURNING true",
                    run_id, TOOL, TOOL_VERSION, run.input_hash, dict(run.config), started_at, now,
                )
                is True
            )
            if not created:
                return ImportReport(run_id, False, 0, 0, 0, 0, 0, 0)
            evidence = sorted(run.evidence, key=lambda item: item.iri)
            new_evidence = await connection.fetchval(
                "WITH rows AS (INSERT INTO crv_kg.evidence (iri, source_hash, source_name,"
                " locator_type, locator, scope_key, run_id, recorded_at)"
                " SELECT x.iri, x.source_hash, x.source_name, x.locator_type, x.locator::jsonb,"
                " x.scope_key, $7, $8 FROM unnest($1::text[], $2::text[], $3::text[], $4::text[],"
                " $5::text[], $6::text[]) AS x(iri, source_hash, source_name, locator_type,"
                " locator, scope_key) ON CONFLICT (iri) DO NOTHING RETURNING 1)"
                " SELECT count(*) FROM rows",
                [item.iri for item in evidence],
                [item.source_hash for item in evidence],
                [item.source_name for item in evidence],
                [item.locator_type for item in evidence],
                [json.dumps(dict(item.locator), ensure_ascii=False) for item in evidence],
                [item.scope_key for item in evidence],
                run_id,
                now,
            )
            entities = sorted(run.entities, key=lambda item: item.iri)
            new_entities = await connection.fetchval(
                "WITH rows AS (INSERT INTO crv_kg.entity (iri, kind, created_by_run, created_at)"
                " SELECT x.iri, x.kind, $3, $4 FROM unnest($1::text[], $2::text[]) AS x(iri, kind)"
                " ON CONFLICT (iri) DO NOTHING RETURNING 1) SELECT count(*) FROM rows",
                [item.iri for item in entities],
                [item.kind for item in entities],
                run_id,
                now,
            )
            await _check_kinds(connection, entities)
            await connection.execute(
                "INSERT INTO crv_kg.entity_record (entity_iri, run_id, review_status, label,"
                " attributes, recorded_at) SELECT x.iri, $5, x.status, x.label,"
                " x.attributes::jsonb, $6 FROM unnest($1::text[], $2::text[], $3::text[],"
                " $4::text[]) AS x(iri, status, label, attributes)",
                [item.iri for item in entities],
                [item.status for item in entities],
                [item.label for item in entities],
                [json.dumps(dict(item.attributes), ensure_ascii=False, sort_keys=True)
                 for item in entities],
                run_id,
                now,
            )
            superseded_entities = await connection.fetchval(
                "WITH rows AS (INSERT INTO crv_kg.entity_record (entity_iri, run_id,"
                " review_status, label, attributes, recorded_at)"
                f" SELECT latest.entity_iri, $2, 'superseded', latest.label, latest.attributes, $3"
                f" FROM ({_LATEST_RECORDS}) AS latest"
                " JOIN crv_kg.entity AS entity ON entity.iri = latest.entity_iri"
                " WHERE latest.review_status <> 'superseded'"
                " AND NOT (entity.kind = ANY ($4::text[]))"
                " AND NOT (latest.entity_iri = ANY ($1::text[])) RETURNING 1)"
                " SELECT count(*) FROM rows",
                [item.iri for item in entities],
                run_id,
                now,
                sorted(EXTRACTED_KINDS),
            )
            keys = sorted({
                (entity.iri, scheme, value, _normalized(scheme, value))
                for entity in entities for scheme, value in entity.identifiers
            })
            await connection.execute(
                "INSERT INTO crv_kg.identifier (entity_iri, scheme, value_raw, value_normalized,"
                " run_id) SELECT x.iri, x.scheme, x.raw, x.normalized, $5"
                " FROM unnest($1::text[], $2::text[], $3::text[], $4::text[])"
                " AS x(iri, scheme, raw, normalized) ON CONFLICT DO NOTHING",
                [key[0] for key in keys],
                [key[1] for key in keys],
                [key[2] for key in keys],
                [key[3] for key in keys],
                run_id,
            )
            links = sorted({(e.iri, item) for e in entities for item in e.evidence})
            await connection.execute(
                "INSERT INTO crv_kg.entity_evidence (entity_iri, evidence_iri, run_id)"
                " SELECT x.entity, x.evidence, $3 FROM unnest($1::text[], $2::text[])"
                " AS x(entity, evidence) ON CONFLICT DO NOTHING",
                [entity for entity, _ in links],
                [item for _, item in links],
                run_id,
            )
            assertions = sorted(run.assertions, key=lambda item: item.iri)
            new_assertions = await connection.fetchval(
                "WITH rows AS (INSERT INTO crv_kg.assertion (iri, subject_iri, predicate,"
                " object_iri, method, generated_by_run, recorded_at)"
                " SELECT x.iri, x.subject, x.predicate, x.object, x.method, $6, $7"
                " FROM unnest($1::text[], $2::text[], $3::text[], $4::text[], $5::text[])"
                " AS x(iri, subject, predicate, object, method)"
                " ON CONFLICT (iri) DO NOTHING RETURNING 1) SELECT count(*) FROM rows",
                [item.iri for item in assertions],
                [item.subject for item in assertions],
                [item.predicate for item in assertions],
                [item.object for item in assertions],
                [item.method for item in assertions],
                run_id,
                now,
            )
            await _check_triples(connection, assertions)
            current = {
                row["assertion_iri"]: (row["status"], row["decision_id"])
                for row in await connection.fetch(
                    "SELECT current.assertion_iri, current.status, current.decision_id"
                    " FROM crv_kg.assertion_current AS current"
                    " JOIN crv_kg.assertion AS assertion ON assertion.iri = current.assertion_iri"
                    " WHERE NOT (assertion.method = ANY ($1::text[]))",
                    sorted(EXTRACTED_METHODS),
                )
            }
            # An expert decision is never overwritten by a build.
            changed = [
                item for item in assertions
                if (state := current.get(item.iri)) is None
                or (state[1] is None and state[0] != item.status)
            ]
            present = {item.iri for item in assertions}
            gone = sorted(
                iri for iri, (status, decision) in current.items()
                if iri not in present and decision is None and status != "superseded"
            )
            rows = [(item.iri, item.status) for item in changed] + [
                (iri, "superseded") for iri in gone
            ]
            await connection.execute(
                "INSERT INTO crv_kg.assertion_status (assertion_iri, status, run_id, recorded_at)"
                " SELECT x.iri, x.status, $3, $4"
                " FROM unnest($1::text[], $2::text[]) WITH ORDINALITY AS x(iri, status, n)"
                " ORDER BY x.n",
                [iri for iri, _ in rows],
                [status for _, status in rows],
                run_id,
                now,
            )
            support = sorted({(a.iri, item) for a in assertions for item in a.evidence})
            await connection.execute(
                "INSERT INTO crv_kg.assertion_evidence (assertion_iri, evidence_iri, run_id)"
                " SELECT x.assertion, x.evidence, $3 FROM unnest($1::text[], $2::text[])"
                " AS x(assertion, evidence) ON CONFLICT DO NOTHING",
                [assertion for assertion, _ in support],
                [item for _, item in support],
                run_id,
            )
        return ImportReport(
            run_id, True, int(new_entities), int(new_evidence), int(new_assertions),
            len(changed), int(superseded_entities), len(gone),
        )

    # --- Release ---------------------------------------------------------------------------

    async def current_projection(self, *, statuses: tuple[str, ...]) -> Projection:
        """Project the latest records and current assertion statuses (one snapshot).

        Single-table reads joined here: right after a bulk import, table statistics are stale
        and join plans unstable.
        """

        async with self._pool.acquire() as connection, connection.transaction():
            await connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            kinds = {
                row["iri"]: row["kind"]
                for row in await connection.fetch(
                    "SELECT iri, kind FROM crv_kg.entity WHERE state = 'active'"
                )
            }
            records = await connection.fetch(_LATEST_RECORDS)
            inputs = [
                row["run_id"] for row in await connection.fetch(
                    "SELECT run_id FROM crv_kg.run WHERE kind = 'build' ORDER BY completed_at DESC"
                    " LIMIT 1"
                )
            ]
            scope_of = {
                row["iri"]: row["scope_key"]
                for row in await connection.fetch("SELECT iri, scope_key FROM crv_kg.evidence")
            }
            entity_links = await connection.fetch(
                "SELECT entity_iri, evidence_iri FROM crv_kg.entity_evidence"
            )
            status_of = {
                row["assertion_iri"]: row["status"]
                for row in await connection.fetch(
                    "SELECT assertion_iri, status FROM crv_kg.assertion_current"
                    " WHERE status = ANY ($1::text[])",
                    list(statuses),
                )
            }
            assertion_rows = await connection.fetch(
                "SELECT iri, subject_iri, predicate, object_iri, method FROM crv_kg.assertion"
            )
            support = await connection.fetch(
                "SELECT assertion_iri, evidence_iri FROM crv_kg.assertion_evidence"
            )
            observations = await connection.fetch(
                "SELECT item.iri, item.document_iri, state.status FROM crv_kg.observation AS item"
                " JOIN crv_kg.observation_current AS state ON state.observation_iri = item.iri"
            )
        entity_scopes = _scopes(entity_links, "entity_iri", scope_of)
        assertion_scopes = _scopes(support, "assertion_iri", scope_of)
        entities = [
            ProjectedEntity(
                row["entity_iri"], kinds[row["entity_iri"]], row["label"], row["review_status"],
                _properties(row["attributes"]), entity_scopes.get(row["entity_iri"], ()),
            )
            for row in records
            if row["entity_iri"] in kinds
        ]
        assertions = [
            ProjectedAssertion(
                row["iri"], row["subject_iri"], row["predicate"], row["object_iri"],
                row["method"], status_of[row["iri"]], assertion_scopes.get(row["iri"], ()),
            )
            for row in assertion_rows
            if row["iri"] in status_of
        ]
        return build_projection(
            entities, assertions, statuses=statuses, inputs=inputs,
            observations=[(r["iri"], r["document_iri"], r["status"]) for r in observations],
        )

    async def build_release(
        self, *, statuses: tuple[str, ...], started_at: datetime, now: datetime
    ) -> ReleaseRef:
        """Freeze the current projection; an unchanged projection returns its existing release."""

        projection = await self.current_projection(statuses=statuses)
        manifest = projection.manifest_sha256
        release_id = projection.release_id(now)
        async with self._pool.acquire() as connection, connection.transaction():
            existing = await connection.fetchrow(
                "SELECT release_id, vertex_count, edge_count FROM crv_kg.release"
                " WHERE manifest_sha256 = $1",
                manifest,
            )
            if existing is not None:
                return ReleaseRef(existing["release_id"], manifest, projection.graph_name,
                                  existing["vertex_count"], existing["edge_count"], created=False)
            run_id = f"urn:crv:run:{manifest}"
            await connection.execute(
                "INSERT INTO crv_kg.run (run_id, kind, tool, tool_version, input_hash, config,"
                " started_at, completed_at) VALUES ($1, 'release', 'crv-kg-release', $2, $3,"
                " $4::jsonb, $5, $6)",
                run_id, PROJECTION_VERSION, manifest,
                {"statuses": list(projection.statuses), "inputs": list(projection.inputs)},
                started_at, now,
            )
            await connection.execute(
                "INSERT INTO crv_kg.release (release_id, manifest_sha256, projection_version,"
                " statuses, vertex_count, edge_count, built_by_run, created_at)"
                " VALUES ($1, $2, $3, $4::text[], $5, $6, $7, $8)",
                release_id, manifest, PROJECTION_VERSION, list(projection.statuses),
                len(projection.vertices), len(projection.edges), run_id, now,
            )
            members = sorted(projection.members.items())
            await connection.execute(
                "INSERT INTO crv_kg.release_member (release_id, assertion_iri, status)"
                " SELECT $1, x.iri, x.status FROM unnest($2::text[], $3::text[]) AS x(iri, status)",
                release_id,
                [iri for iri, _ in members],
                [status for _, status in members],
            )
            await connection.execute(
                "INSERT INTO crv_kg.vertex_projection (release_id, iri, label, properties,"
                " scope_keys) SELECT $1, x.iri, x.label, x.properties::jsonb, x.scopes::text[]"
                " FROM unnest($2::text[], $3::text[], $4::text[], $5::text[])"
                " AS x(iri, label, properties, scopes)",
                release_id,
                [v.iri for v in projection.vertices],
                [v.label for v in projection.vertices],
                [json.dumps({**v.properties, "release_id": release_id}, ensure_ascii=False)
                 for v in projection.vertices],
                [_text_array(v.scope_keys) for v in projection.vertices],
            )
            await connection.execute(
                "INSERT INTO crv_kg.edge_projection (release_id, label, start_iri, end_iri,"
                " properties, scope_keys, assertion_iris)"
                " SELECT $1, x.label, x.start_iri, x.end_iri, x.properties::jsonb,"
                " x.scopes::text[], x.assertions::text[]"
                " FROM unnest($2::text[], $3::text[], $4::text[], $5::text[], $6::text[],"
                " $7::text[]) AS x(label, start_iri, end_iri, properties, scopes, assertions)",
                release_id,
                [e.label for e in projection.edges],
                [e.start for e in projection.edges],
                [e.end for e in projection.edges],
                [json.dumps({**e.properties, "release_id": release_id}) for e in projection.edges],
                [_text_array(e.scope_keys) for e in projection.edges],
                [_text_array(e.assertion_iris) for e in projection.edges],
            )
            await connection.execute(
                "INSERT INTO crv_kg.release_observation (release_id, observation_iri, status)"
                " SELECT $1, x.iri, x.status FROM unnest($2::text[], $3::text[]) AS x(iri, status)",
                release_id,
                [iri for iri, _ in projection.observations],
                [status for _, status in projection.observations],
            )
        return ReleaseRef(release_id, manifest, projection.graph_name, len(projection.vertices),
                          len(projection.edges), created=True)

    # --- AGE load --------------------------------------------------------------------------

    async def load_age(self, release_id: str, *, now: datetime) -> str:
        """Bulk-load a release into its own AGE graph (set-based, ids assigned up front)."""

        async with self._pool.acquire() as connection, connection.transaction():
            release = await connection.fetchrow(
                "SELECT r.manifest_sha256, r.vertex_count, r.edge_count, g.graph_name"
                " FROM crv_kg.release AS r LEFT JOIN crv_kg.release_graph AS g USING (release_id)"
                " WHERE r.release_id = $1",
                release_id,
            )
            if release is None:
                raise InvalidInputError(f"Unknown release {release_id}")
            if release["graph_name"] is not None:
                return str(release["graph_name"])
            graph = f"crv_research_{release['manifest_sha256'][:12]}"
            quoted_graph = _quoted(graph, _GRAPH)
            await _use_age(connection)
            await connection.execute("SELECT ag_catalog.create_graph($1::name)", graph)
            vertex_labels = [
                row["label"] for row in await connection.fetch(
                    "SELECT DISTINCT label FROM crv_kg.vertex_projection WHERE release_id = $1"
                    " ORDER BY label",
                    release_id,
                )
            ]
            edge_labels = [
                row["label"] for row in await connection.fetch(
                    "SELECT DISTINCT label FROM crv_kg.edge_projection WHERE release_id = $1"
                    " ORDER BY label",
                    release_id,
                )
            ]
            for label in vertex_labels:
                _quoted(label, _VERTEX_LABEL)
                await connection.execute(
                    "SELECT ag_catalog.create_vlabel($1::cstring, $2::cstring)", graph, label
                )
            for label in edge_labels:
                _quoted(label, _EDGE_LABEL)
                await connection.execute(
                    "SELECT ag_catalog.create_elabel($1::cstring, $2::cstring)", graph, label
                )
            labels = {
                row["name"]: (row["id"], row["seq_name"])
                for row in await connection.fetch(
                    "SELECT l.name, l.id, l.seq_name FROM ag_catalog.ag_label AS l"
                    " JOIN ag_catalog.ag_graph AS g ON g.graphid = l.graph WHERE g.name = $1",
                    graph,
                )
            }
            await connection.execute(
                "CREATE TEMP TABLE crv_kg_vertex_ids (iri TEXT PRIMARY KEY,"
                " gid ag_catalog.graphid NOT NULL) ON COMMIT DROP"
            )
            for label in vertex_labels:
                label_id, sequence = labels[label]
                table = f"{quoted_graph}.{_quoted(label, _VERTEX_LABEL)}"
                count = await connection.fetchval(
                    "WITH ids AS (INSERT INTO crv_kg_vertex_ids (iri, gid)"
                    " SELECT iri, ag_catalog._graphid($2, row_number() OVER (ORDER BY iri))"
                    " FROM crv_kg.vertex_projection WHERE release_id = $1 AND label = $3"
                    " RETURNING 1) SELECT count(*) FROM ids",
                    release_id, label_id, label,
                )
                await connection.execute(
                    f"INSERT INTO {table} (id, properties)"
                    " SELECT ids.gid, (v.properties::text)::ag_catalog.agtype"
                    " FROM crv_kg.vertex_projection AS v JOIN crv_kg_vertex_ids AS ids USING (iri)"
                    " WHERE v.release_id = $1 AND v.label = $2",
                    release_id, label,
                )
                await _advance(connection, graph, sequence, count)
            for label in edge_labels:
                label_id, sequence = labels[label]
                table = f"{quoted_graph}.{_quoted(label, _EDGE_LABEL)}"
                count = await connection.fetchval(
                    f"WITH rows AS (INSERT INTO {table} (id, start_id, end_id, properties)"
                    " SELECT ag_catalog._graphid($2, row_number() OVER"
                    " (ORDER BY e.start_iri, e.end_iri)), s.gid, t.gid,"
                    " (e.properties::text)::ag_catalog.agtype"
                    " FROM crv_kg.edge_projection AS e"
                    " JOIN crv_kg_vertex_ids AS s ON s.iri = e.start_iri"
                    " JOIN crv_kg_vertex_ids AS t ON t.iri = e.end_iri"
                    " WHERE e.release_id = $1 AND e.label = $3 RETURNING 1)"
                    " SELECT count(*) FROM rows",
                    release_id, label_id, label,
                )
                await _advance(connection, graph, sequence, count)
            for label in vertex_labels:
                table = f"{quoted_graph}.{_quoted(label, _VERTEX_LABEL)}"
                await connection.execute(f"CREATE INDEX ON {table} USING btree (id)")
                await connection.execute(
                    f"CREATE INDEX ON {table} USING btree"
                    " (ag_catalog.agtype_access_operator(VARIADIC ARRAY[properties,"
                    " '\"iri\"'::ag_catalog.agtype]))"
                )
            for label in edge_labels:
                table = f"{quoted_graph}.{_quoted(label, _EDGE_LABEL)}"
                await connection.execute(f"CREATE INDEX ON {table} USING btree (id)")
                await connection.execute(f"CREATE INDEX ON {table} USING btree (start_id)")
                await connection.execute(f"CREATE INDEX ON {table} USING btree (end_id)")
            for label in (*vertex_labels, *edge_labels):
                await connection.execute(f'ANALYZE {quoted_graph}."{label}"')
            vertices = await _count(connection, quoted_graph, "_ag_label_vertex")
            edges = await _count(connection, quoted_graph, "_ag_label_edge")
            if (vertices, edges) != (release["vertex_count"], release["edge_count"]):
                raise IntegrityError(
                    f"AGE load of {release_id}: {vertices}/{edges} rows, manifest says "
                    f"{release['vertex_count']}/{release['edge_count']}"
                )
            await connection.execute(
                "INSERT INTO crv_kg.release_graph (release_id, graph_name, vertex_count,"
                " edge_count, loaded_at) VALUES ($1, $2, $3, $4, $5)",
                release_id, graph, vertices, edges, now,
            )
        return graph

    async def activate(self, release_id: str, *, environment: str, now: datetime) -> None:
        async with self._pool.acquire() as connection:
            await connection.execute(
                "INSERT INTO crv_kg.release_pointer (environment, release_id, activated_at)"
                " VALUES ($1, $2, $3)",
                environment, release_id, now,
            )

    async def cypher(self, graph: str, query: str, *, columns: tuple[str, ...]) -> list[Any]:
        """Run a fixed, operator-written Cypher query (smoke tests); never agent input."""

        quoted = _quoted(graph, _GRAPH).replace('"', "'")
        if "$$" in query:
            raise InvalidInputError("Cypher text must not contain $$")
        shape = ", ".join(f"{_quoted(c, re.compile(r'^[a-z_]+$'))} ag_catalog.agtype"
                          for c in columns)
        async with self._pool.acquire() as connection, connection.transaction():
            await _use_age(connection)
            await _agtype_codec(connection)
            rows = await connection.fetch(
                f"SELECT * FROM ag_catalog.cypher({quoted}, $$ {query} $$) AS ({shape})"
            )
        return [dict(row) for row in rows]


async def _agtype_codec(connection: Any) -> None:
    await connection.set_type_codec(
        "agtype", schema="ag_catalog", encoder=str, decoder=str, format="text"
    )


def _properties(value: Any) -> dict[str, Any]:
    attributes = value if isinstance(value, dict) else json.loads(value)
    return {key: item for key, item in attributes.items() if item not in (None, "", [])}


def _scopes(links: list[Any], key: str, scope_of: dict[str, str]) -> dict[str, tuple[str, ...]]:
    found: dict[str, set[str]] = defaultdict(set)
    for link in links:
        scope = scope_of.get(link["evidence_iri"])
        if scope is not None:
            found[link[key]].add(scope)
    return {owner: tuple(sorted(scopes)) for owner, scopes in found.items()}


def _text_array(values: tuple[str, ...]) -> str:
    """Postgres array literal; every element is quoted, so commas and braces are safe."""

    escaped = (value.replace("\\", "\\\\").replace('"', '\\"') for value in values)
    return "{" + ",".join(f'"{value}"' for value in escaped) + "}"


async def _advance(connection: PgConnection, graph: str, sequence: str, count: int) -> None:
    if count:
        await connection.execute(
            "SELECT setval(format('%I.%I', $1::text, $2::text)::regclass, $3)",
            graph, sequence, count,
        )


async def _count(connection: PgConnection, quoted_graph: str, parent: str) -> int:
    return int(await connection.fetchval(f"SELECT count(*) FROM {quoted_graph}.{parent}"))


async def _check_kinds(connection: PgConnection, entities: list[Any]) -> None:
    recorded = {
        row["iri"]: row["kind"]
        for row in await connection.fetch(
            "SELECT iri, kind FROM crv_kg.entity WHERE iri = ANY ($1::text[])",
            [item.iri for item in entities],
        )
    }
    clash = next((e.iri for e in entities if recorded.get(e.iri, e.kind) != e.kind), None)
    if clash is not None:
        raise IntegrityError(f"Entity {clash} was recorded with a different kind")


async def _check_triples(connection: PgConnection, assertions: list[Any]) -> None:
    recorded = {
        row["iri"]: (row["subject_iri"], row["predicate"], row["object_iri"])
        for row in await connection.fetch(
            "SELECT iri, subject_iri, predicate, object_iri FROM crv_kg.assertion"
            " WHERE iri = ANY ($1::text[])",
            [item.iri for item in assertions],
        )
    }
    clash = next(
        (a.iri for a in assertions
         if recorded.get(a.iri, (a.subject, a.predicate, a.object))
         != (a.subject, a.predicate, a.object)),
        None,
    )
    if clash is not None:
        raise IntegrityError(f"Assertion {clash} was recorded with a different triple")
