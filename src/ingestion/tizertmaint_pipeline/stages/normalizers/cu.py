"""Normalize a Content Understanding analyze result into canonical evidence.

Each document content is mapped onto the Document Intelligence layout shape and normalized by
the same code, so tables, sections and figure folding behave identically. CU additions are kept
as their own evidence kinds: field observations (with the printed text recovered from spans) and
generated descriptions, which are never mixed into source text.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ...domain.cu import (
    CU_MARKDOWN,
    evidence_kind,
    flatten_fields,
    leaf_value,
    parse_source,
    span_text,
)
from ...domain.errors import IntegrityError
from ...domain.evidence import (
    BlockKind,
    EvidenceBlock,
    EvidenceFigure,
    EvidenceKind,
    EvidencePage,
    EvidenceUnit,
    FieldObservation,
    Locator,
    LocatorKind,
    block_id,
    detect_language,
    normalize_text,
)
from ...domain.models import ArtifactRef, UnitRecord
from .di_layout import layout_blocks, resolve_page_map

NORMALIZER = "cu-document@1"


def to_layout(content: Mapping[str, Any]) -> dict[str, Any]:
    """Map one CU document content onto the DI layout shape (regions, spans, page units)."""

    def element(item: Mapping[str, Any]) -> dict[str, Any]:
        converted = dict(item)
        converted["boundingRegions"] = [
            {"pageNumber": page, "polygon": list(polygon)}
            for page, polygon in parse_source(item.get("source"))
        ]
        span = item.get("span")
        converted["spans"] = [span] if span else list(item.get("spans") or [])
        return converted

    tables = []
    for table in content.get("tables") or []:
        converted = element(table)
        converted["cells"] = [element(cell) for cell in table.get("cells") or []]
        tables.append(converted)
    unit = content.get("unit")
    return {
        "pages": [{**page, "unit": unit} for page in content.get("pages") or []],
        "paragraphs": [element(item) for item in content.get("paragraphs") or []],
        "tables": tables,
        "figures": [element(item) for item in content.get("figures") or []],
        "sections": list(content.get("sections") or []),
    }


class _Locate:
    def __init__(
        self,
        content: Mapping[str, Any],
        *,
        mapping: dict[int, int],
        raw: ArtifactRef,
        representation: str,
        encoding: str,
    ) -> None:
        self.unit = content.get("unit")
        self.sizes = {
            int(page["pageNumber"]): page for page in content.get("pages") or []
        }
        self.mapping = mapping
        self.raw = raw
        self.representation = representation
        self.encoding = encoding

    def __call__(self, item: Mapping[str, Any]) -> tuple[tuple[int, ...], tuple[Locator, ...]]:
        pages: list[int] = []
        locators: list[Locator] = []
        for page, polygon in parse_source(item.get("source")):
            original = self.mapping.get(page)
            if original is None:
                raise IntegrityError(f"CU page {page} has no original page")
            pages.append(original)
            info = self.sizes.get(page, {})
            if polygon and self.unit:
                locators.append(
                    Locator(
                        kind=LocatorKind.REGION,
                        page=original,
                        polygon=polygon,
                        unit=self.unit,
                        page_width=info.get("width"),
                        page_height=info.get("height"),
                    )
                )
            else:
                locators.append(Locator(kind=LocatorKind.PAGE, page=original))
        spans = list(item.get("spans") or []) or ([item["span"]] if item.get("span") else [])
        locators.extend(
            Locator(
                kind=LocatorKind.SPAN,
                artifact_sha256=self.raw.sha256,
                representation=self.representation,
                index_type=self.encoding,
                offset=int(span.get("offset", 0)),
                length=int(span.get("length", 0)),
            )
            for span in spans
        )
        return tuple(sorted(set(pages))), tuple(locators)


def _offset(item: Mapping[str, Any], default: int) -> int:
    span = item.get("span") or {}
    return int(span.get("offset", default))


def normalize_analysis(
    document: Mapping[str, Any],
    *,
    unit: UnitRecord,
    plan_id: str,
    content_hash: str,
    raw: ArtifactRef,
    page_map: Mapping[int, int] | None,
    request: Mapping[str, Any],
    profile: Mapping[str, Any],
    field_methods: Mapping[str, str],
) -> EvidenceUnit:
    """``document`` is the full GET response (``status``, ``result`` and ``usage``)."""

    result = document.get("result")
    if not isinstance(result, Mapping):
        raise IntegrityError("CU response has no result")
    analyzer_id = str(result.get("analyzerId") or request.get("analyzer_id"))
    encoding = str(result.get("stringEncoding") or request.get("string_encoding") or "codePoint")
    ordered: list[tuple[tuple[int, int, int], EvidenceBlock]] = []
    pages: dict[int, EvidencePage] = {}
    observations: list[FieldObservation] = []
    for index, content in enumerate(result.get("contents") or []):
        if content.get("kind", "document") != "document":
            continue
        layout = to_layout(content)
        cu_pages = [int(page["pageNumber"]) for page in layout["pages"]]
        mapping = resolve_page_map(cu_pages, unit.pages, page_map)
        representation = CU_MARKDOWN if index == 0 else f"{CU_MARKDOWN}[{index}]"
        laid_out, content_pages = layout_blocks(
            layout, raw=raw, mapping=mapping, representation=representation, index_type=encoding
        )
        ordered.extend(((index, order, 0), block) for order, block in laid_out)
        for page in content_pages:
            pages.setdefault(page.page, page)
        locate = _Locate(
            content, mapping=mapping, raw=raw, representation=representation, encoding=encoding
        )
        markdown = str(content.get("markdown") or "")
        for position, figure in enumerate(content.get("figures") or []):
            description = normalize_text(str(figure.get("description") or ""))
            if not description:
                continue
            figure_pages, locators = locate(figure)
            caption = (figure.get("caption") or {}).get("content")
            ordered.append(
                (
                    (index, _offset(figure, len(markdown) + position), 1),
                    EvidenceBlock(
                        evidence_id="pending",
                        kind=BlockKind.FIGURE,
                        text=description,
                        pages=figure_pages,
                        locators=locators,
                        figure=EvidenceFigure(
                            figure_id=str(figure.get("id", position)),
                            caption=normalize_text(caption) if caption else None,
                        ),
                        evidence_kind=EvidenceKind.GENERATED_DESCRIPTION,
                    ),
                )
            )
        tail = len(markdown) + len(content.get("figures") or [])
        for position, (top, path, leaf) in enumerate(flatten_fields(content.get("fields") or {})):
            method = field_methods.get(top, "generate")
            leaf_pages, locators = locate(leaf)
            value = leaf_value(leaf)
            observations.append(
                FieldObservation(
                    path=path,
                    field=top,
                    method=method,
                    evidence_kind=evidence_kind(method),
                    value_type=str(leaf.get("type")),
                    value=value,
                    raw_text=span_text(markdown, leaf.get("spans")),
                    confidence=leaf.get("confidence"),
                    pages=leaf_pages,
                    locators=locators,
                    analyzer_id=analyzer_id,
                )
            )
            if method == "generate" and isinstance(value, str) and normalize_text(value):
                ordered.append(
                    (
                        (index, tail + position, 2),
                        EvidenceBlock(
                            evidence_id="pending",
                            kind=BlockKind.PARAGRAPH,
                            text=normalize_text(value),
                            role=f"field:{path}",
                            pages=leaf_pages,
                            locators=locators,
                            evidence_kind=EvidenceKind.GENERATED_DESCRIPTION,
                        ),
                    )
                )
    blocks = tuple(
        block.model_copy(update={"evidence_id": block_id(unit.unit_id, raw.sha256, position)})
        for position, (_, block) in enumerate(sorted(ordered, key=lambda item: item[0]))
    )
    text = "\n".join(
        block.text
        for block in blocks
        if block.text and block.evidence_kind is EvidenceKind.SOURCE_TEXT
    )
    return EvidenceUnit(
        unit_id=unit.unit_id,
        plan_id=plan_id,
        content_hash=content_hash,
        route=unit.route,
        extraction_method="cu_analyze",
        normalizer=NORMALIZER,
        provider={
            "api_version": result.get("apiVersion"),
            "analyzer_id": analyzer_id,
            "string_encoding": encoding,
            "profile": dict(profile),
            "request": dict(request),
            "unit_kind": unit.kind,
            "replaces": unit.locator.get("replaces"),
        },
        raw_artifact=raw,
        pages=tuple(page for _, page in sorted(pages.items())),
        blocks=blocks,
        language=detect_language(text),
        warnings=tuple(
            str(warning.get("code", "warning")) for warning in result.get("warnings") or []
        ),
        fields=tuple(observations),
    )
