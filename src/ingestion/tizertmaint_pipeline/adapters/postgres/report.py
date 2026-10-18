"""Read-only operational summary of the ledger, evidence and projections (no content)."""

from __future__ import annotations

from typing import Any

from .pool import PgPool

_JOBS = """
SELECT stage, status, count(*) AS jobs FROM ingest.stage_job GROUP BY stage, status ORDER BY 1, 2
"""
_UNITS = """
SELECT route, COALESCE(disposition, 'open') AS disposition, count(*) AS units,
       COALESCE(sum(cardinality(pages)), 0) AS pages
FROM ingest.unit GROUP BY 1, 2 ORDER BY 1, 2
"""
_USAGE = """
SELECT usage->>'provider' AS provider,
       COALESCE(usage->>'account', 'this subscription') AS account,
       COALESCE(sum((usage->>'pages')::bigint), 0) AS pages,
       COALESCE(sum((usage->>'input_tokens')::bigint), 0) AS input_tokens,
       COALESCE(sum((usage->>'requests')::bigint), 0) AS requests,
       round(COALESCE(sum((usage->>'cost_usd')::numeric), 0), 2) AS cost_usd
FROM ingest.stage_attempt WHERE usage IS NOT NULL GROUP BY 1, 2 ORDER BY 1, 2
"""
_EVIDENCE = """
SELECT status, completeness, count(*) AS documents, COALESCE(sum(chunk_count), 0) AS chunks
FROM ingest.evidence_document GROUP BY 1, 2 ORDER BY 1, 2
"""
_PROJECTIONS = """
SELECT target, state, count(*) AS rows FROM ingest.projection_item GROUP BY 1, 2 ORDER BY 1, 2
"""
_FIGURES = "SELECT state, count(*) AS figures FROM ingest.figure_asset GROUP BY 1 ORDER BY 1"
_PROBLEMS = """
SELECT job_id, stage, status, last_error_category, left(last_error, 300) AS last_error, updated_at
FROM ingest.stage_job
WHERE status IN ('failed_permanent', 'needs_review', 'submission_unknown', 'quarantined',
                 'unsupported', 'retry_wait')
ORDER BY updated_at DESC LIMIT $1
"""


async def pipeline_status(pool: PgPool, *, failures: int) -> dict[str, Any]:
    async with pool.acquire() as connection:
        sections = {
            "jobs": await connection.fetch(_JOBS),
            "units": await connection.fetch(_UNITS),
            "provider_usage": await connection.fetch(_USAGE),
            "evidence": await connection.fetch(_EVIDENCE),
            "projections": await connection.fetch(_PROJECTIONS),
            "figures": await connection.fetch(_FIGURES),
            "problems": await connection.fetch(_PROBLEMS, failures),
        }
    return {name: [dict(row) for row in rows] for name, rows in sections.items()}
