from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from ingest_pipeline.adapters.postgres import retrieval_checks
from ingest_pipeline.adapters.postgres.retrieval_checks import (
    CheckOptions,
    distance_recall,
    plan_summary,
    search_settings,
    verify_release,
    version,
)
from ingest_pipeline.cli import build_parser, read_questions
from ingest_pipeline.domain.errors import InvalidInputError

OPTIONS = CheckOptions(release_id="crv-v1", source_store="azure_search",
                       source_target="crv-evidence-v1", sample=2, k=3)


def test_distance_recall_counts_tied_duplicates_but_not_misses() -> None:
    exact = [("a", 0.0), ("b", 0.1), ("c", 0.2)]
    assert distance_recall([("a", 0.0), ("b", 0.1), ("c", 0.2)], exact, 3) == (1.0, 1.0)
    # "d" is another placement of c's text: same distance, different key.
    assert distance_recall([("a", 0.0), ("b", 0.1), ("d", 0.2)], exact, 3) == (2 / 3, 1.0)
    assert distance_recall([("a", 0.0), ("e", 0.5)], exact, 3) == (1 / 3, 1 / 3)
    assert distance_recall([], [], 3) == (1.0, 1.0)


def test_plan_summary_reads_indexes_scans_and_buffers() -> None:
    explained = json.dumps([{
        "Plan": {
            "Node Type": "Limit", "Shared Hit Blocks": 40, "Shared Read Blocks": 2,
            "Plans": [{"Node Type": "Index Scan", "Index Name": "chunk_te3_hnsw_idx",
                       "Relation Name": "chunk"}],
        },
        "Execution Time": 12.5,
    }])
    summary = plan_summary(explained)
    assert summary == {
        "nodes": ["Index Scan", "Limit"], "indexes": ["chunk_te3_hnsw_idx"],
        "chunk_seq_scan": False, "execution_ms": 12.5, "shared_hit_blocks": 40,
        "shared_read_blocks": 2,
    }
    seq = [{"Plan": {"Node Type": "Seq Scan", "Relation Name": "chunk"}}]
    assert plan_summary(seq)["chunk_seq_scan"] is True


def test_version_parsing() -> None:
    assert version("0.8.2") == (0, 8, 2)
    assert version("0.7.4") < retrieval_checks.MIN_VECTOR_VERSION <= version("0.8.0")
    assert version(None) == ()


def test_vector_sql_follows_each_columns_index() -> None:
    te3, m3 = retrieval_checks.vector_sql("te3"), retrieval_checks.vector_sql("m3")
    assert "halfvec(3072)" in te3.approximate
    assert te3.index == "chunk_te3_hnsw_idx"
    assert "halfvec" not in m3.approximate
    assert "vector(1024)" in m3.approximate
    assert "embedding_m3" in m3.sample
    assert m3.index == "chunk_m3_hnsw_idx"
    with pytest.raises(InvalidInputError, match="model"):
        retrieval_checks.vector_sql("bge")
    with pytest.raises(InvalidInputError, match="model"):
        CheckOptions(release_id="r", source_store="s", source_target="t", model="bge")
    args = build_parser().parse_args(["retrieval-verify", "recall", "--model", "m3"])
    assert args.model == "m3"


@pytest.mark.parametrize(
    ("changes", "message"),
    [({"sample": -1}, "sample"), ({"k": 51}, "sample"), ({"ef_search": 5}, "ef_search"),
     ({"min_recall": 0}, "ef_search")],
)
def test_options_reject_out_of_range_values(changes: dict[str, Any], message: str) -> None:
    with pytest.raises(InvalidInputError, match=message):
        CheckOptions(release_id="r", source_store="s", source_target="t", **changes)


