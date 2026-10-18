"""Knowledge-graph records: parse a Phase 1 registry run and build a release projection.

Pure functions only. Import keeps every reported record and its provenance; an import can
never approve anything. The release projection is a deterministic, content-addressed view
of current assertions (Docs/knowledge-graph/03-storage-architecture.md §4-§5).
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .errors import InvalidInputError
from .ids import fingerprint, sha256_hex

REGISTRY_SCHEMA_VERSION = "1.0.0"
IMPORT_TOOL = "kg-import-registry"
IMPORT_TOOL_VERSION = "1"
ALIAS_TOOL = "kg-import-aliases"
PROJECTION_VERSION = "kg-projection/2"
REGISTER_SCOPE = "registers"
DEFAULT_RELEASE_STATUSES = ("approved", "reported")

ENTITY_KINDS = frozenset(
    {
        "Site",
        "ProcessGroup",
        "ProcessArea",
        "Equipment",
        "InstrumentTag",
        "EntityCandidate",
        "DocumentReference",
        "Concept",
    }
)
EDGE_LABELS: Mapping[str, str] = {
    "inSite": "IN_SITE",
    "inProcessGroup": "IN_PROCESS_GROUP",
    "reportedInArea": "REPORTED_IN_AREA",
    "tagOf": "TAG_OF",
    "referencesDocument": "REFERENCES_DOCUMENT",
    "measuresMonitorsOrControls": "MEASURES_MONITORS_OR_CONTROLS",
    "actuatesOrFinalElementFor": "ACTUATES_OR_FINAL_ELEMENT_FOR",
}
IMPORT_STATUSES = frozenset({"observed", "reported", "candidate", "rejected", "superseded"})
# Higher wins when several assertions back one edge.
STATUS_RANK: Mapping[str, int] = {
    "rejected": 0,
    "superseded": 0,
    "candidate": 1,
    "observed": 2,
    "reported": 3,
    "approved": 4,
}
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
# Same shape as kg.resolve and the agent's tag finder: 6730ya6381 -> 6730-YA-6381.
_TAG = re.compile(r"([0-9]{4})-?([A-Z]{1,5})-?([0-9]{3,4}[A-Z]?)")
_IDENTIFIER_SCHEMES: Mapping[str, str] = {
    "Site": "site",
    "ProcessGroup": "process_group",
    "ProcessArea": "area_code",
    "Equipment": "equipment_tag",
    "InstrumentTag": "instrument_tag",
    "DocumentReference": "document_file",
}
_TAG_SCHEMES = frozenset({"equipment_tag", "instrument_tag"})


@dataclass(frozen=True)
class Provenance:
    evidence_iri: str
    source_name: str
    source_hash: str
    row_number: int | None
    locator: str | None = None


@dataclass(frozen=True)
class EntityRecord:
    iri: str
    kind: str
    identifier: str
    label: str
    review_status: str
    attributes: Mapping[str, Any]
    provenance: tuple[Provenance, ...]


@dataclass(frozen=True)
class AssertionRecord:
    iri: str
    subject: str
    predicate: str
    object: str
    method: str
    review_status: str
    provenance: tuple[Provenance, ...]


@dataclass(frozen=True)
class Identifier:
    scheme: str
    value_raw: str
    value_normalized: str


@dataclass(frozen=True)
class RegistryImport:
    input_hash: str
    entities: tuple[EntityRecord, ...]
    assertions: tuple[AssertionRecord, ...]

    @property
    def run_id(self) -> str:
        """The same registry bytes imported by the same tool version are the same run."""

        payload = {"input": self.input_hash, "tool": IMPORT_TOOL, "version": IMPORT_TOOL_VERSION}
        return f"urn:tizert:run:{fingerprint('kg-import', payload)}"

    def evidence(self) -> dict[str, Provenance]:
        found: dict[str, Provenance] = {}
        records: tuple[EntityRecord | AssertionRecord, ...] = (*self.entities, *self.assertions)
        for record in records:
            for item in record.provenance:
                found.setdefault(item.evidence_iri, item)
        return found


def normalize_identifier(scheme: str, value: str) -> str:
    """Accent- and case-insensitive key; tags also drop whitespace (``6220 ML 2241``)."""

    folded = unicodedata.normalize("NFKC", value).strip()
    if scheme in _TAG_SCHEMES:
        return re.sub(r"\s+", "", folded).upper()
    decomposed = unicodedata.normalize("NFKD", folded)
    stripped = "".join(char for char in decomposed if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", stripped).casefold()


def canonical_tag(value: str) -> str | None:
    """``6730ya6381`` -> ``6730-YA-6381``; None unless ``value`` is exactly one tag."""

    match = _TAG.fullmatch(value.strip().upper())
    return "-".join(match.groups()) if match else None


def identifiers(entity: EntityRecord) -> tuple[Identifier, ...]:
    """Lookup identifiers an entity reports; legend concepts are keyed by their code prefix."""

    if entity.kind == "Concept":
        raw = str(entity.attributes.get("Code / Prefix") or "").strip()
        scheme = "legend_prefix"
    else:
        raw = entity.identifier.strip()
        scheme = _IDENTIFIER_SCHEMES.get(entity.kind, "")
    if not raw or not scheme:
        return ()
    return (Identifier(scheme, raw, normalize_identifier(scheme, raw)),)


def _provenance(value: Any, where: str) -> tuple[Provenance, ...]:
    if not isinstance(value, list) or not value:
        raise InvalidInputError(f"{where}: provenance must be a non-empty list")
    items: list[Provenance] = []
    for entry in value:
        iri = entry.get("id") if isinstance(entry, dict) else None
        source_hash = entry.get("source_hash") if isinstance(entry, dict) else None
        source_name = entry.get("source_name") if isinstance(entry, dict) else None
        if not (isinstance(iri, str) and iri.startswith("urn:tizert:evidence:")):
            raise InvalidInputError(f"{where}: provenance id must be an evidence URN")
        if not (isinstance(source_hash, str) and _SHA256.match(source_hash)):
            raise InvalidInputError(f"{where}: provenance source_hash must be a SHA-256")
        if not (isinstance(source_name, str) and source_name):
            raise InvalidInputError(f"{where}: provenance source_name is required")
        row = entry.get("row_number")
        if row is not None and (not isinstance(row, int) or isinstance(row, bool) or row < 1):
            raise InvalidInputError(f"{where}: provenance row_number must be a positive integer")
        locator = entry.get("locator")
        items.append(Provenance(iri, source_name, source_hash, row, locator))
    return tuple(items)


def _status(value: Any, where: str) -> str:
    if value == "approved":
        raise InvalidInputError(f"{where}: an import cannot approve; approval needs a decision")
    if value not in IMPORT_STATUSES:
        raise InvalidInputError(f"{where}: unknown review_status {value!r}")
    return str(value)


def parse_registry(data: bytes) -> RegistryImport:
    """Parse and cross-check a registry JSONL run; any defect rejects the whole input."""

    entities: dict[str, EntityRecord] = {}
    assertions: dict[str, AssertionRecord] = {}
    for number, line in enumerate(data.decode("utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        where = f"line {number}"
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            raise InvalidInputError(f"{where}: not JSON ({error.msg})") from error
        if not isinstance(record, dict):
            raise InvalidInputError(f"{where}: a record must be a JSON object")
        if record.get("schema_version") != REGISTRY_SCHEMA_VERSION:
            raise InvalidInputError(f"{where}: schema_version must be {REGISTRY_SCHEMA_VERSION}")
        iri = record.get("id")
        if not isinstance(iri, str) or not iri.startswith("urn:tizert:"):
            raise InvalidInputError(f"{where}: id must be a urn:tizert: IRI")
        if iri in entities or iri in assertions:
            raise InvalidInputError(f"{where}: duplicate id {iri}")
        kind = record.get("kind")
        status = _status(record.get("review_status"), where)
        provenance = _provenance(record.get("provenance"), where)
        if kind == "Assertion":
            predicate = record.get("predicate")
            if predicate not in EDGE_LABELS:
                raise InvalidInputError(f"{where}: unsupported predicate {predicate!r}")
            subject, target, method = (record.get(key) for key in ("subject", "object", "method"))
            if not all(isinstance(value, str) and value for value in (subject, target, method)):
                raise InvalidInputError(f"{where}: assertion needs subject, object and method")
            assertions[iri] = AssertionRecord(
                iri, str(subject), str(predicate), str(target), str(method), status, provenance
            )
            continue
        if kind not in ENTITY_KINDS:
            raise InvalidInputError(f"{where}: unsupported kind {kind!r}")
        label, identifier = record.get("label"), record.get("identifier")
        attributes = record.get("attributes")
        if not isinstance(label, str) or not isinstance(attributes, dict):
            raise InvalidInputError(f"{where}: entity needs a label and attributes")
        entities[iri] = EntityRecord(
            iri,
            str(kind),
            identifier if isinstance(identifier, str) else "",
            label,
            status,
            attributes,
            provenance,
        )
    for assertion in assertions.values():
        for endpoint in (assertion.subject, assertion.object):
            if endpoint not in entities:
                raise InvalidInputError(f"{assertion.iri}: endpoint {endpoint} is not in the run")
    return RegistryImport(sha256_hex(data), tuple(entities.values()), tuple(assertions.values()))


# --- Curated aliases ----------------------------------------------------------------------


@dataclass(frozen=True)
class AliasEntry:
    alias: str
    tag: str
    label: str | None
    note: str | None


@dataclass(frozen=True)
class AliasSet:
    input_hash: str
    entries: tuple[AliasEntry, ...]

    def run_id(self, *, previous: str | None, started_at: datetime) -> str:
        """A new run whenever the current set changes, even back to an earlier file."""

        payload = {
            "input": self.input_hash,
            "tool": ALIAS_TOOL,
            "version": IMPORT_TOOL_VERSION,
            "previous": previous,
            "started_at": started_at.isoformat(),
        }
        return f"urn:tizert:run:{fingerprint('kg-alias-import', payload)}"


def parse_aliases(data: bytes) -> AliasSet:
    """Parse ``aliases.json`` (a list of ``{alias, tag, label?, note?}``); any defect rejects it.

    Targets need not be in the register; the read API reports whether they are.
    """

    try:
        items = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InvalidInputError(f"Aliases are not UTF-8 JSON ({error})") from error
    if not isinstance(items, list):
        raise InvalidInputError("Aliases must be a JSON list")
    entries: dict[tuple[str, str], AliasEntry] = {}
    for number, item in enumerate(items, start=1):
        where = f"alias {number}"
        if not isinstance(item, dict):
            raise InvalidInputError(f"{where}: must be a JSON object")
        alias, tag = item.get("alias"), item.get("tag")
        if not isinstance(alias, str) or not alias.strip():
            raise InvalidInputError(f"{where}: alias is required")
        canonical = canonical_tag(tag) if isinstance(tag, str) else None
        if canonical is None:
            raise InvalidInputError(f"{where}: {tag!r} is not a plant tag")
        label, note = item.get("label"), item.get("note")
        if not all(value is None or isinstance(value, str) for value in (label, note)):
            raise InvalidInputError(f"{where}: label and note must be text")
        key = (alias.strip().casefold(), canonical)
        if key in entries:
            raise InvalidInputError(f"{where}: duplicate alias {alias!r} for {canonical}")
        entries[key] = AliasEntry(alias.strip(), canonical, label, note)
    ordered = tuple(entries[key] for key in sorted(entries))
    return AliasSet(sha256_hex(data), ordered)


# --- Release projection -------------------------------------------------------------------


@dataclass(frozen=True)
class ProjectedEntity:
    """Current state of an entity as read from the system of record."""

    iri: str
    kind: str
    identifier: str
    label: str
    review_status: str
    scope_keys: tuple[str, ...]


@dataclass(frozen=True)
class ProjectedAssertion:
    iri: str
    subject: str
    predicate: str
    object: str
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
    """Assertion IRI to the status it entered the release with."""
    inputs: tuple[str, ...] = ()
    """Runs whose records the release serves (attributes, legend), so any new import that
    reports something yields a new release even when the graph itself is unchanged."""

    @property
    def manifest_sha256(self) -> str:
        return fingerprint(
            PROJECTION_VERSION,
            {
                "statuses": list(self.statuses),
                "inputs": list(self.inputs),
                "vertices": [
                    [v.iri, v.label, dict(v.properties), list(v.scope_keys)] for v in self.vertices
                ],
                "edges": [
                    [e.label, e.start, e.end, dict(e.properties), list(e.scope_keys)]
                    for e in self.edges
                ],
            },
        )

    def release_id(self, built_at: datetime) -> str:
        return f"kg-{built_at:%Y%m%d}-{self.manifest_sha256[:12]}"

    @property
    def graph_name(self) -> str:
        return f"kgr_{self.manifest_sha256[:12]}"


def build_projection(
    entities: Iterable[ProjectedEntity],
    assertions: Iterable[ProjectedAssertion],
    *,
    statuses: Iterable[str] = DEFAULT_RELEASE_STATUSES,
    inputs: Iterable[str] = (),
) -> Projection:
    """Edges for assertions in ``statuses``, one per (label, start, end); vertices for entities
    in ``statuses`` and every edge endpoint. Raw records and text never enter the projection."""

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
        label = EDGE_LABELS[assertion.predicate]
        grouped[(label, assertion.subject, assertion.object)].append(assertion)

    edges: list[Edge] = []
    members: dict[str, str] = {}
    endpoints: set[str] = set()
    for (label, start, end), backing in sorted(grouped.items()):
        status = max((item.status for item in backing), key=STATUS_RANK.__getitem__)
        iris = tuple(sorted(item.iri for item in backing))
        scopes = tuple(sorted({key for item in backing for key in item.scope_keys}))
        if not scopes:
            raise InvalidInputError(f"{iris[0]}: an edge needs at least one evidence scope")
        edges.append(
            Edge(label, start, end, {"status": status, "assertion_count": len(iris)}, scopes, iris)
        )
        members.update((item.iri, item.status) for item in backing)
        endpoints.update((start, end))

    vertices: list[Vertex] = []
    for iri in sorted(by_iri):
        entity = by_iri[iri]
        if entity.review_status not in allowed and iri not in endpoints:
            continue
        if not entity.scope_keys:
            raise InvalidInputError(f"{iri}: a vertex needs at least one evidence scope")
        properties = {
            "iri": iri,
            "identifier": entity.identifier,
            "name": entity.label,
            "status": entity.review_status,
        }
        vertices.append(Vertex(iri, entity.kind, properties, tuple(sorted(entity.scope_keys))))
    return Projection(allowed, tuple(vertices), tuple(edges), members, tuple(sorted(set(inputs))))
