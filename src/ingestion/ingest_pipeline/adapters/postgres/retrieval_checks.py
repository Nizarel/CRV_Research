"""Read-only checks of a ``crv_retrieval`` release copy: server preflight, row parity with the
Search projection, approximate-versus-exact vector recall, query plans and search latency.

The approximate query is the unscoped vector leg (te3 or m3) of ``crv_retrieval.search``
(migration 0022), run under that function's own planner settings (``pg_proc.proconfig``); the
exact query is a full-precision scan. Nothing here writes.
"""

from __future__ import annotations

import json
import re
import statistics
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ...domain.errors import InvalidInputError
from .pool import PgConnection
from .retrieval import M3_DIMENSIONS, STORE, TARGET, TE3_DIMENSIONS, VECTOR_INDEXES

CHECKS = ("preflight", "parity", "recall", "explain", "latency")
MODELS = ("te3", "m3")
LATENCY_BUDGET_SECONDS = 900.0
"""Both latency passes stop starting new queries after this long (one query can take longer)."""
MIN_VECTOR_VERSION = (0, 8, 0)
"""Iterative index scans (``hnsw.iterative_scan``) need pgvector 0.8.0."""
CANDIDATES = 100
"""The vector leg reads 2 x depth HNSW candidates before the full-precision re-rank."""
DISTANCE_TOLERANCE = 1e-6
HNSW_INDEX = VECTOR_INDEXES["te3"][0]
SEARCH_FUNCTION = "crv_retrieval.search(text, text, text[], text, real[], integer)"
_PLANNER_SETTING = re.compile(r"^(hnsw\.[a-z_]+|enable_[a-z_]+)=([A-Za-z0-9_.]+)$")


@dataclass(frozen=True)
class VectorSql:
    """One model's vector column: the queries mirror its leg of ``crv_retrieval.search``."""

    model: str
    dimensions: int
    index: str
    approximate: str
    exact: str
    scoped: str
    sample: str


def vector_sql(model: str) -> VectorSql:
    if model not in MODELS:
        raise InvalidInputError(f"Unknown embedding model {model!r}")
    index, _, column = VECTOR_INDEXES[model]
    dimensions = TE3_DIMENSIONS if model == "te3" else M3_DIMENSIONS
    vector = f"$2::real[]::vector({dimensions})"
    # te3 is indexed as halfvec (3,072 dimensions exceed the vector index limit); m3 as is.
    order = (
        f"c.{column}::halfvec({dimensions}) <=> {vector}::halfvec({dimensions})"
        if model == "te3" else f"c.{column} <=> {vector}"
    )
    return VectorSql(
        model=model, dimensions=dimensions, index=index,
        approximate=f"""
WITH candidate AS MATERIALIZED (
    SELECT c.projection_id, c.{column} FROM crv_retrieval.chunk AS c
    WHERE c.release_id = $1
    ORDER BY {order}
    LIMIT $3)
SELECT candidate.projection_id, candidate.{column} <=> {vector} AS distance
FROM candidate WHERE candidate.{column} IS NOT NULL
ORDER BY distance, candidate.projection_id
LIMIT $4
""",
        exact=f"""
SELECT c.projection_id, c.{column} <=> {vector} AS distance
FROM crv_retrieval.chunk AS c
WHERE c.release_id = $1 AND c.{column} IS NOT NULL
ORDER BY distance, c.projection_id
LIMIT $3
""",
        scoped=f"""
WITH candidate AS MATERIALIZED (
    SELECT c.projection_id, c.{column} FROM crv_retrieval.chunk AS c
    WHERE c.release_id = $1 AND c.content_hash = ANY ($3::text[])
        AND c.{column} IS NOT NULL)
SELECT candidate.projection_id FROM candidate
ORDER BY candidate.{column} <=> {vector}, candidate.projection_id
LIMIT $4
""",
        # Sample keys first: computing the vector cast inside the ORDER BY ... LIMIT would read
        # them all.
        sample=f"""
WITH picked AS (
    SELECT c.projection_id FROM crv_retrieval.chunk AS c
    WHERE c.release_id = $1 AND c.{column} IS NOT NULL
    ORDER BY md5(c.projection_id || $2), c.projection_id
    LIMIT $3)
SELECT c.projection_id, c.content_hash, c.title, c.{column}::real[] AS vector
FROM picked JOIN crv_retrieval.chunk AS c USING (projection_id)
ORDER BY md5(c.projection_id || $2), c.projection_id
""",
    )


