from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from azure.core.exceptions import HttpResponseError

from tizertmaint_pipeline.adapters.search.projector import AzureSearchProjector
from tizertmaint_pipeline.domain.errors import AuthError, InvalidInputError


def _http_error(status: int) -> HttpResponseError:
    error = HttpResponseError(message=f"HTTP {status}")
    error.status_code = status
    error.response = SimpleNamespace(headers={})
    return error


class _Results:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        for row in self._rows:
            yield row


class _SearchClient:
    def __init__(self, statuses: dict[str, int] | None = None) -> None:
        self.statuses = statuses or {}
        self.batches: list[list[dict[str, Any]]] = []
        self.failures: list[Exception] = []
        self.omit: set[str] = set()
        self.index: dict[str, dict[str, Any]] = {}
        self.filters: list[str] = []

    async def upload_documents(self, documents: list[dict[str, Any]]):
        if self.failures:
            raise self.failures.pop(0)
        self.batches.append(documents)
        results = []
        for document in documents:
            key = document["projection_id"]
            if key in self.omit:
                continue
            status = self.statuses.get(key, 201)
            if status in (200, 201):
                self.index[key] = document
            results.append(
                SimpleNamespace(
                    key=key, succeeded=status in (200, 201), status_code=status, error_message=None
                )
            )
        return results

    async def search(self, *, search_text: str, filter: str, select: list[str], top: int):
        self.filters.append(filter)
        keys = filter.split("'")[1].split("|")
        return _Results([self.index[key] for key in keys if key in self.index][:top])

    async def delete_documents(self, documents: list[dict[str, Any]]):
        self.batches.append(documents)
        return [
            SimpleNamespace(
                key=document["projection_id"], succeeded=True, status_code=200, error_message=None
            )
            for document in documents
        ]


def _doc(key: str, size: int = 10) -> dict[str, Any]:
    return {"projection_id": key, "content": "x" * size, "payload_sha256": f"digest-{key}"}


async def test_upload_reports_each_key_and_never_marks_partial_success_complete() -> None:
    client = _SearchClient({"b": 503, "c": 400})
    projector = AzureSearchProjector(client, max_batch_documents=10, max_batch_bytes=10_000)

    report = await projector.upload([_doc("a"), _doc("b"), _doc("c")], key_field="projection_id")

    assert report.accepted == ("a",)
    assert set(report.retryable) == {"b"}
    assert set(report.permanent) == {"c"}
    assert not report.complete


async def test_upload_batches_by_bytes_and_count_and_isolates_oversized_documents() -> None:
    client = _SearchClient()
    documents = [_doc(str(index), size=100) for index in range(5)] + [_doc("huge", size=5000)]
    one_doc_bytes = len(json.dumps(documents[0], separators=(",", ":")).encode())
    projector = AzureSearchProjector(
        client, max_batch_documents=3, max_batch_bytes=one_doc_bytes * 2 + 1
    )

    report = await projector.upload(documents, key_field="projection_id")

    assert [len(batch) for batch in client.batches] == [2, 2, 1]
    assert set(report.permanent) == {"huge"}
    assert len(report.accepted) == 5


async def test_missing_item_results_and_transient_batch_failures_are_retryable() -> None:
    client = _SearchClient()
    client.omit = {"b"}
    client.failures = [_http_error(503)]
    projector = AzureSearchProjector(client, max_batch_documents=1, max_batch_bytes=10_000)

    report = await projector.upload([_doc("a"), _doc("b")], key_field="projection_id")

    assert set(report.retryable) == {"a", "b"}
    assert "HTTP 503" in report.retryable["a"]


async def test_auth_failures_stop_the_projection() -> None:
    client = _SearchClient()
    client.failures = [_http_error(403)]
    projector = AzureSearchProjector(client, max_batch_documents=10, max_batch_bytes=10_000)
    with pytest.raises(AuthError):
        await projector.upload([_doc("a")], key_field="projection_id")


async def test_rejected_batches_are_permanent_not_retried_forever() -> None:
    client = _SearchClient()
    client.failures = [_http_error(400)]
    projector = AzureSearchProjector(client, max_batch_documents=10, max_batch_bytes=10_000)

    report = await projector.upload([_doc("a"), _doc("b")], key_field="projection_id")

    assert set(report.permanent) == {"a", "b"}
    assert not report.retryable


@pytest.mark.parametrize("keys", [["a", "a"], ["bad'key"], [""], ["_leading"]])
async def test_upload_rejects_duplicate_or_unsafe_keys(keys: list[str]) -> None:
    projector = AzureSearchProjector(
        _SearchClient(), max_batch_documents=10, max_batch_bytes=10_000
    )
    with pytest.raises(InvalidInputError):
        await projector.upload([_doc(key) for key in keys], key_field="projection_id")


async def test_delete_treats_keys_the_index_refuses_as_already_absent() -> None:
    client = _SearchClient()
    projector = AzureSearchProjector(client, max_batch_documents=10, max_batch_bytes=10_000)

    report = await projector.delete(["_leading", "a"], key_field="projection_id")

    assert set(report.accepted) == {"_leading", "a"}
    assert client.batches == [[{"projection_id": "a"}]]


async def test_verify_detects_missing_and_stale_rows() -> None:
    client = _SearchClient()
    projector = AzureSearchProjector(client, max_batch_documents=10, max_batch_bytes=10_000)
    await projector.upload([_doc("a"), _doc("b")], key_field="projection_id")

    problems = await projector.verify(
        {"a": "digest-a", "b": "other", "c": "digest-c"},
        key_field="projection_id",
        digest_field="payload_sha256",
    )

    assert problems == {"b": "digest_mismatch", "c": "missing"}
    assert client.filters == ["search.in(projection_id, 'a|b|c', '|')"]