class _Connection:
    def __init__(self, rows: dict[str, Any], values: list[Any]) -> None:
        self.rows = rows
        self.values = values
        self.queries: list[str] = []

    async def fetchrow(self, query: str, *args: Any) -> Any:
        self.queries.append(query)
        for marker, row in self.rows.items():
            if marker in query:
                return row
        raise AssertionError(query)

    async def fetchval(self, query: str, *args: Any) -> Any:
        self.queries.append(query)
        return self.values.pop(0)

    async def fetch(self, query: str, *args: Any) -> list[Any]:
        self.queries.append(query)
        return [{"version": "0021_x"}, {"version": "0022_crv_retrieval"}]

    async def execute(self, query: str, *args: Any) -> str:
        raise AssertionError("checks never write")

    @asynccontextmanager
    async def transaction(self) -> Any:
        yield


SERVER = {"installed": None, "available": "0.8.2", "has_schema": False, "database_bytes": 1,
          "shared_buffers": "1GB", "work_mem": "4MB", "maintenance_work_mem": "64MB",
          "max_parallel_maintenance_workers": "2", "max_connections": "429",
          "can_read_migrations": True}


async def test_preflight_passes_before_the_schema_exists_and_fails_on_old_pgvector() -> None:
    rows = {"pg_extension": SERVER, "ingest.projection_item": {"rows": 145855, "contents": 1929}}
    report = await verify_release(_Connection(rows, []), ["preflight"], OPTIONS)
    assert report["ok"] is True
    assert report["preflight"]["has_0022"] is True
    assert report["preflight"]["source_rows"] == 145855

    old = {**rows, "pg_extension": {**SERVER, "installed": "0.7.4", "available": "0.8.2"}}
    assert (await verify_release(_Connection(old, []), ["preflight"], OPTIONS))["ok"] is False


@pytest.mark.parametrize(
    ("problem", "values", "ok"),
    [({}, [0, 10], True), ({"digest_mismatches": 1}, [0, 10], False), ({}, [2, 10], False),
     ({}, [0, 9], False)],
)
async def test_parity_needs_every_source_row_copied_and_identical(
    problem: dict[str, int], values: list[int], ok: bool
) -> None:
    parity = {"source_rows": 10, "missing_rows": 0, "digest_mismatches": 0,
              "missing_vectors": 0, "wrong_release": 0, **problem}
    report = await verify_release(
        _Connection({"LEFT JOIN crv_retrieval.chunk": parity}, list(values)), ["parity"], OPTIONS
    )
    assert report["parity"]["ok"] is ok
    assert report["ok"] is ok


async def test_recall_gate_uses_sampled_vectors(monkeypatch: pytest.MonkeyPatch) -> None:
    async def sample(*_: Any) -> list[dict[str, Any]]:
        return [{"projection_id": "a", "content_hash": "c" * 64, "vector": [0.0] * 3072}]

    async def approximate(*_: Any) -> list[tuple[str, float]]:
        return [("a", 0.0), ("x", 0.9), ("y", 0.9)]

    async def exact(*_: Any) -> list[tuple[str, float]]:
        return [("a", 0.0), ("b", 0.1), ("c", 0.2)]

    async def settings(*_: Any) -> list[tuple[str, str]]:
        return [("hnsw.ef_search", "500")]

    monkeypatch.setattr(retrieval_checks, "_sample", sample)
    monkeypatch.setattr(retrieval_checks, "_approximate", approximate)
    monkeypatch.setattr(retrieval_checks, "_exact", exact)
    monkeypatch.setattr(retrieval_checks, "search_settings", settings)
    report = await verify_release(_Connection({}, []), ["recall"], OPTIONS)
    assert report["recall"]["sampled"]["recall"] == pytest.approx(1 / 3, abs=1e-4)
    assert report["recall"]["sampled"]["below_threshold"] == 1
    assert report["ok"] is False


