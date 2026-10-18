"""Versioned work-message contract (schema v2): IDs only, never document content."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .errors import InvalidInputError
from .ids import Sha256Hex
from .states import StageName

MAX_ENVELOPE_BYTES = 8 * 1024


class WorkEnvelope(BaseModel):
    """Wake-up for one durable stage job; the ledger, not the message, is authoritative."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["2"] = "2"
    event_id: str = Field(min_length=1, max_length=128)
    job_id: str = Field(min_length=1, max_length=256)
    stage: StageName
    unit_id: str = Field(min_length=1, max_length=512)
    fingerprint: Sha256Hex
    source_snapshot_id: str | None = Field(default=None, max_length=256)
    content_hash: Sha256Hex | None = None
    traceparent: str | None = Field(default=None, max_length=128)

    def to_bytes(self) -> bytes:
        return self.model_dump_json(exclude_none=True).encode("utf-8")

    @classmethod
    def parse(cls, body: bytes | str) -> WorkEnvelope:
        raw = body.encode("utf-8") if isinstance(body, str) else body
        if len(raw) > MAX_ENVELOPE_BYTES:
            raise InvalidInputError(f"Envelope exceeds {MAX_ENVELOPE_BYTES} bytes")
        try:
            return cls.model_validate_json(raw)
        except ValidationError as error:
            raise InvalidInputError(
                f"Invalid work envelope: {error.error_count()} validation error(s)"
            ) from error