_TE3 = vector_sql("te3")
_APPROXIMATE = _TE3.approximate
_EXACT = _TE3.exact
_SCOPED = _TE3.scoped
# The first stage of the keyword leg (migrations 0026, 0027).
_LEXICAL = """
SELECT t.projection_id FROM crv_retrieval.chunk_terms AS t
WHERE t.release_id = $1 AND t.search_text @@ crv_retrieval.match_terms($2)
ORDER BY ts_rank('{1, 1, 0.5, 1}', t.search_text, crv_retrieval.query_terms($2), 1) DESC,
    t.projection_id
LIMIT 600
"""
_SEARCH = "SELECT * FROM crv_retrieval.search($1, $2, $3::text[], $4, $5::real[], $6)"
_SERVER = """
SELECT (SELECT extversion FROM pg_extension WHERE extname = 'vector') AS installed,
    (SELECT default_version FROM pg_available_extensions WHERE name = 'vector') AS available,
    to_regclass('crv_retrieval.chunk') IS NOT NULL AS has_schema,
    pg_database_size(current_database()) AS database_bytes,
    current_setting('shared_buffers') AS shared_buffers,
    current_setting('work_mem') AS work_mem,
    current_setting('maintenance_work_mem') AS maintenance_work_mem,
    current_setting('max_parallel_maintenance_workers') AS max_parallel_maintenance_workers,
    current_setting('max_connections') AS max_connections,
    has_table_privilege('ingest.schema_migration', 'SELECT') AS can_read_migrations
"""
_SIZES = """
SELECT pg_table_size('crv_retrieval.chunk') AS table_bytes,
    pg_indexes_size('crv_retrieval.chunk') AS index_bytes,
    coalesce(pg_relation_size(to_regclass('crv_retrieval.' || $1)), 0) AS hnsw_bytes
"""
_SOURCE = """
SELECT count(*) AS rows, count(DISTINCT content_hash) AS contents
FROM ingest.projection_item
WHERE store = $1 AND target = $2 AND release_id = $3 AND state = 'verified'
"""
_PARITY = """
SELECT count(*) AS source_rows,
    count(*) FILTER (WHERE c.projection_id IS NULL) AS missing_rows,
    count(*) FILTER (WHERE c.payload_sha256 <> s.payload_sha256) AS digest_mismatches,
    count(*) FILTER (WHERE c.projection_id IS NOT NULL AND c.embedding_te3 IS NULL)
        AS missing_vectors,
    count(*) FILTER (WHERE c.release_id <> s.release_id) AS wrong_release
FROM ingest.projection_item AS s
LEFT JOIN crv_retrieval.chunk AS c ON c.projection_id = s.projection_id
WHERE s.store = $1 AND s.target = $2 AND s.release_id = $3 AND s.state = 'verified'
"""
_EXTRA = """
SELECT count(*) FROM crv_retrieval.chunk AS c
WHERE c.release_id = $3 AND NOT EXISTS (
    SELECT 1 FROM ingest.projection_item AS s
    WHERE s.store = $1 AND s.target = $2 AND s.projection_id = c.projection_id
        AND s.state = 'verified')
"""
_COPIED = """
SELECT count(*) FROM ingest.projection_item
WHERE store = $1 AND target = $2 AND release_id = $3 AND state = 'verified'
"""


