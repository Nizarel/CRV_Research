"""KG-1 records (entities, assertions, evidence), the build run and the release projection.

A build is one append-only run; re-running on the same inputs is the same run. The release
projection is a deterministic, content-addressed view of current records: raw text never
enters it, only short as-printed attributes (title, genre, date, issuer...).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..errors import InvalidInputError
from ..ids import fingerprint, sha256_hex
from .vocab import EDGE_LABELS, ENTITY_KINDS, PROJECTION_VERSION, STATUS_RANK, TOOL, TOOL_VERSION

LOCATOR_TYPES = frozenset({"file_path", "text_span", "page_header", "gazetteer"})


def evidence_iri(*parts: str) -> str:
    return "urn:crv:evidence:" + sha256_hex("|".join(parts))


def assertion_iri(subject: str, predicate: str, target: str) -> str:
    return "urn:crv:assertion:" + sha256_hex(f"{subject}|{predicate}|{target}")


@dataclass(frozen=True)
class Evidence:
    iri: str
    source_hash: str
    """Content hash of the source document, or the gazetteer file hash."""
    source_name: str
    locator_type: str
    locator: Mapping[str, Any]
    scope_key: str


@dataclass(frozen=True)
class Entity:
    iri: str
    kind: str
    label: str
    status: str
    attributes: Mapping[str, Any]
    identifiers: tuple[tuple[str, str], ...]
    """``(scheme, value)`` pairs: content_hash, report_code, source_path, alias, code_s3..."""
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class Assertion:
    iri: str
    subject: str
    predicate: str
    object: str
    method: str
    status: str
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class KgRun:
    input_hash: str
    entities: tuple[Entity, ...]
    assertions: tuple[Assertion, ...]
    evidence: tuple[Evidence, ...]
    config: Mapping[str, Any] = field(default_factory=dict)

    @property
    def run_id(self) -> str:
        """The same inputs built by the same tool version are the same run."""

        payload = {"input": self.input_hash, "tool": TOOL, "version": TOOL_VERSION}
        return f"urn:crv:run:{fingerprint('crv-kg-build', payload)}"

    def validate(self) -> list[str]:
        """Design §8 rules that hold per run; an empty list means the run may be imported."""

        problems: list[str] = []
        evidence = {item.iri for item in self.evidence}
        entities = {item.iri: item for item in self.entities}
        if len(entities) != len(self.entities):
            problems.append("duplicate entity IRIs")
        for entity in self.entities:
            if entity.kind not in ENTITY_KINDS:
                problems.append(f"{entity.iri}: unknown kind {entity.kind}")
            if not entity.evidence or not set(entity.evidence) <= evidence:
                problems.append(f"{entity.iri}: needs evidence")
        for assertion in self.assertions:
            if assertion.predicate not in EDGE_LABELS:
                problems.append(f"{assertion.iri}: unknown predicate {assertion.predicate}")
            if assertion.subject not in entities or assertion.object not in entities:
                problems.append(f"{assertion.iri}: endpoint missing")
            if not assertion.evidence or not set(assertion.evidence) <= evidence:
                problems.append(f"{assertion.iri}: needs evidence")
        problems.extend(_cycles([a for a in self.assertions if a.predicate == "supersedes"]))
        return problems


def _cycles(edges: list[Assertion]) -> list[str]:
    graph: dict[str, list[str]] = defaultdict(list)
    for edge in edges:
        graph[edge.subject].append(edge.object)
    state: dict[str, int] = {}
    found: list[str] = []

    def visit(node: str) -> None:
        state[node] = 1
        for target in graph.get(node, ()):
            if state.get(target) == 1:
                found.append(f"supersedes cycle through {node}")
            elif target not in state:
                visit(target)
        state[node] = 2

    for node in sorted(graph):
        if node not in state:
            visit(node)
    return found


# --- Release projection -------------------------------------------------------------------


@dataclass(frozen=True)
class ProjectedEntity:
    iri: str
    kind: str
    label: str
    status: str
    properties: Mapping[str, Any]
    scope_keys: tuple[str, ...]


@dataclass(frozen=True)
class ProjectedAssertion:
    iri: str
    subject: str
    predicate: str
    object: str
    method: str
    status: str
    scope_keys: tuple[str, ...]


@dataclass(frozen=True)
class Vertex:
    iri: str
    label: str
    properties: Mapping[str, Any]
    scope_keys: tuple[str, ...]


@dataclass(frozen=True)
class Edge:
    label: str
    start: str
    end: str
    properties: Mapping[str, Any]
    scope_keys: tuple[str, ...]
    assertion_iris: tuple[str, ...]


@dataclass(frozen=True)
class Projection:
    statuses: tuple[str, ...]
    vertices: tuple[Vertex, ...]
    edges: tuple[Edge, ...]
    members: Mapping[str, str] = field(default_factory=dict)
    inputs: tuple[str, ...] = ()
    observations: tuple[tuple[str, str], ...] = ()
    """``(observation IRI, status)`` frozen with the release (KG-2); relational, not projected."""

    @property
    def manifest_sha256(self) -> str:
        payload: dict[str, Any] = {
            "statuses": list(self.statuses),
            "inputs": list(self.inputs),
            "vertices": [
                [v.iri, v.label, dict(v.properties), list(v.scope_keys)] for v in self.vertices
            ],
            "edges": [
                [e.label, e.start, e.end, dict(e.properties), list(e.scope_keys)]
                for e in self.edges
            ],
        }
        if self.observations:  # KG-1-only releases keep their manifests and IDs
            payload["observations"] = [list(item) for item in self.observations]
        return fingerprint(PROJECTION_VERSION, payload)

    def release_id(self, built_at: datetime) -> str:
        return f"crvkg-{built_at:%Y%m%d}-{self.manifest_sha256[:12]}"

    @property
    def graph_name(self) -> str:
        return f"crv_research_{self.manifest_sha256[:12]}"


def build_projection(
    entities: Iterable[ProjectedEntity],
    assertions: Iterable[ProjectedAssertion],
    *,
    statuses: Iterable[str],
    inputs: Iterable[str] = (),
    observations: Iterable[tuple[str, str, str]] = (),
) -> Projection:
    """One edge per (label, start, end) over assertions in ``statuses``; vertices for entities
    in ``statuses`` and every edge endpoint. The best backing status labels the edge.
    ``observations`` are ``(iri, document IRI, status)``: kept in ``statuses`` when their
    document is a vertex of the release."""

    allowed = tuple(sorted(set(statuses)))
    if not allowed or any(status not in STATUS_RANK for status in allowed):
        raise InvalidInputError(f"Release statuses must be drawn from {sorted(STATUS_RANK)}")
    by_iri = {entity.iri: entity for entity in entities}
    grouped: dict[tuple[str, str, str], list[ProjectedAssertion]] = defaultdict(list)
    for assertion in assertions:
        if assertion.status not in allowed:
            continue
        if assertion.subject not in by_iri or assertion.object not in by_iri:
            raise InvalidInputError(f"{assertion.iri}: endpoint missing from the entity set")
        grouped[(EDGE_LABELS[assertion.predicate], assertion.subject, assertion.object)].append(
            assertion
        )
    edges: list[Edge] = []
    members: dict[str, str] = {}
    endpoints: set[str] = set()
    for (label, start, end), backing in sorted(grouped.items()):
        status = max((item.status for item in backing), key=STATUS_RANK.__getitem__)
        iris = tuple(sorted(item.iri for item in backing))
        scopes = tuple(sorted({key for item in backing for key in item.scope_keys}))
        if not scopes:
            raise InvalidInputError(f"{iris[0]}: an edge needs at least one evidence scope")
        methods = sorted({item.method for item in backing})
        edges.append(Edge(label, start, end, {"status": status, "methods": methods}, scopes, iris))
        members.update((item.iri, item.status) for item in backing)
        endpoints.update((start, end))
    vertices: list[Vertex] = []
    for iri in sorted(by_iri):
        entity = by_iri[iri]
        if entity.status not in allowed and iri not in endpoints:
            continue
        if not entity.scope_keys:
            raise InvalidInputError(f"{iri}: a vertex needs at least one evidence scope")
        properties = {**entity.properties, "iri": iri, "name": entity.label,
                      "status": entity.status}
        vertices.append(Vertex(iri, entity.kind, properties, tuple(sorted(entity.scope_keys))))
    present = {vertex.iri for vertex in vertices}
    released = tuple(sorted(
        (iri, status) for iri, document, status in observations
        if status in allowed and document in present
    ))
    return Projection(allowed, tuple(vertices), tuple(edges), members, tuple(sorted(set(inputs))),
                      released)