async def test_latency_runs_hybrid_first_and_gates_its_first_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def sample(*_: Any) -> list[dict[str, Any]]:
        return [{"projection_id": "a", "content_hash": "c" * 64, "title": "Titre",
                 "vector": [0.0] * 3072}]

    calls: list[tuple[str | None, bool, tuple[str, ...]]] = []

    async def timed(
        _: Any, __: Any, text: str | None, vector: Any, scope: Any
    ) -> float:
        calls.append((text, vector is not None, tuple(scope)))
        return 500.0 if len(calls) == 1 else 50.0

    monkeypatch.setattr(retrieval_checks, "_sample", sample)
    monkeypatch.setattr(retrieval_checks, "_timed_search", timed)
    report = await verify_release(_Connection({}, []), ["latency"], OPTIONS)

    hybrid, scoped = ("Titre", True, ()), ("Titre", True, ("c" * 64,))
    keyword, vector = ("Titre", False, ()), (None, True, ())
    assert calls == [hybrid, hybrid, scoped, scoped, keyword, keyword, vector, vector]
    latency = report["latency"]
    assert latency["first_pass"]["hybrid"]["p95_ms"] == 500.0
    assert latency["repeat_pass"]["hybrid"]["p95_ms"] == 50.0
    assert (latency["truncated"], latency["ok"], report["ok"]) == (False, True, True)

    strict = CheckOptions(release_id="crv-v1", source_store="s", source_target="t",
                          max_p95_ms=100)
    calls.clear()
    assert (await verify_release(_Connection({}, []), ["latency"], strict))["ok"] is False


async def test_latency_uses_the_questions_and_their_vectors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def sample(*_: Any) -> list[dict[str, Any]]:
        return [{"projection_id": "a", "content_hash": "c" * 64, "title": "Titre",
                 "vector": [0.0] * 3072}]

    texts: list[str | None] = []

    async def timed(_: Any, __: Any, text: str | None, *___: Any) -> float:
        texts.append(text)
        return 1.0

    monkeypatch.setattr(retrieval_checks, "_sample", sample)
    monkeypatch.setattr(retrieval_checks, "_timed_search", timed)
    vectors = [[0.0] * 3072, [1.0] * 3072]
    report = await verify_release(
        _Connection({}, []), ["latency"], OPTIONS, vectors, ["Quelle teneur ?", "Why?"]
    )
    assert report["latency"]["cases"] == 2
    assert set(texts) == {"Quelle teneur ?", "Why?", None}
    with pytest.raises(InvalidInputError, match="query vector"):
        await verify_release(_Connection({}, []), ["latency"], OPTIONS, vectors[:1], ["a", "b"])


async def test_checks_must_be_known() -> None:
    with pytest.raises(InvalidInputError):
        await verify_release(_Connection({}, []), ["drop"], OPTIONS)


async def test_search_settings_follow_the_deployed_function() -> None:
    configured = ["search_path=pg_catalog, public, pg_temp", "hnsw.ef_search=500",
                  "hnsw.iterative_scan=relaxed_order", "enable_seqscan=off", "enable_sort=off"]
    applied = await search_settings(_Connection({}, [configured]), OPTIONS)
    assert applied == [("hnsw.iterative_scan", "relaxed_order"), ("enable_seqscan", "off"),
                       ("enable_sort", "off"), ("hnsw.ef_search", "500")]
    tuned = CheckOptions(release_id="crv-v1", source_store="s", source_target="t", ef_search=200)
    assert (await search_settings(_Connection({}, [configured]), tuned))[-1] == (
        "hnsw.ef_search", "200"
    )
    assert await search_settings(_Connection({}, [None]), OPTIONS) == []


def test_parser_and_question_cap(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        ["retrieval-verify", "parity", "recall", "--sample", "5", "--ef-search", "200"]
    )
    assert (args.checks, args.sample, args.ef_search, args.questions) == (
        ["parity", "recall"], 5, 200, ""
    )
    golden = tmp_path / "golden.jsonl"
    golden.write_text('{"question": " Quelle teneur ? "}\n\n{"question": "Why?"}\n',
                      encoding="utf-8")
    assert read_questions(str(golden)) == ["Quelle teneur ?", "Why?"]
    golden.write_text('{"question": "x"}\n' * 201, encoding="utf-8")
    with pytest.raises(SystemExit):
        read_questions(str(golden))