@dataclass(frozen=True)
class CheckOptions:
    release_id: str
    source_store: str
    source_target: str
    sample: int = 30
    k: int = 50
    ef_search: int | None = None
    """Overrides the function's ``hnsw.ef_search`` (tuning runs only)."""
    min_recall: float = 0.95
    max_p95_ms: float = 1000.0
    """Latency gate: p95 of the first hybrid search of each query."""
    seed: str = "crv-retrieval-verify"
    model: str = "te3"
    """Vector column checked by recall, explain and latency."""

    def __post_init__(self) -> None:
        if self.model not in MODELS:
            raise InvalidInputError(f"model must be one of {', '.join(MODELS)}")
        if not (0 <= self.sample <= 500 and 1 <= self.k <= CANDIDATES // 2):
            raise InvalidInputError("sample must be 0-500 and k 1-50")
        ef_ok = self.ef_search is None or 10 <= self.ef_search <= 1000
        if not (ef_ok and 0 < self.min_recall <= 1 and self.max_p95_ms > 0):
            raise InvalidInputError(
                "ef_search must be 10-1000, min_recall in (0, 1] and max_p95_ms positive"
            )


def version(text: str | None) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", text or "")[:3])


def distance_recall(
    approximate: Sequence[tuple[str, float]], exact: Sequence[tuple[str, float]], k: int
) -> tuple[float, float]:
    """Key recall, and distance recall where an approximate hit no farther than the k-th exact
    neighbour counts: duplicate placements share a vector, so tied keys are interchangeable."""

    truth = list(exact[:k])
    if not truth:
        return 1.0, 1.0
    keys = {key for key, _ in truth}
    farthest = truth[-1][1] + DISTANCE_TOLERANCE
    found = list(approximate[:k])
    by_key = sum(key in keys for key, _ in found) / len(truth)
    by_distance = min(len(truth), sum(distance <= farthest for _, distance in found)) / len(truth)
    return by_key, by_distance


def _nodes(plan: Mapping[str, Any]) -> Iterator[Mapping[str, Any]]:
    yield plan
    for child in plan.get("Plans", ()):
        yield from _nodes(child)


def plan_summary(explained: Any) -> dict[str, Any]:
    """Node types, indexes and buffer counts of an ``EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)``."""

    document = json.loads(explained) if isinstance(explained, str) else explained
    top = document[0]
    plan = top["Plan"]
    nodes = list(_nodes(plan))
    return {
        "nodes": sorted({str(node["Node Type"]) for node in nodes}),
        "indexes": sorted({str(node["Index Name"]) for node in nodes if "Index Name" in node}),
        "chunk_seq_scan": any(
            node["Node Type"] == "Seq Scan"
            and node.get("Relation Name") in ("chunk", "chunk_terms")
            for node in nodes
        ),
        "execution_ms": top.get("Execution Time"),
        "shared_hit_blocks": plan.get("Shared Hit Blocks"),
        "shared_read_blocks": plan.get("Shared Read Blocks"),
    }


def _percentile(values: Sequence[float], share: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(share * len(ordered)))], 1)


async def _preflight(connection: PgConnection, options: CheckOptions) -> dict[str, Any]:
    server = dict(await connection.fetchrow(_SERVER))
    report: dict[str, Any] = {
        key: value for key, value in server.items() if key != "can_read_migrations"
    }
    if server["can_read_migrations"]:
        applied = [row["version"] for row in await connection.fetch(
            "SELECT version FROM ingest.schema_migration ORDER BY version"
        )]
        report["latest_migration"] = applied[-1] if applied else None
        report["has_0022"] = "0022_crv_retrieval" in applied
    if server["has_schema"]:
        report.update(dict(await connection.fetchrow(_SIZES, vector_sql(options.model).index)))
    source = await connection.fetchrow(
        _SOURCE, options.source_store, options.source_target, options.release_id
    )
    report["source_rows"], report["source_contents"] = source["rows"], source["contents"]
    usable = max(version(server["installed"]), version(server["available"]))
    installed_ok = server["installed"] is None or version(server["installed"]) >= MIN_VECTOR_VERSION
    report["ok"] = bool(
        usable >= MIN_VECTOR_VERSION and installed_ok and report["source_rows"] > 0
    )
    return report


