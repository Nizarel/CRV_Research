from __future__ import annotations

import math

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ingest_pipeline.domain.batching import pack
from ingest_pipeline.domain.errors import IntegrityError
from ingest_pipeline.domain.projection import (
    ItemDisposition,
    ProjectionReport,
    classify_item,
)
from ingest_pipeline.domain.vectors import reconcile_embeddings


@given(
    st.lists(st.integers(min_value=0, max_value=50), max_size=60),
    st.integers(min_value=1, max_value=10),
    st.integers(min_value=1, max_value=60),
)
def test_pack_preserves_order_covers_items_and_respects_limits(
    sizes: list[int], max_items: int, max_total: int
) -> None:
    result = pack(sizes, max_items=max_items, max_total=max_total)
    flattened = [index for batch in result.batches for index in batch]
    assert flattened == sorted(flattened)
    assert sorted(flattened + list(result.oversized)) == list(range(len(sizes)))
    for batch in result.batches:
        assert 1 <= len(batch) <= max_items
        assert sum(sizes[index] for index in batch) <= max_total
    assert all(sizes[index] > max_total for index in result.oversized)


def test_pack_applies_a_per_item_cap() -> None:
    result = pack([5, 20, 5], max_items=10, max_total=100, max_item=10)
    assert result.oversized == (1,)
    assert result.batches == ((0, 2),)


def _vector(value: float = 0.5, dims: int = 3) -> list[float]:
    return [value] * dims


def test_reconcile_returns_vectors_in_input_order() -> None:
    vectors = reconcile_embeddings(2, [(1, _vector(0.2)), (0, _vector(0.1))], dimensions=3)
    assert vectors == [(0.1, 0.1, 0.1), (0.2, 0.2, 0.2)]


@pytest.mark.parametrize(
    ("items", "message"),
    [
        ([(0, _vector())], "Missing 1"),
        ([(0, _vector()), (0, _vector()), (1, _vector())], "Duplicate"),
        ([(0, _vector()), (2, _vector())], "outside"),
        ([(0, _vector()), (1, _vector(dims=2))], "dimensions"),
        ([(0, _vector()), (1, [0.1, math.nan, 0.1])], "non-finite"),
        ([(0, _vector()), (1, [0.0, 0.0, 0.0])], "all-zero"),
    ],
)
def test_reconcile_rejects_incomplete_or_invalid_responses(items, message: str) -> None:
    with pytest.raises(IntegrityError, match=message):
        reconcile_embeddings(2, items, dimensions=3)


@pytest.mark.parametrize(
    ("succeeded", "status", "expected"),
    [
        (True, 200, ItemDisposition.ACCEPTED),
        (True, 201, ItemDisposition.ACCEPTED),
        (False, 409, ItemDisposition.RETRYABLE),
        (False, 422, ItemDisposition.RETRYABLE),
        (False, 503, ItemDisposition.RETRYABLE),
        (False, 400, ItemDisposition.PERMANENT),
        (False, 404, ItemDisposition.PERMANENT),
        (False, 429, ItemDisposition.PERMANENT),
    ],
)
def test_search_item_classification(
    succeeded: bool, status: int, expected: ItemDisposition
) -> None:
    assert classify_item(succeeded=succeeded, status_code=status) is expected


def test_projection_report_merge_prefers_later_acceptance() -> None:
    first = ProjectionReport(accepted=("a",), retryable={"b": "503"})
    retried = ProjectionReport(accepted=("b",))
    merged = first.merge(retried)
    assert merged.accepted == ("a", "b")
    assert merged.complete
    assert not ProjectionReport(permanent={"c": "400"}).complete
