"""Collect each arm's evidence and spend from the pipeline stores (runs in the cluster)."""

from __future__ import annotations

from typing import Any

from ...domain.evidence import EvidenceDocument, EvidenceUnit
from ...domain.ids import legacy_placement_id, legacy_source_object_id
from ...ports.evidence import EvidenceStatus
from ...stages.base import StageServices
from .model import DocumentEvidence, Sample, Usage
from .score import page_evidence

_ARM_USAGE = """
SELECT attempt.usage->>'provider' AS provider,
       COALESCE(sum((attempt.usage->>'pages')::bigint), 0) AS pages,
       COALESCE(sum((attempt.usage->>'input_tokens')::bigint), 0) AS input_tokens,
       COALESCE(sum((attempt.usage->>'output_tokens')::bigint), 0) AS output_tokens,
       COALESCE(sum((attempt.usage->>'requests')::bigint), 0) AS requests
FROM ingest.stage_attempt AS attempt
JOIN ingest.stage_job AS job ON job.job_id = attempt.job_id
JOIN ingest.unit AS unit ON unit.unit_id = job.unit_id
WHERE unit.plan_id = ANY($1::text[]) AND attempt.usage IS NOT NULL
GROUP BY 1 ORDER BY 1
"""
_ARM_JOBS = """
SELECT count(*) FILTER (WHERE job.stage = 'cu_submit') AS cu_submits,
       count(*) FILTER (WHERE job.stage = 'cu_submit' AND job.checkpoint ? 'reused') AS reused,
       count(*) FILTER (WHERE job.stage = 'normalize' AND job.checkpoint ? 'escalated_to')
           AS escalated,
       count(*) FILTER (WHERE job.status IN ('retry_wait', 'pending', 'awaiting_remote'))
           AS unfinished
FROM ingest.stage_job AS job
JOIN ingest.unit AS unit ON unit.unit_id = job.unit_id
WHERE unit.plan_id = ANY($1::text[])
"""
# Reused results were paid by another arm; their recorded usage is this arm's effective cost.
_ARM_REUSED = """
SELECT COALESCE(sum((cache.details->'usage'->>'pages')::bigint), 0) AS pages,
       COALESCE(sum((cache.details->'usage'->>'input_tokens')::bigint), 0) AS input_tokens,
       COALESCE(sum((cache.details->'usage'->>'output_tokens')::bigint), 0) AS output_tokens
FROM ingest.stage_job AS job
JOIN ingest.unit AS unit ON unit.unit_id = job.unit_id
JOIN ingest.extraction_cache AS cache ON cache.fingerprint = job.checkpoint->>'reused'
WHERE unit.plan_id = ANY($1::text[]) AND job.stage = 'cu_submit'
"""


async def collect_documents(
    services: StageServices, sample: Sample, *, container_url: str, policy_version: str
) -> dict[str, DocumentEvidence]:
    snapshots = services.require_snapshots()
    repository = services.require_evidence()
    artifacts = services.require_artifacts()
    out: dict[str, DocumentEvidence] = {}
    for document in sample.documents:
        source_object = legacy_source_object_id(container_url, document.blob_name)
        snapshot = await snapshots.latest_for_placement(
            legacy_placement_id(source_object, document.blob_name)
        )
        content_hash = snapshot.content_hash if snapshot is not None else document.sha256
        record = await repository.latest_document(
            content_hash, policy_version, status=EvidenceStatus.EMBEDDED
        ) or await repository.latest_document(
            content_hash, policy_version, status=EvidenceStatus.CHUNKED
        )
        if record is None:
            out[document.id] = DocumentEvidence(doc=document.id, found=False)
            continue
        manifest = EvidenceDocument.model_validate_json(await artifacts.get(record.manifest))
        units = [
            EvidenceUnit.model_validate_json(await artifacts.get(summary.evidence))
            for summary in manifest.units
            if summary.evidence is not None
        ]
        out[document.id] = DocumentEvidence(
            doc=document.id,
            found=True,
            complete=manifest.complete,
            evidence_id=record.evidence_id,
            plan_id=record.plan_id,
            pages=page_evidence(units),
        )
    return out


async def collect_usage(pool: Any, plan_ids: list[str]) -> tuple[list[Usage], dict[str, int]]:
    if not plan_ids:
        return [], {}
    async with pool.acquire() as connection:
        rows = await connection.fetch(_ARM_USAGE, plan_ids)
        jobs = await connection.fetchrow(_ARM_JOBS, plan_ids)
        reused = await connection.fetchrow(_ARM_REUSED, plan_ids)
    usage = [
        Usage(
            provider=row["provider"],
            pages=int(row["pages"]),
            input_tokens=int(row["input_tokens"]),
            output_tokens=int(row["output_tokens"]),
            requests=int(row["requests"]),
        )
        for row in rows
    ]
    if int(reused["pages"]):
        usage.append(
            Usage(
                provider="content_understanding (reused)",
                pages=int(reused["pages"]),
                input_tokens=int(reused["input_tokens"]),
                output_tokens=int(reused["output_tokens"]),
            )
        )
    counts = ("cu_submits", "reused", "escalated", "unfinished")
    return usage, {key: int(jobs[key]) for key in counts}
