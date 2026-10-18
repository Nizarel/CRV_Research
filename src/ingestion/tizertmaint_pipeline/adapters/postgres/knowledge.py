"""Knowledge-graph store on PostgreSQL: register import, release projection, AGE load and the
bounded equipment-context template (AGE traversal plus an equal SQL fallback).

The agent never reaches these tables directly; a later read API wraps the template with the
caller's access scopes. Graph and label names are interpolated into SQL only after matching
fixed patterns; every value travels as a bound parameter.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from ...domain.errors import IntegrityError, InvalidInputError
from ...domain.knowledge import (
    ALIAS_TOOL,
    IMPORT_TOOL,
    IMPORT_TOOL_VERSION,
    PROJECTION_VERSION,
    REGISTER_SCOPE,
    AliasSet,
    ProjectedAssertion,
    ProjectedEntity,
    Projection,
    RegistryImport,
    build_projection,
    identifiers,
)
from .pool import PgConnection, PgPool

_GRAPH = re.compile(r"^kgr_[0-9a-f]{12}$")
_VERTEX_LABEL = re.compile(r"^[A-Z][A-Za-z]+$")
_EDGE_LABEL = re.compile(r"^[A-Z][A-Z_]+$")
CONTEXT_LIMIT = 25


@dataclass(frozen=True)
class ImportReport:
    run_id: str
    run_created: bool
    entities: int
    evidence: int
    assertions: int
    statuses: int


@dataclass(frozen=True)
class AliasReport:
    run_id: str
    created: bool
    aliases: int


@dataclass(frozen=True)
class ReleaseRef:
    release_id: str
    manifest_sha256: str
    graph_name: str
    vertex_count: int
    edge_count: int
    created: bool


@dataclass(frozen=True)
class Neighbor:
    relation: str
    direction: Literal["out", "in"]
    iri: str
    kind: str
    identifier: str
    name: str
    status: str
    assertion_iris: tuple[str, ...]


def _quoted(name: str, pattern: re.Pattern[str]) -> str:
    if not pattern.match(name):
        raise InvalidInputError(f"Refusing identifier {name!r}")
    return f'"{name}"'


async def _use_age(connection: PgConnection) -> None:
    # Azure preloads AGE; never run LOAD 'age' here.
    await connection.execute('SET LOCAL search_path = ag_catalog, "$user", public')


class PostgresKnowledgeStore:
    def __init__(self, pool: PgPool) -> None:
        self._pool = pool

    # --- Import ----------------------------------------------------------------------------

    async def import_registry(
        self, batch: RegistryImport, *, started_at: datetime, now: datetime
    ) -> ImportReport:
        """Record a registry run; re-importing the same bytes adds nothing."""

        run_id = batch.run_id
        evidence = batch.evidence()
        async with self._pool.acquire() as connection, connection.transaction():
            run_created = (
                await connection.fetchval(
                    "INSERT INTO kg.run (run_id, kind, tool, tool_version, input_hash, config,"
                    " started_at, completed_at) VALUES ($1, 'import', $2, $3, $4, $5::jsonb,"
                    " $6, $7) ON CONFLICT (run_id) DO NOTHING RETURNING true",
                    run_id,
                    IMPORT_TOOL,
                    IMPORT_TOOL_VERSION,
                    batch.input_hash,
                    # The pool's jsonb codec encodes objects; a pre-encoded string is stored
                    # as a JSON string.
                    {"scope_key": REGISTER_SCOPE},
                    started_at,
                    now,
                )
                is True
            )
            evidence_rows = sorted(evidence.values(), key=lambda item: item.evidence_iri)
            new_evidence = await connection.fetchval(
                "WITH rows AS (INSERT INTO kg.evidence (iri, source_hash, source_name,"
                " locator_type, locator, scope_key, run_id, recorded_at)"
                " SELECT x.iri, x.source_hash, x.source_name, 'csv_row', x.locator::jsonb, $5, $6,"
                " $7 FROM unnest($1::text[], $2::text[], $3::text[], $4::text[])"
                " AS x(iri, source_hash, source_name, locator)"
                " ON CONFLICT (iri) DO NOTHING RETURNING 1) SELECT count(*) FROM rows",
                [item.evidence_iri for item in evidence_rows],
                [item.source_hash for item in evidence_rows],
                [item.source_name for item in evidence_rows],
                [
                    json.dumps({"row_number": item.row_number, "locator": item.locator})
                    for item in evidence_rows
                ],
                REGISTER_SCOPE,
                run_id,
                now,
            )
            entities = sorted(batch.entities, key=lambda item: item.iri)
            new_entities = await connection.fetchval(
                "WITH rows AS (INSERT INTO kg.entity (iri, kind, created_by_run, created_at)"
                " SELECT x.iri, x.kind, $3, $4 FROM unnest($1::text[], $2::text[]) AS x(iri, kind)"
                " ON CONFLICT (iri) DO NOTHING RETURNING 1) SELECT count(*) FROM rows",
                [item.iri for item in entities],
                [item.kind for item in entities],
                run_id,
                now,
            )
            await self._check_entity_kinds(connection, entities)
            await connection.execute(
                "INSERT INTO kg.entity_record (entity_iri, run_id, review_status, identifier,"
                " label, raw_record, recorded_at)"
                " SELECT x.iri, $6, x.status, x.identifier, x.label, x.raw::jsonb, $7"
                " FROM unnest($1::text[], $2::text[], $3::text[], $4::text[], $5::text[])"
                " AS x(iri, status, identifier, label, raw)"
                " ON CONFLICT (entity_iri, run_id) DO NOTHING",
                [item.iri for item in entities],
                [item.review_status for item in entities],
                [item.identifier for item in entities],
                [item.label for item in entities],
                [json.dumps(dict(item.attributes), ensure_ascii=False) for item in entities],
                run_id,
                now,
            )
            keys = [(item.iri, key) for item in entities for key in identifiers(item)]
            await connection.execute(
                "INSERT INTO kg.identifier (entity_iri, scheme, value_raw, value_normalized,"
                " status, run_id) SELECT x.iri, x.scheme, x.raw, x.normalized, 'reported', $5"
                " FROM unnest($1::text[], $2::text[], $3::text[], $4::text[])"
                " AS x(iri, scheme, raw, normalized) ON CONFLICT DO NOTHING",
                [iri for iri, _ in keys],
                [key.scheme for _, key in keys],
                [key.value_raw for _, key in keys],
                [key.value_normalized for _, key in keys],
                run_id,
            )
            links = sorted({(e.iri, p.evidence_iri) for e in entities for p in e.provenance})
            await connection.execute(
                "INSERT INTO kg.entity_evidence (entity_iri, evidence_iri, run_id)"
                " SELECT x.entity, x.evidence, $3 FROM unnest($1::text[], $2::text[])"
                " AS x(entity, evidence) ON CONFLICT DO NOTHING",
                [entity for entity, _ in links],
                [evidence_iri for _, evidence_iri in links],
                run_id,
            )
            assertions = sorted(batch.assertions, key=lambda item: item.iri)
            new_assertions = await connection.fetchval(
                "WITH rows AS (INSERT INTO kg.assertion (iri, subject_iri, predicate, object_iri,"
                " method, generated_by_run, recorded_at)"
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
            await self._check_assertion_triples(connection, assertions)
            # A decision-backed status is never overwritten by an import.
            current = {
                row["assertion_iri"]: (row["status"], row["decision_id"])
                for row in await connection.fetch(
                    "SELECT DISTINCT ON (assertion_iri) assertion_iri, status, decision_id"
                    " FROM kg.assertion_status WHERE assertion_iri = ANY($1::text[])"
                    " ORDER BY assertion_iri, seq DESC",
                    [item.iri for item in assertions],
                )
            }
            changed = [
                item
                for item in assertions
                if (state := current.get(item.iri)) is None
                or (state[1] is None and state[0] != item.review_status)
            ]
            await connection.execute(
                "INSERT INTO kg.assertion_status (assertion_iri, status, run_id, recorded_at)"
                " SELECT x.iri, x.status, $3, $4"
                " FROM unnest($1::text[], $2::text[]) WITH ORDINALITY AS x(iri, status, n)"
                " ORDER BY x.n",
                [item.iri for item in changed],
                [item.review_status for item in changed],
                run_id,
                now,
            )
            new_statuses = len(changed)
            support = sorted({(a.iri, p.evidence_iri) for a in assertions for p in a.provenance})
            await connection.execute(
                "INSERT INTO kg.assertion_evidence (assertion_iri, evidence_iri, role, run_id)"
                " SELECT x.assertion, x.evidence, 'supports', $3"
                " FROM unnest($1::text[], $2::text[]) AS x(assertion, evidence)"
                " ON CONFLICT DO NOTHING",
                [assertion for assertion, _ in support],
                [evidence_iri for _, evidence_iri in support],
                run_id,
            )
        return ImportReport(
            run_id,
            run_created,
            int(new_entities),
            int(new_evidence),
            int(new_assertions),
            int(new_statuses),
        )

    # Integrity checks read single tables and compare here: joining a large array against a
    # table that was just bulk-loaded can pick a quadratic plan from stale statistics.
    @staticmethod
    async def _check_entity_kinds(connection: PgConnection, entities: list[Any]) -> None:
        recorded = {
            row["iri"]: row["kind"]
            for row in await connection.fetch(
                "SELECT iri, kind FROM kg.entity WHERE iri = ANY($1::text[])",
                [item.iri for item in entities],
            )
        }
        clash = next((e.iri for e in entities if recorded.get(e.iri, e.kind) != e.kind), None)
        if clash is not None:
            raise IntegrityError(f"Entity {clash} was recorded with a different kind")

    @staticmethod
    async def _check_assertion_triples(connection: PgConnection, assertions: list[Any]) -> None:
        recorded = {
            row["iri"]: (row["subject_iri"], row["predicate"], row["object_iri"])
            for row in await connection.fetch(
                "SELECT iri, subject_iri, predicate, object_iri FROM kg.assertion"
                " WHERE iri = ANY($1::text[])",
                [item.iri for item in assertions],
            )
        }
        clash = next(
            (
                a.iri
                for a in assertions
                if recorded.get(a.iri, (a.subject, a.predicate, a.object))
                != (a.subject, a.predicate, a.object)
            ),
            None,
        )
        if clash is not None:
            raise IntegrityError(f"Assertion {clash} was recorded with a different triple")

    async def import_aliases(
        self, aliases: AliasSet, *, started_at: datetime, now: datetime
    ) -> AliasReport:
        """Make ``aliases`` the current alias set; importing the current set again is a no-op."""

        async with self._pool.acquire() as connection, connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext('tizert-kg-alias-import'))"
            )
            current = await connection.fetchrow(
                "SELECT s.run_id, r.input_hash FROM kg.alias_set AS s"
                " JOIN kg.run AS r USING (run_id) ORDER BY s.seq DESC LIMIT 1"
            )
            if current is not None and current["input_hash"] == aliases.input_hash:
                return AliasReport(current["run_id"], created=False, aliases=len(aliases.entries))
            run_id = aliases.run_id(
                previous=None if current is None else current["run_id"], started_at=started_at
            )
            await connection.execute(
                "INSERT INTO kg.run (run_id, kind, tool, tool_version, input_hash, config,"
                " started_at, completed_at) VALUES ($1, 'import', $2, $3, $4, '{}'::jsonb, $5, $6)",
                run_id,
                ALIAS_TOOL,
                IMPORT_TOOL_VERSION,
                aliases.input_hash,
                started_at,
                now,
            )
            await connection.execute(
                "INSERT INTO kg.alias_set (run_id, recorded_at) VALUES ($1, $2)", run_id, now
            )
            await connection.execute(
                "INSERT INTO kg.alias (run_id, alias, target_tag, label, note)"
                " SELECT $1, x.alias, x.tag, x.label, x.note"
                " FROM unnest($2::text[], $3::text[], $4::text[], $5::text[])"
                " AS x(alias, tag, label, note)",
                run_id,
                [entry.alias for entry in aliases.entries],
                [entry.tag for entry in aliases.entries],
                [entry.label for entry in aliases.entries],
                [entry.note for entry in aliases.entries],
            )
        return AliasReport(run_id, created=True, aliases=len(aliases.entries))

    # --- Release ---------------------------------------------------------------------------

    async def current_projection(self, *, statuses: tuple[str, ...]) -> Projection:
        """Project the latest reported entity state and current assertion statuses.

        Single-table reads from one snapshot, joined here: right after a bulk import the table
        statistics are stale and join plans unstable (seen in tests: 0.7 s or over 30 s for the
        same data).
        """

        async with self._pool.acquire() as connection, connection.transaction():
            await connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            active = {
                row["iri"]: row["kind"]
                for row in await connection.fetch(
                    "SELECT iri, kind FROM kg.entity WHERE state = 'active'"
                )
            }
            records = await connection.fetch(
                "SELECT DISTINCT ON (entity_iri) entity_iri, identifier, label, review_status"
                " FROM kg.entity_record ORDER BY entity_iri, recorded_at DESC, run_id DESC"
            )
            inputs = [
                row["run_id"]
                for row in await connection.fetch("SELECT DISTINCT run_id FROM kg.entity_record")
            ]
            scope_of = {
                row["iri"]: row["scope_key"]
                for row in await connection.fetch("SELECT iri, scope_key FROM kg.evidence")
            }
            entity_links = await connection.fetch(
                "SELECT entity_iri, evidence_iri FROM kg.entity_evidence"
            )
            status_of = {
                row["assertion_iri"]: row["status"]
                for row in await connection.fetch(
                    "SELECT assertion_iri, status FROM kg.assertion_current"
                    " WHERE status = ANY($1::text[])",
                    list(statuses),
                )
            }
            assertion_rows = await connection.fetch(
                "SELECT iri, subject_iri, predicate, object_iri FROM kg.assertion"
            )
            support_links = await connection.fetch(
                "SELECT assertion_iri, evidence_iri FROM kg.assertion_evidence"
                " WHERE role = 'supports'"
            )
        entity_scopes = _scopes(entity_links, "entity_iri", scope_of)
        assertion_scopes = _scopes(support_links, "assertion_iri", scope_of)
        entities = [
            ProjectedEntity(
                row["entity_iri"],
                active[row["entity_iri"]],
                row["identifier"],
                row["label"],
                row["review_status"],
                entity_scopes.get(row["entity_iri"], ()),
            )
            for row in records
            if row["entity_iri"] in active
        ]
        assertions = [
            ProjectedAssertion(
                row["iri"],
                row["subject_iri"],
                row["predicate"],
                row["object_iri"],
                status_of[row["iri"]],
                assertion_scopes.get(row["iri"], ()),
            )
            for row in assertion_rows
            if row["iri"] in status_of
            and row["subject_iri"] in active
            and row["object_iri"] in active
        ]
        return build_projection(entities, assertions, statuses=statuses, inputs=inputs)

    async def build_release(
        self, *, statuses: tuple[str, ...], started_at: datetime, now: datetime
    ) -> ReleaseRef:
        """Freeze the current projection; an unchanged projection returns its existing release."""

        projection = await self.current_projection(statuses=statuses)
        manifest = projection.manifest_sha256
        release_id = projection.release_id(now)
        async with self._pool.acquire() as connection, connection.transaction():
            existing = await connection.fetchrow(
                "SELECT release_id, vertex_count, edge_count FROM kg.release"
                " WHERE manifest_sha256 = $1",
                manifest,
            )
            if existing is not None:
                return ReleaseRef(
                    existing["release_id"],
                    manifest,
                    projection.graph_name,
                    existing["vertex_count"],
                    existing["edge_count"],
                    created=False,
                )
            run_id = f"urn:tizert:run:{manifest}"
            await connection.execute(
                "INSERT INTO kg.run (run_id, kind, tool, tool_version, input_hash, config,"
                " started_at, completed_at) VALUES ($1, 'release', 'kg-build-release', $2, $3,"
                " $4::jsonb, $5, $6)",
                run_id,
                PROJECTION_VERSION,
                manifest,
                {"statuses": list(projection.statuses), "inputs": list(projection.inputs)},
                started_at,
                now,
            )
            await connection.execute(
                "INSERT INTO kg.release (release_id, manifest_sha256, projection_version,"
                " statuses, vertex_count, edge_count, built_by_run, created_at)"
                " VALUES ($1, $2, $3, $4::text[], $5, $6, $7, $8)",
                release_id,
                manifest,
                PROJECTION_VERSION,
                list(projection.statuses),
                len(projection.vertices),
                len(projection.edges),
                run_id,
                now,
            )
            members = sorted(projection.members.items())
            await connection.execute(
                "INSERT INTO kg.release_member (release_id, assertion_iri, status)"
                " SELECT $1, x.iri, x.status FROM unnest($2::text[], $3::text[]) AS x(iri, status)",
                release_id,
                [iri for iri, _ in members],
                [status for _, status in members],
            )
            await connection.execute(
                "INSERT INTO kg.vertex_projection (release_id, iri, label, properties, scope_keys)"
                " SELECT $1, x.iri, x.label, x.properties::jsonb, x.scopes::text[]"
                " FROM unnest($2::text[], $3::text[], $4::text[], $5::text[])"
                " AS x(iri, label, properties, scopes)",
                release_id,
                [v.iri for v in projection.vertices],
                [v.label for v in projection.vertices],
                [
                    json.dumps({**v.properties, "release_id": release_id}, ensure_ascii=False)
                    for v in projection.vertices
                ],
                [_text_array(v.scope_keys) for v in projection.vertices],
            )
            await connection.execute(
                "INSERT INTO kg.edge_projection (release_id, label, start_iri, end_iri,"
                " properties, scope_keys, assertion_iris)"
                " SELECT $1, x.label, x.start_iri, x.end_iri, x.properties::jsonb,"
                " x.scopes::text[], x.assertions::text[]"
                " FROM unnest($2::text[], $3::text[], $4::text[], $5::text[], $6::text[],"
                " $7::text[]) AS x(label, start_iri, end_iri, properties, scopes, assertions)",
                release_id,
                [e.label for e in projection.edges],
                [e.start for e in projection.edges],
                [e.end for e in projection.edges],
                [
                    json.dumps(
                        {
                            **e.properties,
                            "release_id": release_id,
                            "assertion_iris": list(e.assertion_iris),
                        }
                    )
                    for e in projection.edges
                ],
                [_text_array(e.scope_keys) for e in projection.edges],
                [_text_array(e.assertion_iris) for e in projection.edges],
            )
        return ReleaseRef(
            release_id,
            manifest,
            projection.graph_name,
            len(projection.vertices),
            len(projection.edges),
            created=True,
        )

    # --- AGE load --------------------------------------------------------------------------

    async def load_age(self, release_id: str, *, now: datetime) -> str:
        """Bulk-load a release into its own AGE graph (set-based, ids assigned up front)."""

        async with self._pool.acquire() as connection, connection.transaction():
            release = await connection.fetchrow(
                "SELECT r.manifest_sha256, r.vertex_count, r.edge_count, g.graph_name"
                " FROM kg.release AS r LEFT JOIN kg.release_graph AS g USING (release_id)"
                " WHERE r.release_id = $1",
                release_id,
            )
            if release is None:
                raise InvalidInputError(f"Unknown release {release_id}")
            if release["graph_name"] is not None:
                return str(release["graph_name"])
            graph = f"kgr_{release['manifest_sha256'][:12]}"
            quoted_graph = _quoted(graph, _GRAPH)
            await _use_age(connection)
            await connection.execute("SELECT ag_catalog.create_graph($1::name)", graph)
            vertex_labels = [
                row["label"]
                for row in await connection.fetch(
                    "SELECT DISTINCT label FROM kg.vertex_projection WHERE release_id = $1"
                    " ORDER BY label",
                    release_id,
                )
            ]
            edge_labels = [
                row["label"]
                for row in await connection.fetch(
                    "SELECT DISTINCT label FROM kg.edge_projection WHERE release_id = $1"
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
                "CREATE TEMP TABLE kg_vertex_ids (iri TEXT PRIMARY KEY,"
                " gid ag_catalog.graphid NOT NULL) ON COMMIT DROP"
            )
            for label in vertex_labels:
                label_id, sequence = labels[label]
                table = f"{quoted_graph}.{_quoted(label, _VERTEX_LABEL)}"
                count = await connection.fetchval(
                    "WITH ids AS (INSERT INTO kg_vertex_ids (iri, gid)"
                    " SELECT iri, ag_catalog._graphid($2, row_number() OVER (ORDER BY iri))"
                    " FROM kg.vertex_projection WHERE release_id = $1 AND label = $3"
                    " RETURNING 1) SELECT count(*) FROM ids",
                    release_id,
                    label_id,
                    label,
                )
                await connection.execute(
                    f"INSERT INTO {table} (id, properties)"
                    " SELECT ids.gid, (v.properties::text)::ag_catalog.agtype"
                    " FROM kg.vertex_projection AS v JOIN kg_vertex_ids AS ids USING (iri)"
                    " WHERE v.release_id = $1 AND v.label = $2",
                    release_id,
                    label,
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
                    " FROM kg.edge_projection AS e"
                    " JOIN kg_vertex_ids AS s ON s.iri = e.start_iri"
                    " JOIN kg_vertex_ids AS t ON t.iri = e.end_iri"
                    " WHERE e.release_id = $1 AND e.label = $3 RETURNING 1)"
                    " SELECT count(*) FROM rows",
                    release_id,
                    label_id,
                    label,
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
                "INSERT INTO kg.release_graph (release_id, graph_name, vertex_count, edge_count,"
                " loaded_at) VALUES ($1, $2, $3, $4, $5)",
                release_id,
                graph,
                vertices,
                edges,
                now,
            )
        return graph

    async def activate(self, release_id: str, *, environment: str, now: datetime) -> None:
        async with self._pool.acquire() as connection:
            await connection.execute(
                "INSERT INTO kg.release_pointer (environment, release_id, activated_at)"
                " VALUES ($1, $2, $3)",
                environment,
                release_id,
                now,
            )

    # --- Bounded template: equipment context -----------------------------------------------

    async def equipment_context(
        self,
        release_id: str,
        iri: str,
        *,
        scopes: tuple[str, ...],
        via: Literal["age", "sql"],
        limit: int = CONTEXT_LIMIT,
    ) -> list[Neighbor]:
        """One-hop neighbourhood of a visible Equipment vertex, at most ``limit`` per relation.

        The start vertex, each edge and each neighbour must share a scope with the caller,
        and filtering happens before the per-relation limit. Invisible equals unknown.
        """

        if not scopes or not 1 <= limit <= CONTEXT_LIMIT:
            return []
        async with self._pool.acquire() as connection, connection.transaction():
            if via == "sql":
                rows = await connection.fetch(_SQL_CONTEXT, release_id, iri, list(scopes), limit)
            else:
                graph = await connection.fetchval(
                    "SELECT graph_name FROM kg.release_graph WHERE release_id = $1", release_id
                )
                if graph is None:
                    raise InvalidInputError(f"Release {release_id} has no AGE graph")
                await _use_age(connection)
                await _agtype_codec(connection)
                rows = await connection.fetch(
                    _age_context(graph),
                    json.dumps({"iri": iri}),
                    release_id,
                    iri,
                    list(scopes),
                    limit,
                )
        return [
            Neighbor(
                row["relation"],
                row["direction"],
                row["iri"],
                row["kind"],
                row["identifier"],
                row["name"],
                row["status"],
                tuple(row["assertion_iris"]),
            )
            for row in rows
        ]


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
            graph,
            sequence,
            count,
        )


async def _count(connection: PgConnection, quoted_graph: str, parent: str) -> int:
    return int(await connection.fetchval(f"SELECT count(*) FROM {quoted_graph}.{parent}"))


async def _agtype_codec(connection: Any) -> None:
    await connection.set_type_codec(
        "agtype", schema="ag_catalog", encoder=str, decoder=str, format="text"
    )


# Authorization and ranking use the relational projection of the same release, so AGE only
# finds candidate neighbours; both paths share the same visibility and limit rules.
def _visible(release: str, iri: str, scopes: str, limit: str) -> str:
    return (
        "visible AS (SELECT e.label AS relation, n.direction, v.iri, v.label AS kind,"
        " v.properties ->> 'identifier' AS identifier, v.properties ->> 'name' AS name,"
        " e.properties ->> 'status' AS status, e.assertion_iris,"
        " row_number() OVER (PARTITION BY e.label, n.direction"
        " ORDER BY v.properties ->> 'identifier', v.iri) AS rank"
        " FROM candidates AS n"
        " JOIN kg.vertex_projection AS start"
        f" ON start.release_id = {release} AND start.iri = {iri} AND start.label = 'Equipment'"
        f" AND start.scope_keys && {scopes}::text[]"
        " JOIN kg.edge_projection AS e"
        f" ON e.release_id = {release} AND e.label = n.relation"
        f" AND e.start_iri = CASE n.direction WHEN 'out' THEN {iri} ELSE n.neighbor END"
        f" AND e.end_iri = CASE n.direction WHEN 'out' THEN n.neighbor ELSE {iri} END"
        f" AND e.scope_keys && {scopes}::text[]"
        " JOIN kg.vertex_projection AS v"
        f" ON v.release_id = {release} AND v.iri = n.neighbor AND v.scope_keys && {scopes}::text[])"
        " SELECT relation, direction, iri, kind, identifier, name, status, assertion_iris"
        f" FROM visible WHERE rank <= {limit} ORDER BY relation, direction, rank"
    )


_SQL_CONTEXT = (
    "WITH candidates AS ("
    " SELECT label AS relation, 'out' AS direction, end_iri AS neighbor"
    " FROM kg.edge_projection WHERE release_id = $1 AND start_iri = $2"
    " UNION ALL SELECT label, 'in', start_iri FROM kg.edge_projection"
    " WHERE release_id = $1 AND end_iri = $2), " + _visible("$1", "$2", "$3", "$4")
)


def _age_context(graph: str) -> str:
    name = _quoted(graph, _GRAPH).replace('"', "'")

    # Two directed matches: AGE plans an undirected (e)-[r]-(n) as a filter over every
    # edge-vertex pair, while a direction lets it use the start_id/end_id indexes.
    def match(pattern: str, direction: str) -> str:
        return (
            f"SELECT * FROM ag_catalog.cypher({name}, $$ MATCH {pattern} WHERE e.iri = $iri"
            f" RETURN type(r), '{direction}', n.iri $$, $1) AS (relation ag_catalog.agtype,"
            " direction ag_catalog.agtype, neighbor ag_catalog.agtype)"
        )

    return (
        "WITH found AS ("
        + match("(e:Equipment)-[r]->(n)", "out")
        + " UNION ALL "
        + match("(e:Equipment)<-[r]-(n)", "in")
        + "),"
        # agtype -> text yields a string scalar's bare value in AGE 1.6.
        " candidates AS (SELECT DISTINCT relation::text AS relation,"
        " direction::text AS direction, neighbor::text AS neighbor FROM found), "
        + _visible("$2", "$3", "$4", "$5")
    )
