"""Offline intake manifests: originals, derived objects, and explicit per-input outcomes."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from .ids import Sha256Hex
from .models import Frozen


class IntakeLimits(Frozen):
    max_source_bytes: int = Field(default=1024**3, gt=0)
    max_total_bytes: int = Field(default=64 * 1024**3, gt=0)
    max_archive_bytes: int = Field(default=1024**3, gt=0)
    max_members: int = Field(default=20_000, gt=0)
    max_depth: int = Field(default=2, ge=0, le=10)
    max_ratio: float = Field(default=200, gt=1)


class IntakeItem(Frozen):
    source_path: str
    status: Literal["ready", "converted", "expanded", "excluded", "failed"]
    input_hash: Sha256Hex | None = None
    content_hash: Sha256Hex | None = None
    file_kind: str | None = None
    size_bytes: int = Field(default=0, ge=0)
    archive_members: tuple[str, ...] = ()
    ancestor_hashes: tuple[Sha256Hex, ...] = ()
    converter: str | None = None
    reason: str | None = None


class IntakeManifest(Frozen):
    schema_name: Literal["crv-intake.v1"] = "crv-intake.v1"
    root: str
    complete: bool = False
    items: tuple[IntakeItem, ...] = ()

    @property
    def failed(self) -> int:
        return sum(item.status == "failed" for item in self.items)
