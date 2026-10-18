"""Content Understanding contracts: analyzer profiles, grounded field observations and usage.

The service returns typed field values already normalized (dates, numbers) and does not return
the printed text separately, so the printed text is recovered from each field's spans into the
result Markdown and kept beside the normalized value.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .errors import ConfigurationError
from .evidence import EvidenceKind
from .ids import Sha256Hex, canonical_json, sha256_hex
from .models import ProviderUsage
from .sniff import FileKind

PROVIDER = "content_understanding"
API_VERSION = "2025-11-01"
CU_MARKDOWN = "cu_markdown"
RAW_SCHEMA = "cu-analyze-operation.2025-11-01"

_SOURCE = re.compile(r"D\((\d+),([^)]*)\)")
_VALUE_KEYS = {
    "string": "valueString",
    "number": "valueNumber",
    "integer": "valueInteger",
    "boolean": "valueBoolean",
    "date": "valueDate",
    "time": "valueTime",
    "json": "valueJson",
}
_EVIDENCE_KINDS = {
    "extract": EvidenceKind.EXTRACTED_FIELD,
    "classify": EvidenceKind.CLASSIFIED_LABEL,
    "generate": EvidenceKind.GENERATED_DESCRIPTION,
}
_VOLATILE = frozenset({"createdAt", "lastModifiedAt", "status", "warnings", "tags"})

MIME_TYPES: dict[FileKind, str] = {
    FileKind.PDF: "application/pdf",
    FileKind.DOCX: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    FileKind.PPTX: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    FileKind.HTML: "text/html",
    FileKind.PNG: "image/png",
    FileKind.JPEG: "image/jpeg",
    FileKind.TIFF: "image/tiff",
    FileKind.BMP: "image/bmp",
    FileKind.HEIF: "image/heif",
}


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class AnalyzerProfile(_Frozen):
    """Resolved analyzer identity; part of every CU extraction fingerprint."""

    analyzer_id: str
    definition_sha256: Sha256Hex
    prebuilt: bool = False
    completion_model: str | None = None
    embedding_model: str | None = None
    deployments: dict[str, str] = Field(default_factory=dict)
    model_versions: dict[str, str] = Field(default_factory=dict)
    field_methods: dict[str, str] = Field(default_factory=dict)

    def identity(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"field_methods"})


def definition_digest(definition: Mapping[str, Any]) -> str:
    return sha256_hex(canonical_json(definition))


def service_definition(document: Mapping[str, Any]) -> dict[str, Any]:
    """An analyzer as returned by the service, without timestamps, status or tags."""

    return {key: value for key, value in document.items() if key not in _VOLATILE}


def subset_mismatches(expected: Any, actual: Any, path: str = "$") -> list[str]:
    """Paths where ``actual`` differs from ``expected``; extra keys in ``actual`` are allowed."""

    if isinstance(expected, Mapping):
        if not isinstance(actual, Mapping):
            return [path]
        mismatches: list[str] = []
        for key, value in expected.items():
            if key not in actual:
                mismatches.append(f"{path}.{key}")
            else:
                mismatches.extend(subset_mismatches(value, actual[key], f"{path}.{key}"))
        return mismatches
    return [] if expected == actual else [path]


def field_methods(definition: Mapping[str, Any]) -> dict[str, str]:
    # The service picks a method when none is set; treat that as generated, never authoritative.
    fields = (definition.get("fieldSchema") or {}).get("fields") or {}
    return {name: str(spec.get("method") or "generate") for name, spec in fields.items()}


@dataclass(frozen=True)
class AnalyzerRegistry:
    """Expected analyzers and model mappings, loaded from the reviewed infra definition file."""

    api_version: str
    deployments: dict[str, str]
    model_versions: dict[str, str]
    definitions: dict[str, dict[str, Any]]
    prebuilt: tuple[str, ...] = ()
    profiles: dict[str, AnalyzerProfile] = field(default_factory=dict)

    @classmethod
    def load(cls, document: Mapping[str, Any]) -> AnalyzerRegistry:
        deployments = dict(document.get("modelDeployments") or {})
        versions = dict(document.get("modelVersions") or {})
        definitions = {
            str(name): dict(spec) for name, spec in (document.get("analyzers") or {}).items()
        }
        registry = cls(
            api_version=str(document.get("apiVersion") or API_VERSION),
            deployments=deployments,
            model_versions=versions,
            definitions=definitions,
            prebuilt=tuple(document.get("prebuiltAnalyzers") or ()),
        )
        for name, definition in definitions.items():
            registry.profiles[name] = registry._profile(name, definition, prebuilt=False)
        return registry

    def _profile(
        self, name: str, definition: Mapping[str, Any], *, prebuilt: bool
    ) -> AnalyzerProfile:
        models = definition.get("models") or {}
        completion = models.get("completion")
        embedding = models.get("embedding")
        used = {model: self.deployments[model] for model in (completion, embedding) if model}
        if len(used) != len([model for model in (completion, embedding) if model]):
            raise ConfigurationError(f"Analyzer {name} uses a model without a deployment mapping")
        return AnalyzerProfile(
            analyzer_id=name,
            definition_sha256=definition_digest(definition),
            prebuilt=prebuilt,
            completion_model=completion,
            embedding_model=embedding,
            deployments=used,
            model_versions={
                deployment: self.model_versions[deployment]
                for deployment in used.values()
                if deployment in self.model_versions
            },
            field_methods=field_methods(definition),
        )

    def add_prebuilt(self, name: str, document: Mapping[str, Any]) -> None:
        """Register a prebuilt analyzer from its service definition (verified at startup)."""

        if name not in self.prebuilt:
            raise ConfigurationError(f"Prebuilt analyzer {name} is not in the reviewed registry")
        definition = service_definition(document)
        models = dict(definition.get("models") or {})
        aliases = {
            role: self.deployments.get(model, self.deployments.get(f"prebuilt-analyzer-{role}"))
            for role, model in models.items()
        }
        self.profiles[name] = AnalyzerProfile(
            analyzer_id=name,
            definition_sha256=definition_digest(definition),
            prebuilt=True,
            completion_model=models.get("completion"),
            embedding_model=models.get("embedding"),
            deployments={role: value for role, value in aliases.items() if value},
            model_versions={
                value: self.model_versions[value]
                for value in aliases.values()
                if value and value in self.model_versions
            },
            field_methods=field_methods(definition),
        )

    def profile(self, analyzer_id: str) -> AnalyzerProfile:
        try:
            return self.profiles[analyzer_id]
        except KeyError as error:
            raise ConfigurationError(
                f"Analyzer {analyzer_id} is not registered or failed verification"
            ) from error


def parse_source(source: str | None) -> list[tuple[int, tuple[float, ...]]]:
    """Decode ``D(page,x1,y1,...,x4,y4)`` or ``D(page,left,top,width,height)``; ``;`` joins."""

    regions: list[tuple[int, tuple[float, ...]]] = []
    for match in _SOURCE.finditer(source or ""):
        numbers = tuple(float(value) for value in match.group(2).split(",") if value.strip())
        if len(numbers) == 4:
            left, top, width, height = numbers
            numbers = (
                left, top, left + width, top, left + width, top + height, left, top + height
            )
        if len(numbers) < 6 or len(numbers) % 2:
            numbers = ()
        regions.append((int(match.group(1)), numbers))
    return regions


def span_text(markdown: str, spans: Any) -> str | None:
    """Printed text of a field: its spans sliced from the Markdown (code point offsets)."""

    parts: list[str] = []
    for span in spans or ():
        offset, length = int(span.get("offset", 0)), int(span.get("length", 0))
        if offset >= 0 and offset + length <= len(markdown) and length:
            parts.append(markdown[offset : offset + length])
    return " ".join(part.strip() for part in parts) or None


def flatten_fields(
    fields: Mapping[str, Any],
) -> Iterator[tuple[str, str, Mapping[str, Any]]]:
    """Yield ``(top-level name, path, leaf field)`` for every leaf of nested arrays/objects."""

    for name, value in fields.items():
        yield from _walk(name, name, value)


def _walk(top: str, path: str, value: Any) -> Iterator[tuple[str, str, Mapping[str, Any]]]:
    if not isinstance(value, Mapping):
        return
    kind = value.get("type")
    if kind == "array":
        for index, item in enumerate(value.get("valueArray") or []):
            yield from _walk(top, f"{path}[{index}]", item)
    elif kind == "object":
        for key, item in (value.get("valueObject") or {}).items():
            yield from _walk(top, f"{path}.{key}", item)
    else:
        yield top, path, value


def leaf_value(value: Mapping[str, Any]) -> Any:
    key = _VALUE_KEYS.get(str(value.get("type")))
    return value.get(key) if key else None


def evidence_kind(method: str) -> EvidenceKind:
    return _EVIDENCE_KINDS.get(method, EvidenceKind.GENERATED_DESCRIPTION)


def usage_from(usage: Mapping[str, Any] | None, *, requests: int) -> ProviderUsage:
    """Map the operation ``usage`` object; pages sum every content-extraction meter."""

    usage = usage or {}
    meters = {
        key: int(value or 0) for key, value in usage.items() if key.startswith("documentPages")
    }
    tokens = {str(key): int(value or 0) for key, value in (usage.get("tokens") or {}).items()}
    return ProviderUsage(
        provider=PROVIDER,
        pages=sum(meters.values()),
        input_tokens=sum(value for key, value in tokens.items() if not key.endswith("-output")),
        output_tokens=sum(value for key, value in tokens.items() if key.endswith("-output")),
        requests=requests,
        details={
            "meters": meters,
            "contextualization_tokens": int(usage.get("contextualizationTokens") or 0),
            "tokens": tokens,
        },
    )
