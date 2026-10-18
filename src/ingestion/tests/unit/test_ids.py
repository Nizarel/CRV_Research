from __future__ import annotations

import math
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tizertmaint_pipeline.domain.ids import (
    artifact_key,
    canonical_json,
    fingerprint,
    job_id_for,
    legacy_placement_id,
    legacy_source_object_id,
    stable_urn,
)

json_scalars = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(),
    st.text(),
    st.floats(allow_nan=False, allow_infinity=False),
)
json_values = st.recursive(
    json_scalars,
    lambda children: (
        st.lists(children, max_size=4) | st.dictionaries(st.text(max_size=8), children, max_size=4)
    ),
    max_leaves=12,
)


@given(st.dictionaries(st.text(max_size=8), json_values, max_size=6))
def test_canonical_json_ignores_key_insertion_order(payload: dict) -> None:
    reversed_payload = dict(reversed(list(payload.items())))
    assert canonical_json(payload) == canonical_json(reversed_payload)
    assert fingerprint("k", payload) == fingerprint("k", reversed_payload)


def test_canonical_json_normalizes_supported_types() -> None:
    moment = datetime(2026, 1, 1, 12, tzinfo=UTC)
    assert canonical_json({"b": (1, 2), "a": moment, "d": Decimal("1.50")}) == (
        '{"a":"2026-01-01T12:00:00+00:00","b":[1,2],"d":"1.50"}'
    )


@pytest.mark.parametrize(
    "value",
    [math.nan, math.inf, {1, 2}, datetime(2026, 1, 1), {1: "non-string key"}, Decimal("NaN")],
)
def test_canonical_json_rejects_ambiguous_values(value: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        canonical_json({"value": value})


def test_fingerprint_depends_on_kind_and_payload() -> None:
    base = fingerprint("stage:snapshot", {"a": 1})
    assert base == fingerprint("stage:snapshot", {"a": 1})
    assert base != fingerprint("stage:snapshot", {"a": 2})
    assert base != fingerprint("stage:other", {"a": 1})
    assert len(base) == 64


def test_stable_urn_matches_v1_knowledge_identifiers() -> None:
    # Value produced by the v1 discovery.stable_id before the cleanup.
    assert stable_urn("equipment", "6220-PP-2211", "zone") == (
        "urn:tizert:equipment:0e08a57a-7349-5f43-bdf5-1efa4cedfc06"
    )


def test_legacy_ids_match_phase1_manifest_and_placement() -> None:
    # Values produced by the v1 blob_manifest and path_metadata modules before the cleanup.
    container_url = "https://account.blob.core.windows.net/content"
    blob_name = "2-Zone Broyage/1-Grinding 1/6220-PP-2211 Pump/Manuals/N°1 manual.pdf"

    source_id = legacy_source_object_id(container_url, blob_name)

    assert source_id == "23c4b610-12f8-56cf-8506-2455fd3e43d9"
    assert legacy_placement_id(source_id, blob_name) == "b37bdbef-7aa1-5c31-b329-1e63f5cf569a"
    assert legacy_source_object_id(container_url + "/", blob_name) == source_id


def test_job_ids_and_artifact_keys_are_deterministic() -> None:
    digest = "ab" + "0" * 62
    assert job_id_for("unit", "snapshot", digest) == job_id_for("unit", "snapshot", digest)
    assert job_id_for("unit", "snapshot", digest).startswith("urn:tizert:job:")
    uuid.UUID(job_id_for("unit", "snapshot", digest).rsplit(":", 1)[1])
    assert artifact_key(digest) == f"sha256/ab/{digest}"