async def _parity(connection: PgConnection, options: CheckOptions) -> dict[str, Any]:
    scope = (options.source_store, options.source_target, options.release_id)
    report = dict(await connection.fetchrow(_PARITY, *scope))
    report["extra_rows"] = await connection.fetchval(_EXTRA, *scope)
    report["copy_verified_rows"] = await connection.fetchval(
        _COPIED, STORE, TARGET, options.release_id
    )
    problems = ("missing_rows", "digest_mismatches", "missing_vectors", "wrong_release",
                "extra_rows")
    report["ok"] = bool(
        report["source_rows"] > 0
        and all(report[name] == 0 for name in problems)
        and report["copy_verified_rows"] == report["source_rows"]
    )
    return report


async def _sample(connection: PgConnection, options: CheckOptions) -> list[Mapping[str, Any]]:
    return list(await connection.fetch(
        vector_sql(options.model).sample, options.release_id, options.seed, options.sample
    ))


async def search_settings(
    connection: PgConnection, options: CheckOptions
) -> list[tuple[str, str]]:
    """The planner settings ``crv_retrieval.search`` runs with, ef_search optionally overridden."""

    configured = await connection.fetchval(
        "SELECT proconfig FROM pg_proc WHERE oid = to_regprocedure($1)", SEARCH_FUNCTION
    )
    parsed = [m for m in map(_PLANNER_SETTING.match, configured or ()) if m is not None]
    settings = [(m[1], m[2]) for m in parsed if m[1] != "hnsw.ef_search"]
    ef_search = options.ef_search or next(
        (int(m[2]) for m in parsed if m[1] == "hnsw.ef_search"), None
    )
    if ef_search is not None:
        settings.append(("hnsw.ef_search", str(ef_search)))
    return settings


async def _apply(connection: PgConnection, settings: Sequence[tuple[str, str]]) -> None:
    for name, value in settings:
        await connection.execute(f"SET LOCAL {name} = {value}")


async def _approximate(
    connection: PgConnection,
    options: CheckOptions,
    settings: Sequence[tuple[str, str]],
    vector: Sequence[float],
) -> list[tuple[str, float]]:
    async with connection.transaction():
        await _apply(connection, settings)
        rows = await connection.fetch(
            vector_sql(options.model).approximate, options.release_id, list(vector), CANDIDATES,
            options.k,
        )
    return [(row["projection_id"], float(row["distance"])) for row in rows]


async def _exact(
    connection: PgConnection, options: CheckOptions, vector: Sequence[float]
) -> list[tuple[str, float]]:
    async with connection.transaction():
        await connection.execute("SET LOCAL enable_indexscan = off")
        # The planner ignores out-of-line vectors when costing parallel scans.
        await connection.execute("SET LOCAL min_parallel_table_scan_size = 1")
        await connection.execute("SET LOCAL parallel_setup_cost = 1")
        rows = await connection.fetch(
            vector_sql(options.model).exact, options.release_id, list(vector), options.k
        )
    return [(row["projection_id"], float(row["distance"])) for row in rows]


