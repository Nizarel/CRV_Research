"""Deterministic identifiers, canonical JSON and fingerprints."""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from typing import Annotated, Any
from urllib.parse import quote

from pydantic import BaseModel, StringConstraints

SHA256_PATTERN = r"^[0-9a-f]{64}$"
Sha256Hex = Annotated[str, StringConstraints(pattern=SHA256_PATTERN)]

FINGERPRINT_SCHEMA_VERSION = 1


def _normalize(value: Any) -> Any:
    if isinstance(value, Enum):
        return _normalize(value.value)
    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Canonical JSON rejects NaN and infinite floats")
        return value
    if isinstance(value, BaseModel):
        return _normalize(value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("Canonical JSON object keys must be strings")
            normalized[key] = _normalize(item)
        return normalized
    if isinstance(value, list | tuple):
        return [_normalize(item) for item in value]
    if isinstance(value, datetime):
        if value.utcoffset() is None:
            raise ValueError("Canonical JSON rejects naive datetimes")
        return value.astimezone(UTC).isoformat()
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("Canonical JSON rejects non-finite decimals")
        return str(value)
    raise TypeError(f"Unsupported canonical JSON type: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    """Serialize ``value`` deterministically: sorted keys, no whitespace, UTC datetimes.

    Sets are rejected because they have no canonical order; callers pass sorted lists.
    """

    return json.dumps(
        _normalize(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def fingerprint(kind: str, payload: Mapping[str, Any]) -> str:
    """Digest of a typed, versioned payload; equal inputs always yield equal fingerprints."""

    return sha256_hex(
        canonical_json({"kind": kind, "payload": payload, "schema": FINGERPRINT_SCHEMA_VERSION})
    )


def stable_urn(kind: str, *parts: str) -> str:
    """Phase 1 knowledge-foundation URN algorithm, so v1 identifiers stay valid."""

    key = json.dumps(parts, ensure_ascii=True, separators=(",", ":"))
    return f"urn:tizert:{kind}:{uuid.uuid5(uuid.NAMESPACE_URL, key)}"


def legacy_source_uri(container_url: str, blob_name: str) -> str:
    return f"{container_url.rstrip('/')}/{quote(blob_name, safe='/')}"


def legacy_source_object_id(container_url: str, blob_name: str) -> str:
    """Phase 1 ``SourceObject.id`` (removed v1 Blob manifest), preserved for continuity."""

    return str(uuid.uuid5(uuid.NAMESPACE_URL, legacy_source_uri(container_url, blob_name)))


def legacy_placement_id(source_object_id: str, blob_name: str) -> str:
    """Phase 1 ``Placement.id`` (removed v1 path metadata), preserved for continuity."""

    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{source_object_id}:placement:{blob_name}"))


def job_id_for(unit_id: str, stage: str, job_fingerprint: str) -> str:
    return stable_urn("job", unit_id, stage, job_fingerprint)


def snapshot_id_for(source_object_id: str, etag: str, content_hash: str) -> str:
    return stable_urn("snapshot", source_object_id, etag, content_hash)


def artifact_key(digest: str) -> str:
    """Content-addressed object name inside the artifact container."""

    return f"sha256/{digest[:2]}/{digest}"


def new_event_id() -> str:
    return str(uuid.uuid4())


def new_run_id() -> str:
    return str(uuid.uuid4())
