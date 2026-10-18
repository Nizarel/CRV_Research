"""Registry import parsing and the release projection (pure domain, no database)."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from ingest_pipeline.domain.errors import InvalidInputError
from ingest_pipeline.domain.knowledge import (
    ProjectedAssertion,
    ProjectedEntity,
    build_projection,
    canonical_tag,
    identifiers,
    normalize_identifier,
    parse_aliases,
    parse_registry,
)

HASH = "a" * 64


def _registry_path() -> Path:
    configured = os.environ.get("TIZ_TEST_KG_REGISTRY")
    if configured:
        return Path(configured)
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "data/knowledge/runs/2026-10-07/registry.jsonl"
        if candidate.exists():
            return candidate
    return Path("registry-not-found")


REGISTRY = _registry_path()


def _prov(row: int = 2, evidence: str = "e1") -> list[dict[str, Any]]:
    return [
        {
            "id": f"urn:tizert:evidence:{evidence}",
            "row_number": row,
            "source_hash": HASH,
            "source_name": "Equipment_Register.csv",
        }
    ]


def _entity(iri: str, kind: str, identifier: str, **extra: Any) -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "id": iri,
        "kind": kind,
        "identifier": identifier,
        "label": extra.pop("label", identifier),
        "attributes": extra.pop("attributes", {}),
        "review_status": extra.pop("review_status", "reported"),
        "provenance": extra.pop("provenance", _prov()),
    }


def _assertion(iri: str, subject: str, predicate: str, obj: str, **extra: Any) -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "id": iri,
        "kind": "Assertion",
        "subject": subject,
        "predicate": predicate,
        "object": obj,
        "method": "register_import",
        "review_status": extra.pop("review_status", "reported"),
        "provenance": extra.pop("provenance", _prov()),
    }


EQUIPMENT = "urn:tizert:equipment:mill"
AREA = "urn:tizert:processarea:6220"
TAG = "urn:tizert:instrumenttag:tt"


def _lines(*records: dict[str, Any]) -> bytes:
    return "\n".join(json.dumps(record) for record in records).encode()


def _registry() -> bytes:
    return _lines(
        _entity(EQUIPMENT, "Equipment", "6220-ML-2241", label="SAG Mill"),
        _entity(AREA, "ProcessArea", "6220", label="Grinding"),
        _entity(TAG, "InstrumentTag", "6220-TT-2241"),
        _entity(
            "urn:tizert:concept:tt",
            "Concept",
            '{"Code / Prefix": "TT"}',
            label="Temperature transmitter",
            attributes={"Code / Prefix": "TT"},
            review_status="candidate",
        ),
        _assertion("urn:tizert:assertion:a1", EQUIPMENT, "reportedInArea", AREA),
        _assertion("urn:tizert:assertion:a2", TAG, "tagOf", EQUIPMENT),
    )


def test_parse_keeps_records_and_derives_a_stable_run_id() -> None:
    batch = parse_registry(_registry())
    assert [entity.kind for entity in batch.entities] == [
        "Equipment",
        "ProcessArea",
        "InstrumentTag",
        "Concept",
    ]
    assert len(batch.assertions) == 2
    assert batch.run_id == parse_registry(_registry()).run_id
    assert batch.run_id.startswith("urn:tizert:run:")
    assert set(batch.evidence()) == {"urn:tizert:evidence:e1"}


@pytest.mark.parametrize(
    ("record", "message"),
    [
        (_entity("urn:tizert:equipment:x", "Equipment", "x", review_status="approved"), "approve"),
        (_entity("urn:tizert:equipment:x", "Pump", "x"), "unsupported kind"),
        (_entity("urn:tizert:equipment:x", "Equipment", "x", provenance=[]), "provenance"),
        (
            _entity(
                "urn:tizert:equipment:x",
                "Equipment",
                "x",
                provenance=[
                    {"id": "urn:tizert:evidence:e", "source_hash": "bad", "source_name": "f"}
                ],
            ),
            "SHA-256",
        ),
        (_assertion("urn:tizert:assertion:x", EQUIPMENT, "feeds", AREA), "predicate"),
    ],
)
def test_parse_rejects_records_an_import_must_not_accept(
    record: dict[str, Any], message: str
) -> None:
    with pytest.raises(InvalidInputError, match=message):
        parse_registry(
            _lines(_entity(EQUIPMENT, "Equipment", "e"), _entity(AREA, "ProcessArea", "a"), record)
        )


def test_parse_rejects_dangling_endpoints_and_duplicates() -> None:
    dangling = _lines(
        _entity(EQUIPMENT, "Equipment", "e"),
        _assertion("urn:tizert:assertion:a", EQUIPMENT, "reportedInArea", AREA),
    )
    with pytest.raises(InvalidInputError, match="not in the run"):
        parse_registry(dangling)
    with pytest.raises(InvalidInputError, match="duplicate"):
        parse_registry(
            _lines(_entity(EQUIPMENT, "Equipment", "e"), _entity(EQUIPMENT, "Equipment", "e"))
        )


def test_identifiers_normalize_tags_and_legend_prefixes() -> None:
    batch = parse_registry(_registry())
    keys = {entity.kind: identifiers(entity) for entity in batch.entities}
    assert keys["Equipment"][0].scheme == "equipment_tag"
    assert keys["Concept"][0].value_normalized == "tt"
    assert normalize_identifier("equipment_tag", " 6220 ml 2241 ") == "6220ML2241"
    assert normalize_identifier("process_group", "Broyage  Électrique") == "broyage electrique"


def _projected() -> tuple[list[ProjectedEntity], list[ProjectedAssertion]]:
    entities = [
        ProjectedEntity(
            EQUIPMENT, "Equipment", "6220-ML-2241", "SAG Mill", "reported", ("registers",)
        ),
        ProjectedEntity(AREA, "ProcessArea", "6220", "Grinding", "reported", ("registers",)),
        ProjectedEntity(TAG, "InstrumentTag", "6220-TT-2241", "TT", "reported", ("registers",)),
        ProjectedEntity(
            "urn:tizert:concept:tt", "Concept", "TT", "TT", "candidate", ("registers",)
        ),
    ]
    assertions = [
        ProjectedAssertion(
            "urn:tizert:assertion:a1", EQUIPMENT, "reportedInArea", AREA, "reported", ("registers",)
        ),
        ProjectedAssertion(
            "urn:tizert:assertion:a1b", EQUIPMENT, "reportedInArea", AREA, "approved", ("pfd",)
        ),
        ProjectedAssertion(
            "urn:tizert:assertion:a2", TAG, "tagOf", EQUIPMENT, "candidate", ("registers",)
        ),
    ]
    return entities, assertions


def test_projection_groups_assertions_and_excludes_other_statuses() -> None:
    projection = build_projection(*_projected())
    assert [(e.label, e.start, e.end) for e in projection.edges] == [
        ("REPORTED_IN_AREA", EQUIPMENT, AREA)
    ]
    edge = projection.edges[0]
    assert edge.properties == {"status": "approved", "assertion_count": 2}
    assert edge.scope_keys == ("pfd", "registers")
    assert edge.assertion_iris == ("urn:tizert:assertion:a1", "urn:tizert:assertion:a1b")
    # The candidate legend concept and the candidate tag edge stay out; the tag is still a
    # reported entity, so it remains a vertex.
    assert {v.label for v in projection.vertices} == {"Equipment", "ProcessArea", "InstrumentTag"}
    assert all("raw" not in v.properties for v in projection.vertices)
    assert set(projection.members) == {"urn:tizert:assertion:a1", "urn:tizert:assertion:a1b"}


def test_manifest_is_content_addressed_and_names_the_release() -> None:
    first = build_projection(*_projected())
    entities, assertions = _projected()
    second = build_projection(list(reversed(entities)), list(reversed(assertions)))
    assert first.manifest_sha256 == second.manifest_sha256
    assert first.release_id(datetime(2026, 10, 14, tzinfo=UTC)) == (
        f"kg-20261014-{first.manifest_sha256[:12]}"
    )
    assert first.graph_name == f"kgr_{first.manifest_sha256[:12]}"
    widened = build_projection(entities, assertions, statuses=("approved", "candidate", "reported"))
    assert widened.manifest_sha256 != first.manifest_sha256
    run = "urn:tizert:run:" + "1" * 64
    reimported = build_projection(entities, assertions, inputs=[run, run])
    assert reimported.inputs == (run,)
    assert reimported.manifest_sha256 != first.manifest_sha256


def test_projection_refuses_unknown_statuses_and_unscoped_support() -> None:
    entities, assertions = _projected()
    with pytest.raises(InvalidInputError, match="statuses"):
        build_projection(entities, assertions, statuses=("published",))
    unscoped = [
        ProjectedAssertion(
            "urn:tizert:assertion:z", EQUIPMENT, "reportedInArea", AREA, "reported", ()
        )
    ]
    with pytest.raises(InvalidInputError, match="evidence scope"):
        build_projection(entities, unscoped)


@pytest.mark.skipif(not REGISTRY.exists(), reason="Phase 1 registry run not checked out")
def test_the_phase1_registry_parses_and_projects() -> None:
    batch = parse_registry(REGISTRY.read_bytes())
    assert len(batch.entities) == 8661
    assert len(batch.assertions) == 25086
    entities = [
        ProjectedEntity(e.iri, e.kind, e.identifier, e.label, e.review_status, ("registers",))
        for e in batch.entities
    ]
    assertions = [
        ProjectedAssertion(a.iri, a.subject, a.predicate, a.object, a.review_status, ("registers",))
        for a in batch.assertions
    ]
    projection = build_projection(entities, assertions)
    assert len(projection.members) == 25086
    assert {v.label for v in projection.vertices} == {
        "Site",
        "ProcessGroup",
        "ProcessArea",
        "Equipment",
        "InstrumentTag",
        "DocumentReference",
    }


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("6730ya6381", "6730-YA-6381"),
        (" 6730-YS-6333a ", "6730-YS-6333A"),
        ("6720FAH-6245", "6720-FAH-6245"),
        ("6730 YA 6381", None),
        ("X6730-YA-6381", None),
        ("6740", None),
    ],
)
def test_canonical_tag(value: str, expected: str | None) -> None:
    assert canonical_tag(value) == expected


def test_aliases_are_canonical_sorted_and_hash_the_input() -> None:
    data = json.dumps(
        [
            {"alias": " SAG ", "tag": "6210ml2175", "label": "SAG Mill", "note": "PFD"},
            {"alias": "ball mill", "tag": "6220-ML-2241"},
        ]
    ).encode()
    aliases = parse_aliases(data)
    assert [(e.alias, e.tag, e.label) for e in aliases.entries] == [
        ("ball mill", "6220-ML-2241", None),
        ("SAG", "6210-ML-2175", "SAG Mill"),
    ]
    assert aliases.input_hash == parse_aliases(data).input_hash
    started = datetime(2026, 10, 14, tzinfo=UTC)
    first = aliases.run_id(previous=None, started_at=started)
    assert first.startswith("urn:tizert:run:")
    assert first != aliases.run_id(previous="urn:tizert:run:" + "0" * 64, started_at=started)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (b"{}", "JSON list"),
        (b"[1]", "JSON object"),
        (b'[{"alias": " ", "tag": "6210-ML-2175"}]', "alias is required"),
        (b'[{"alias": "SAG", "tag": "SAG"}]', "not a plant tag"),
        (b'[{"alias": "SAG", "tag": "6210-ML-2175", "note": 3}]', "must be text"),
        (
            b'[{"alias": "SAG", "tag": "6210-ML-2175"}, {"alias": "sag", "tag": "6210ML2175"}]',
            "duplicate alias",
        ),
    ],
)
def test_bad_aliases_are_rejected(payload: bytes, message: str) -> None:
    with pytest.raises(InvalidInputError, match=message):
        parse_aliases(payload)