async def _recall_of(
    connection: PgConnection,
    options: CheckOptions,
    settings: Sequence[tuple[str, str]],
    vectors: Sequence[Sequence[float]],
) -> dict[str, Any]:
    key_recall: list[float] = []
    recall: list[float] = []
    approximate_ms: list[float] = []
    exact_seconds = 0.0
    dimensions = vector_sql(options.model).dimensions
    for vector in vectors:
        if len(vector) != dimensions:
            raise InvalidInputError(f"Query vectors need {dimensions} dimensions")
        started = time.perf_counter()
        approximate = await _approximate(connection, options, settings, vector)
        approximate_ms.append((time.perf_counter() - started) * 1000)
        started = time.perf_counter()
        exact = await _exact(connection, options, vector)
        exact_seconds += time.perf_counter() - started
        by_key, by_distance = distance_recall(approximate, exact, options.k)
        key_recall.append(by_key)
        recall.append(by_distance)
    return {
        "queries": len(vectors),
        "recall": round(statistics.fmean(recall), 4) if recall else None,
        "min_recall": round(min(recall), 4) if recall else None,
        "key_recall": round(statistics.fmean(key_recall), 4) if key_recall else None,
        "below_threshold": sum(value < options.min_recall for value in recall),
        "approximate_ms_p50": _percentile(approximate_ms, 0.5),
        "approximate_ms_p95": _percentile(approximate_ms, 0.95),
        "exact_seconds": round(exact_seconds, 1),
    }


async def _recall(
    connection: PgConnection,
    options: CheckOptions,
    settings: Sequence[tuple[str, str]],
    sample: Sequence[Mapping[str, Any]],
    queries: Sequence[Sequence[float]],
) -> dict[str, Any]:
    report: dict[str, Any] = {"k": options.k, "settings": dict(settings),
                              "min_recall": options.min_recall}
    report["sampled"] = await _recall_of(
        connection, options, settings, [row["vector"] for row in sample]
    )
    if queries:
        report["questions"] = await _recall_of(connection, options, settings, queries)
    groups = [report[name] for name in ("sampled", "questions") if name in report]
    measured = [group for group in groups if group["queries"]]
    report["ok"] = bool(measured) and all(
        group["recall"] >= options.min_recall for group in measured
    )
    return report


