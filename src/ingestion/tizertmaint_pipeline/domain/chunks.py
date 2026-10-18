"""Evidence chunks: versioned identities built from canonical evidence, never from ordinals."""

from __future__ import annotations

import base64
import hashlib
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from .evidence import EvidenceKind
from .ids import fingerprint, sha256_hex

CHUNK_SET_SCHEMA = "chunk-set.v1"


class ChunkKind(StrEnum):
    PROSE = "prose"
    TABLE = "table"
    DRAWING_TEXT = "drawing_text"
    DRAWING_TABLE = "drawing_table"
    FIGURE = "figure"


class EvidenceChunk(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    chunk_id: str
    evidence_id: str
    ordinal: int = Field(ge=0)
    kind: ChunkKind
    text: str = Field(min_length=1)
    token_count: int = Field(gt=0)
    tokenizer: str
    chunker: str
    block_ids: tuple[str, ...]
    context_block_ids: tuple[str, ...] = ()
    """Blocks repeated from the previous chunk as bounded overlap; never new evidence."""
    pages: tuple[int, ...] = ()
    locators: tuple[str, ...] = ()
    section_path: tuple[str, ...] = ()
    unit_ids: tuple[str, ...] = ()
    figure_ids: tuple[str, ...] = ()
    sheet: str | None = None
    evidence_kind: EvidenceKind = EvidenceKind.SOURCE_TEXT
    language: str | None = None
    overlap_tokens: int = Field(default=0, ge=0)
    process_case_id: str | None = None
    """Printed process case shared by every value column of a stream-table chunk."""

    @property
    def text_sha256(self) -> str:
        return sha256_hex(self.text)


def chunk_id_for(
    *,
    evidence_id: str,
    chunker: str,
    tokenizer: str,
    block_ids: tuple[str, ...],
    text: str,
) -> str:
    """Identity from evidence, chunker version, tokenizer, boundaries and exact text (ING-R12)."""

    return "chunk:" + fingerprint(
        "chunk",
        {
            "evidence_id": evidence_id,
            "chunker": chunker,
            "tokenizer": tokenizer,
            "first": block_ids[0] if block_ids else None,
            "last": block_ids[-1] if block_ids else None,
            "text": sha256_hex(text),
        },
    )


def projection_key(release_id: str, chunk_id: str) -> str:
    """URL-safe, Search-legal key: one row per (release, chunk), shared by every placement."""

    digest = hashlib.sha256(f"{release_id}|{chunk_id}".encode()).digest()
    key = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    # Azure AI Search rejects the whole batch when a document key starts with "_".
    return "k" + key[1:] if key.startswith("_") else key
