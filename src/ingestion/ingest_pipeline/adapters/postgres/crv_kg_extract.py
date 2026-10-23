"""KG-2 extraction records into ``crv_kg`` (candidates only; never touches KG-1 records).

One run per extraction input: re-importing the same records is a no-op. Re-extracting a document
supersedes that document's previous extraction records (studies, samples, ``llm``/``table``
assertions and observations) that the new run no longer reports, unless an expert decided them.
Records of other documents and every KG-1 record are left untouched.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from ...domain.crv_kg.records import EXTRACT_TOOL, EXTRACT_VERSION, ExtractRecords
from ...domain.errors import InvalidInputError
from ...domain.ids import fingerprint
from .pool import PgPool

_LATEST_RECORDS = (
    "SELECT DISTINCT ON (entity_iri) entity_iri, label, attributes, review_status"
    " FROM crv_kg.entity_record ORDER BY entity_iri, recorded_at DESC, run_id DESC"
)


@dataclass(frozen=True)
class ExtractImport:
    run_id: str
    run_created: bool
    entities: int
    assertions: int
    observations: int
    superseded: int


def extract_run_id(documents: Sequence[str], records: ExtractRecords) -> str:
    payload = {
        "tool": EXTRACT_TOOL, "version": EXTRACT_VERSION, "documents": sorted(documents),
        "entities": sorted(e.iri for e in records.entities),
        "assertions": sorted(a.iri for a in records.assertions),
        "observations": sorted(o.iri for o in records.observations),
    }
    return f"urn:crv:run:{fingerprint('crv-kg-extract', payload)}"


def _problems(documents: Sequence[str], records: ExtractRecords) -> list[str]:
    evidence = {item.iri for item in records.evidence}
    problems = [f"{e.iri}: not a candidate study/sample" for e in records.entities
                if e.kind not in {"Study", "Sample"} or e.status != "candidate"]
    problems += [f"{a.iri}: not a candidate extraction link" for a in records.assertions
                 if a.method not in {"llm", "table"} or a.status != "candidate"]
    problems += [f"{o.iri}: unknown evidence" for o in records.observations
                 if o.evidence_iri not in evidence]
    problems += [f"{o.iri}: outside the documents" for o in records.observations
                 if o.document_iri not in documents]
    return problems


async def import_extraction(
    pool: PgPool, documents: Sequence[str], records: ExtractRecords, *, started_at: datetime,
    now: datetime,
) -> ExtractImport:
    """Record ``records`` for ``documents`` (document IRIs, which KG-1 must already hold)."""

    problems = _problems(documents, records)
    if problems:
        raise InvalidInputError(f"Refusing invalid extraction records: {problems[:5]}")
    run_id = extract_run_id(documents, records)
    async with pool.acquire() as connection, connection.transaction():
        await connection.execute("SELECT pg_advisory_xact_lock(hashtext('crv-kg-import'))")
        created = await connection.fetchval(
            "INSERT INTO crv_kg.run (run_id, kind, tool, tool_version, input_hash, config,"
            " started_at, completed_at) VALUES ($1, 'build', $2, $3, $4, $5::jsonb, $6, $7)"
            " ON CONFLICT (run_id) DO NOTHING RETURNING true",
            run_id, EXTRACT_TOOL, EXTRACT_VERSION, run_id.rsplit(":", 1)[-1],
            json.dumps({"documents": len(documents)}), started_at, now,
        ) is True
        if not created:
            return ExtractImport(run_id, False, 0, 0, 0, 0)
        evidence = records.evidence
        await connection.execute(
            "INSERT INTO crv_kg.evidence (iri, source_hash, source_name, locator_type, locator,"
            " scope_key, run_id, recorded_at) SELECT x.iri, x.hash, x.name, x.type,"
            " x.locator::jsonb, x.scope, $7, $8 FROM unnest($1::text[], $2::text[], $3::text[],"
            " $4::text[], $5::text[], $6::text[]) AS x(iri, hash, name, type, locator, scope)"
            " ON CONFLICT (iri) DO NOTHING",
            [e.iri for e in evidence], [e.source_hash for e in evidence],
            [e.source_name for e in evidence], [e.locator_type for e in evidence],
            [json.dumps(dict(e.locator), ensure_ascii=False) for e in evidence],
            [e.scope_key for e in evidence], run_id, now,
        )
        entities = records.entities
        await connection.execute(
            "INSERT INTO crv_kg.entity (iri, kind, created_by_run, created_at)"
            " SELECT x.iri, x.kind, $3, $4 FROM unnest($1::text[], $2::text[]) AS x(iri, kind)"
            " ON CONFLICT (iri) DO NOTHING",
            [e.iri for e in entities], [e.kind for e in entities], run_id, now,
        )
        await connection.execute(
            "INSERT INTO crv_kg.entity_record (entity_iri, run_id, review_status, label,"
            " attributes, recorded_at) SELECT x.iri, $4, 'candidate', x.label, x.attributes::jsonb,"
            " $5 FROM unnest($1::text[], $2::text[], $3::text[]) AS x(iri, label, attributes)"
            f" LEFT JOIN ({_LATEST_RECORDS}) AS latest ON latest.entity_iri = x.iri"
            " WHERE latest.review_status IS NULL"
            " OR latest.review_status NOT IN ('approved', 'rejected')",
            [e.iri for e in entities], [e.label for e in entities],
            [json.dumps(dict(e.attributes), ensure_ascii=False, sort_keys=True) for e in entities],
            run_id, now,
        )
        links = sorted({(e.iri, proof) for e in entities for proof in e.evidence})
        await connection.execute(
            "INSERT INTO crv_kg.entity_evidence (entity_iri, evidence_iri, run_id)"
            " SELECT x.entity, x.evidence, $3 FROM unnest($1::text[], $2::text[])"
            " AS x(entity, evidence) ON CONFLICT DO NOTHING",
            [entity for entity, _ in links], [proof for _, proof in links], run_id,
        )
        assertions = records.assertions
        await connection.execute(
            "INSERT INTO crv_kg.assertion (iri, subject_iri, predicate, object_iri, method,"
            " generated_by_run, recorded_at) SELECT x.iri, x.subject, x.predicate, x.object,"
            " x.method, $6, $7 FROM unnest($1::text[], $2::text[], $3::text[], $4::text[],"
            " $5::text[]) AS x(iri, subject, predicate, object, method)"
            " ON CONFLICT (iri) DO NOTHING",
            [a.iri for a in assertions], [a.subject for a in assertions],
            [a.predicate for a in assertions], [a.object for a in assertions],
            [a.method for a in assertions], run_id, now,
        )
        support = sorted({(a.iri, proof) for a in assertions for proof in a.evidence})
        await connection.execute(
            "INSERT INTO crv_kg.assertion_evidence (assertion_iri, evidence_iri, run_id)"
            " SELECT x.assertion, x.evidence, $3 FROM unnest($1::text[], $2::text[])"
            " AS x(assertion, evidence) ON CONFLICT DO NOTHING",
            [assertion for assertion, _ in support], [proof for _, proof in support], run_id,
        )
        observations = records.observations
        await connection.execute(
            "INSERT INTO crv_kg.observation (iri, document_iri, study_iri, sample_iri,"
            " subject_text, property, element, stream, basis, value_text, value_num, unit,"
            " qualifier, method, evidence_iri, generated_by_run, recorded_at)"
            " SELECT x.iri, x.document, x.study, x.sample, x.subject, x.property, x.element,"
            " x.stream, x.basis, x.value_text, x.value_num, x.unit, x.qualifier, x.method,"
            " x.evidence, $16, $17 FROM unnest($1::text[], $2::text[], $3::text[], $4::text[],"
            " $5::text[], $6::text[], $7::text[], $8::text[], $9::text[], $10::text[],"
            " $11::numeric[], $12::text[], $13::text[], $14::text[], $15::text[])"
            " AS x(iri, document, study, sample, subject, property, element, stream, basis,"
            " value_text, value_num, unit, qualifier, method, evidence)"
            " ON CONFLICT (iri) DO NOTHING",
            [o.iri for o in observations], [o.document_iri for o in observations],
            [o.study_iri for o in observations], [o.sample_iri for o in observations],
            [o.subject_text for o in observations], [o.property for o in observations],
            [o.element for o in observations], [o.stream for o in observations],
            [o.basis for o in observations], [o.value_text for o in observations],
            [o.value_num for o in observations], [o.unit for o in observations],
            [o.qualifier for o in observations], [o.method for o in observations],
            [o.evidence_iri for o in observations], run_id, now,
        )
        # Scope: previous extraction records of these documents only (IRIs carry the hash).
        prefixes = [
            f"urn:crv:{kind}:{document.rsplit(':', 1)[-1]}%"
            for document in documents for kind in ("study", "sample")
        ]
        stale = await connection.fetch(
            f"SELECT latest.entity_iri FROM ({_LATEST_RECORDS}) AS latest"
            " WHERE latest.entity_iri LIKE ANY ($1::text[])"
            " AND latest.review_status IN ('candidate', 'observed')"
            " AND NOT (latest.entity_iri = ANY ($2::text[]))",
            prefixes, [e.iri for e in entities],
        )
        await connection.execute(
            "INSERT INTO crv_kg.entity_record (entity_iri, run_id, review_status, label,"
            " attributes, recorded_at) SELECT DISTINCT ON (record.entity_iri)"
            " record.entity_iri, $2, 'superseded', record.label, record.attributes, $3"
            " FROM crv_kg.entity_record AS record WHERE record.entity_iri = ANY ($1::text[])"
            " ORDER BY record.entity_iri, record.recorded_at DESC",
            [row["entity_iri"] for row in stale], run_id, now,
        )
        reported = {a.iri for a in assertions}
        current = await connection.fetch(
            "SELECT link.iri, state.status, state.decision_id FROM crv_kg.assertion AS link"
            " LEFT JOIN crv_kg.assertion_current AS state ON state.assertion_iri = link.iri"
            " WHERE link.method IN ('llm', 'table') AND (link.iri = ANY ($1::text[])"
            " OR link.subject_iri = ANY ($2::text[]) OR link.subject_iri LIKE ANY ($3::text[]))",
            sorted(reported), list(documents), prefixes,
        )
        rows = [
            (row["iri"], "candidate" if row["iri"] in reported else "superseded")
            for row in current if row["decision_id"] is None
        ]
        state = {row["iri"]: row["status"] for row in current}
        rows = [(iri, status) for iri, status in rows if state.get(iri) != status]
        await connection.execute(
            "INSERT INTO crv_kg.assertion_status (assertion_iri, status, run_id, recorded_at)"
            " SELECT x.iri, x.status, $3, $4 FROM unnest($1::text[], $2::text[]) AS x(iri, status)",
            [iri for iri, _ in rows], [status for _, status in rows], run_id, now,
        )
        current_observations = await connection.fetch(
            "SELECT item.iri, state.status, state.decision_id FROM crv_kg.observation AS item"
            " LEFT JOIN crv_kg.observation_current AS state ON state.observation_iri = item.iri"
            " WHERE item.document_iri = ANY ($1::text[])",
            list(documents),
        )
        kept = {o.iri for o in observations}
        observation_rows = [
            (row["iri"], status) for row in current_observations
            if row["decision_id"] is None
            and row["status"] != (status := "candidate" if row["iri"] in kept else "superseded")
        ]
        await connection.execute(
            "INSERT INTO crv_kg.observation_status (observation_iri, status, run_id, recorded_at)"
            " SELECT x.iri, x.status, $3, $4 FROM unnest($1::text[], $2::text[]) AS x(iri, status)",
            [iri for iri, _ in observation_rows], [status for _, status in observation_rows],
            run_id, now,
        )
        superseded = len(stale) + sum(
            1 for _, status in [*rows, *observation_rows] if status == "superseded"
        )
    return ExtractImport(run_id, True, len(entities), len(assertions), len(observations),
                         superseded)