async def _explain(
    connection: PgConnection,
    options: CheckOptions,
    settings: Sequence[tuple[str, str]],
    sample: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    if not sample:
        return {"ok": False, "error": "no vectors in the release"}
    vector, content_hash = list(sample[0]["vector"]), sample[0]["content_hash"]
    sql = vector_sql(options.model)
    async with connection.transaction():
        await _apply(connection, settings)
        unscoped = plan_summary(await connection.fetchval(
            f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {sql.approximate}",
            options.release_id, vector, CANDIDATES, options.k,
        ))
        scoped = plan_summary(await connection.fetchval(
            f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {sql.scoped}",
            options.release_id, vector, [content_hash], options.k,
        ))
        lexical = plan_summary(await connection.fetchval(
            f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {_LEXICAL}",
            options.release_id, sample[0]["title"],
        ))
    return {
        "settings": dict(settings),
        "unscoped": unscoped,
        "scoped": scoped,
        "lexical": {"query": sample[0]["title"], **lexical},
        "ok": sql.index in unscoped["indexes"]
        and not any(plan["chunk_seq_scan"] for plan in (unscoped, scoped, lexical)),
    }


@dataclass(frozen=True)
class _LatencyCase:
    text: str
    vector: Sequence[float]
    content_hash: str


_LatencyQuery = tuple[str | None, Sequence[float] | None, tuple[str, ...]]


def _latency_cases(
    sample: Sequence[Mapping[str, Any]],
    queries: Sequence[Sequence[float]],
    texts: Sequence[str],
) -> list[_LatencyCase]:
    """Questions with their embeddings, else the sampled chunks (title as the text)."""

    if not texts:
        return [_LatencyCase(row["title"], row["vector"], row["content_hash"]) for row in sample]
    if len(texts) != len(queries):
        raise InvalidInputError("Each latency question needs its query vector")
    # Any stored content gives a scope of realistic size; relevance is not measured here.
    scopes = [row["content_hash"] for row in sample] or ["0" * 64]
    return [
        _LatencyCase(text, vector, scopes[index % len(scopes)])
        for index, (text, vector) in enumerate(zip(texts, queries, strict=True))
    ]


async def _timed_search(
    connection: PgConnection,
    options: CheckOptions,
    text: str | None,
    vector: Sequence[float] | None,
    scope: Sequence[str],
) -> float:
    started = time.perf_counter()
    await connection.fetch(
        _SEARCH, options.release_id, text, list(scope), options.model if vector else None,
        list(vector) if vector else None, options.k,
    )
    return (time.perf_counter() - started) * 1000


def _latency_summary(rows: Sequence[tuple[str, float]]) -> dict[str, Any]:
    values = [milliseconds for _, milliseconds in rows]
    slowest = sorted(rows, key=lambda row: -row[1])[:3]
    return {
        "queries": len(values),
        "p50_ms": _percentile(values, 0.5),
        "p95_ms": _percentile(values, 0.95),
        "max_ms": round(max(values), 1) if values else None,
        "slowest": [{"query": text[:80], "ms": round(ms, 1)} for text, ms in slowest],
    }


async def _latency(
    connection: PgConnection,
    options: CheckOptions,
    cases: Sequence[_LatencyCase],
) -> dict[str, Any]:
    """``crv_retrieval.search`` as the agent calls it (hybrid) over every query, then again,
    before the diagnostic variants: a variant run first for a query would cache its pages for
    the next. The gate applies to the first hybrid pass, as each agent question is new."""

    variants: dict[str, Callable[[_LatencyCase], _LatencyQuery]] = {
        "hybrid": lambda case: (case.text, case.vector, ()),
        "scoped": lambda case: (case.text, case.vector, (case.content_hash,)),
        "keyword": lambda case: (case.text, None, ()),
        "vector": lambda case: (None, case.vector, ()),
    }
    deadline = time.monotonic() + LATENCY_BUDGET_SECONDS
    passes: list[dict[str, list[tuple[str, float]]]] = [
        {name: [] for name in variants} for _ in range(2)
    ]
    for name, build in variants.items():
        for measured in passes:
            for case in cases:
                if time.monotonic() > deadline:
                    break
                text, vector, scope = build(case)
                milliseconds = await _timed_search(connection, options, text, vector, scope)
                measured[name].append((case.text, milliseconds))
    report: dict[str, Any] = {
        "cases": len(cases),
        "truncated": any(
            len(rows) < len(cases) for measured in passes for rows in measured.values()
        ),
        "max_p95_ms": options.max_p95_ms,
    }
    for label, measured in zip(("first_pass", "repeat_pass"), passes, strict=True):
        report[label] = {name: _latency_summary(rows) for name, rows in measured.items()}
    gated = report["first_pass"]["hybrid"]["p95_ms"]
    report["ok"] = gated is not None and gated <= options.max_p95_ms
    return report


async def verify_release(
    connection: PgConnection,
    checks: Sequence[str],
    options: CheckOptions,
    queries: Sequence[Sequence[float]] = (),
    texts: Sequence[str] = (),
) -> dict[str, Any]:
    """Run the requested checks in ``CHECKS`` order; ``ok`` only when every check passes."""
    unknown = sorted(set(checks) - set(CHECKS))
    if unknown or not checks:
        raise InvalidInputError(f"Checks must be among {', '.join(CHECKS)}")
    report: dict[str, Any] = {"release_id": options.release_id}
    if "preflight" in checks:
        report["preflight"] = await _preflight(connection, options)
    if "parity" in checks:
        report["parity"] = await _parity(connection, options)
    sample = (
        await _sample(connection, options)
        if {"recall", "explain", "latency"} & set(checks) else []
    )
    settings = (
        await search_settings(connection, options)
        if {"recall", "explain"} & set(checks) else []
    )
    if "recall" in checks:
        report["recall"] = await _recall(connection, options, settings, sample, queries)
    if "explain" in checks:
        report["explain"] = await _explain(connection, options, settings, sample)
    if "latency" in checks:
        report["latency"] = await _latency(
            connection, options, _latency_cases(sample, queries, texts)
        )
    report["ok"] = all(report[name]["ok"] for name in CHECKS if name in report)
    return report
