"""Build one KG-1 run from ingested documents: placements, file names, cover fields and codes,
then lineage across documents. Deterministic: the same inputs give the same run.

Statuses: facts read directly from a path, file name, cover or printed code are ``observed``;
anything resolved through the provisional gazetteer, and heuristic copies, are ``candidate``.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date
from itertools import pairwise
from pathlib import PurePosixPath
from typing import Any

from ..crv import research_context
from ..ids import fingerprint, sha256_hex
from .codes import ReportCode, codes_in, parse_code
from .cover import CoverFacts, CoverLine, CoverValue, parse_cover
from .filenames import FileNameFacts, parse_file_name
from .gazetteer import Gazetteer, GazetteerEntry
from .model import Assertion, Entity, Evidence, KgRun, assertion_iri, evidence_iri
from .vocab import (
    CODE_TYPE_GENRES,
    COLLECTION_SCOPES,
    DOCUMENT_TYPE_GENRES,
    EXCLUDED_COLLECTIONS,
    GAZETTEER_SCOPE,
    STATUS_RANK,
    TOOL_VERSION,
    fold,
    slug,
)

MIN_YEAR = 1950
MAX_PATHS_IN_PROPERTIES = 5
_GENERIC_FAMILY_KEYS = frozenset({"readme", "document", "rapport", "presentation"})
_METHOD_RANK = {"cover": 5, "header_code": 4, "printed_code": 3, "filename": 2, "path": 1,
                "title": 1, "revision": 2, "gazetteer": 0}


@dataclass(frozen=True)
class Placement:
    source_path: str
    original_hash: str | None = None
    converter: str | None = None
    archive_members: tuple[str, ...] = ()


@dataclass(frozen=True)
class DocumentInput:
    content_hash: str
    evidence_id: str
    page_count: int | None
    placements: tuple[Placement, ...]
    header_codes: tuple[str, ...] = ()
    cover_lines: tuple[CoverLine, ...] = ()

    def payload(self) -> dict[str, Any]:
        return {
            "content_hash": self.content_hash,
            "evidence_id": self.evidence_id,
            "page_count": self.page_count,
            "placements": sorted(
                [p.source_path, p.original_hash, p.converter, list(p.archive_members)]
                for p in self.placements
            ),
            "header_codes": list(self.header_codes),
            "cover": [[line.text, line.page, line.evidence_id] for line in self.cover_lines],
        }


@dataclass
class _EntityAcc:
    kind: str
    label: str
    status: str
    attributes: dict[str, Any] = field(default_factory=dict)
    identifiers: set[tuple[str, str]] = field(default_factory=set)
    evidence: set[str] = field(default_factory=set)


@dataclass
class _AssertionAcc:
    subject: str
    predicate: str
    object: str
    method: str
    status: str
    evidence: set[str] = field(default_factory=set)


@dataclass
class _Doc:
    iri: str
    source: DocumentInput
    primary: Placement
    names: FileNameFacts
    path_evidence: str
    scope: str
    collection: str


class _Builder:
    def __init__(self, gazetteer: Gazetteer) -> None:
        self.gazetteer = gazetteer
        self.entities: dict[str, _EntityAcc] = {}
        self.assertions: dict[str, _AssertionAcc] = {}
        self.evidence: dict[str, Evidence] = {}

    def add_evidence(self, item: Evidence) -> str:
        self.evidence.setdefault(item.iri, item)
        return item.iri

    def entity(
        self,
        iri: str,
        kind: str,
        label: str,
        status: str,
        *,
        attributes: Mapping[str, Any] | None = None,
        identifiers: Iterable[tuple[str, str]] = (),
        evidence: Iterable[str] = (),
    ) -> str:
        acc = self.entities.get(iri)
        if acc is None:
            acc = self.entities[iri] = _EntityAcc(kind, label, status)
        elif STATUS_RANK[status] > STATUS_RANK[acc.status]:
            acc.status = status
        for key, value in (attributes or {}).items():
            if value not in (None, "", [], ()):
                acc.attributes.setdefault(key, value)
        acc.identifiers.update((scheme, value) for scheme, value in identifiers if value)
        acc.evidence.update(evidence)
        return iri

    def gazetteer_entity(self, entry: GazetteerEntry) -> str:
        source = self.add_evidence(
            Evidence(
                evidence_iri("gazetteer", self.gazetteer.input_hash, entry.iri),
                self.gazetteer.input_hash,
                f"crv-gazetteer.json@{self.gazetteer.version}",
                "gazetteer",
                {"iri": entry.iri, "version": self.gazetteer.version},
                GAZETTEER_SCOPE,
            )
        )
        if entry.iri not in self.entities and entry.operated_by:
            operator = self.gazetteer.by_iri()[entry.operated_by]
            self.gazetteer_entity(operator)
        self.entity(
            entry.iri,
            entry.kind,
            entry.label,
            entry.status,
            attributes={
                "subtype": entry.subtype,
                "keys": list(entry.keys),
                "report_code_segment3": list(entry.codes),
                "note": entry.note,
            },
            identifiers=[("alias", alias) for alias in (entry.label, *entry.aliases)]
            + [("code_s3", code) for code in entry.codes],
            evidence=[source],
        )
        if entry.operated_by:
            self.relate(entry.iri, "operatedBy", entry.operated_by, "gazetteer", "candidate",
                        [source])
        return source

    def relate(
        self,
        subject: str,
        predicate: str,
        target: str,
        method: str,
        status: str,
        evidence: Iterable[str],
    ) -> None:
        iri = assertion_iri(subject, predicate, target)
        items = set(evidence)
        acc = self.assertions.get(iri)
        if acc is None:
            self.assertions[iri] = _AssertionAcc(subject, predicate, target, method, status, items)
        else:
            if _METHOD_RANK[method] > _METHOD_RANK[acc.method]:
                acc.method = method
            if STATUS_RANK[status] > STATUS_RANK[acc.status]:
                acc.status = status
            acc.evidence |= items
        # Shared vertices (sites, codes, folders...) become visible where they are used.
        if self.entities[target].kind != "Document":
            self.entities[target].evidence |= items

    def run(self, input_hash: str, config: Mapping[str, Any]) -> KgRun:
        return KgRun(
            input_hash,
            tuple(
                Entity(iri, acc.kind, acc.label, acc.status, dict(acc.attributes),
                       tuple(sorted(acc.identifiers)), tuple(sorted(acc.evidence)))
                for iri, acc in sorted(self.entities.items())
            ),
            tuple(
                Assertion(iri, acc.subject, acc.predicate, acc.object, acc.method, acc.status,
                          tuple(sorted(acc.evidence)))
                for iri, acc in sorted(self.assertions.items())
            ),
            tuple(self.evidence[iri] for iri in sorted(self.evidence)),
            config,
        )


def document_iri(content_hash: str) -> str:
    return f"urn:crv:doc:{content_hash[:16]}"


def _folders(path: str) -> list[str]:
    return list(PurePosixPath(path.replace("\\", "/")).parts[:-1])


def _year_ok(year: int | None, today: date) -> bool:
    return year is not None and MIN_YEAR <= year <= today.year


def build_run(
    documents: Iterable[DocumentInput], gazetteer: Gazetteer, *, today: date
) -> tuple[KgRun, dict[str, Any]]:
    """The run and its coverage report (design §8 metrics, per collection)."""

    docs = sorted(documents, key=lambda item: item.content_hash)
    payload = {
        "documents": [doc.payload() for doc in docs],
        "gazetteer": gazetteer.input_hash,
        "year": today.year,
    }
    input_hash = fingerprint("crv-kg-inputs", payload)
    builder = _Builder(gazetteer)
    stats: dict[str, Counter[str]] = defaultdict(Counter)
    totals: Counter[str] = Counter()
    built: list[_Doc] = []
    for source in docs:
        doc = _document(builder, source, today, stats, totals)
        if doc is not None:
            built.append(doc)
    _lineage(builder, built, totals)
    with_site = {acc.subject for acc in builder.assertions.values()
                 if acc.predicate == "aboutSite"}
    for doc in built:
        stats[doc.collection]["site"] += doc.iri in with_site
    run = builder.run(
        input_hash,
        {"tool_version": TOOL_VERSION, "gazetteer": gazetteer.version, "documents": len(docs)},
    )
    problems = run.validate()
    report = {
        "run_id": run.run_id,
        "input_hash": input_hash,
        "gazetteer": gazetteer.version,
        "documents_in": len(docs),
        "documents_in_graph": len(built),
        "entities": dict(Counter(entity.kind for entity in run.entities)),
        "assertions": dict(Counter(assertion.predicate for assertion in run.assertions)),
        "assertion_status": dict(Counter(assertion.status for assertion in run.assertions)),
        "evidence": len(run.evidence),
        "totals": dict(totals),
        "coverage": {
            collection: {
                **dict(counts),
                "genre_and_date_pct": round(
                    100 * counts["genre_and_date"] / counts["documents"], 1
                ) if counts["documents"] else 0.0,
            }
            for collection, counts in sorted(stats.items())
        },
        "validation_problems": problems[:50],
        "validation_problem_count": len(problems),
    }
    in_graph = sum(c["documents"] for c in stats.values())
    both = sum(c["genre_and_date"] for c in stats.values())
    report["genre_and_date_pct"] = round(100 * both / in_graph, 1) if in_graph else 0.0
    return run, report


def _document(
    builder: _Builder,
    source: DocumentInput,
    today: date,
    stats: dict[str, Counter[str]],
    totals: Counter[str],
) -> _Doc | None:
    contexts = {p.source_path: research_context(p.source_path) for p in source.placements}
    kept = [p for p in source.placements
            if contexts[p.source_path].collection not in EXCLUDED_COLLECTIONS]
    if not kept:
        totals["excluded_documents"] += 1
        return None
    named = {p.source_path: parse_file_name(p.source_path) for p in kept}
    primary = min(kept, key=lambda p: (named[p.source_path].copy, len(p.source_path),
                                        p.source_path))
    context = contexts[primary.source_path]
    names = named[primary.source_path]
    scope = COLLECTION_SCOPES[context.collection]
    iri = document_iri(source.content_hash)
    counts = stats[context.collection]
    counts["documents"] += 1

    path_evidence: dict[str, str] = {}
    for placement in kept:
        path_evidence[placement.source_path] = builder.add_evidence(
            Evidence(
                evidence_iri("path", source.content_hash, placement.source_path),
                source.content_hash,
                named[placement.source_path].file_name,
                "file_path",
                {"source_path": placement.source_path,
                 "archive_members": list(placement.archive_members)},
                COLLECTION_SCOPES[contexts[placement.source_path].collection],
            )
        )
    primary_evidence = path_evidence[primary.source_path]

    def span(value: CoverValue, name: str) -> str:
        return builder.add_evidence(
            Evidence(
                evidence_iri("cover", source.content_hash, name, value.evidence_id),
                source.content_hash,
                names.file_name,
                "text_span",
                {"field": name, "page": value.page, "evidence_id": value.evidence_id,
                 "evidence_document": source.evidence_id},
                scope,
            )
        )

    cover = parse_cover(source.cover_lines)
    cover_evidence = {name: span(value, name) for name, value in cover.fields.items()}

    # Report codes: page header (document identity), printed on the cover, in the file name.
    codes: dict[str, tuple[ReportCode, str, str]] = {}
    if source.header_codes:
        header = builder.add_evidence(
            Evidence(
                evidence_iri("header", source.content_hash, *source.header_codes),
                source.content_hash, names.file_name, "page_header",
                {"codes": list(source.header_codes), "evidence_document": source.evidence_id},
                scope,
            )
        )
        for raw in source.header_codes:
            if parsed := parse_code(raw):
                codes.setdefault(parsed.code, (parsed, "header_code", header))
    for line in source.cover_lines:
        for parsed in codes_in(line.text):
            if parsed.code not in codes:
                value = CoverValue(parsed.code, line.page, line.evidence_id)
                codes[parsed.code] = (parsed, "printed_code", span(value, "report_code"))
    stem = PurePosixPath(names.file_name).stem
    for parsed in codes_in(stem.replace("_", "-")):
        codes.setdefault(parsed.code, (parsed, "filename", primary_evidence))

    year, month, day, date_basis = _date(names, cover, codes, today)
    genre, genre_basis = _genre(names, codes, context.document_type)
    title_value = cover.fields.get("title")
    attributes = {
        "title": title_value.value if title_value else names.title,
        "title_basis": "cover" if title_value else "filename",
        "file_title": names.title,
        "genre": genre,
        "genre_basis": genre_basis,
        "date": _iso(year, month, day),
        "date_basis": date_basis,
        "year": year,
        "revision": names.revision or (cover.revision.value if cover.revision else None),
        "collection": context.collection,
        "collections": sorted({contexts[p.source_path].collection for p in kept}),
        "document_type": context.document_type,
        "page_count": source.page_count,
        "source_paths": sorted(path_evidence)[:MAX_PATHS_IN_PROPERTIES],
        "placement_count": len(kept),
        "converted_from": sorted({p.original_hash for p in kept
                                  if p.converter and p.original_hash}),
        "issuer": cover.fields["issuer"].value if "issuer" in cover.fields else None,
        "project": cover.fields["project"].value if "project" in cover.fields else None,
        "subject": cover.fields["subject"].value if "subject" in cover.fields else None,
        "scope_text": cover.fields["scope"].value if "scope" in cover.fields else None,
        "keywords": cover.fields["keywords"].value if "keywords" in cover.fields else None,
        "report_codes": sorted(codes),
        "content_hash": source.content_hash,
        "evidence_document": source.evidence_id,
    }
    builder.entity(
        iri, "Document", str(attributes["title"])[:300], "observed",
        attributes=attributes,
        identifiers=[("content_hash", source.content_hash), ("file_name", names.file_name)]
        + [("source_path", path) for path in path_evidence]
        + [("report_code", code) for code in codes],
        evidence=[*path_evidence.values(), *cover_evidence.values()],
    )
    counts["genre"] += genre is not None
    counts["date"] += year is not None
    counts["genre_and_date"] += genre is not None and year is not None
    counts[f"date_basis:{date_basis}"] += 1
    counts[f"genre_basis:{genre_basis}"] += 1
    counts["cover"] += bool(cover.fields)
    counts["code"] += bool(codes)

    for placement in kept:
        evidence = [path_evidence[placement.source_path]]
        collection = contexts[placement.source_path].collection
        builder.entity(f"urn:crv:collection:{collection}", "Collection", collection, "observed",
                       evidence=evidence)
        builder.relate(iri, "inCollection", f"urn:crv:collection:{collection}", "path",
                       "observed", evidence)
        folder_path = "/".join(_folders(placement.source_path))
        if folder_path:
            folder = builder.entity(
                f"urn:crv:folder:{sha256_hex(fold(folder_path))[:16]}", "Folder",
                _folders(placement.source_path)[-1], "observed",
                attributes={"path": folder_path}, identifiers=[("folder", folder_path)],
                evidence=evidence,
            )
            builder.relate(iri, "filedUnder", folder, "path", "observed", evidence)
        for entry in builder.gazetteer.for_folders(_folders(placement.source_path)):
            reference = builder.gazetteer_entity(entry)
            predicate = "aboutSite" if entry.kind == "Site" else "concerns"
            builder.relate(iri, predicate, entry.iri, "path", "candidate", [*evidence, reference])

    for code, (parsed, method, code_evidence) in sorted(codes.items()):
        code_iri = builder.entity(
            f"urn:crv:code:{slug(code)}", "ReportCode", code, "observed",
            attributes={"segments": list(parsed.segments), "s2": parsed.s2, "s3": parsed.s3,
                        "doc_type": parsed.doc_type, "seq": parsed.seq, "year": parsed.year,
                        "keys": [fold(code).replace(" ", "")]},
            identifiers=[("report_code", code)], evidence=[code_evidence],
        )
        builder.relate(iri, "hasCode", code_iri, method, "observed", [code_evidence])
        if coded := builder.gazetteer.for_code(parsed.s3):
            reference = builder.gazetteer_entity(coded)
            predicate = "aboutSite" if coded.kind == "Site" else "concerns"
            builder.relate(iri, predicate, coded.iri, method, "candidate",
                           [code_evidence, reference])
        if year and parsed.year and abs(parsed.year - year) > 1:
            totals["code_year_mismatch"] += 1

    title_evidence = cover_evidence.get("title", primary_evidence)
    for entry in builder.gazetteer.mentioned(str(attributes["title"]), kind="Site"):
        reference = builder.gazetteer_entity(entry)
        builder.relate(iri, "aboutSite", entry.iri, "title", "candidate",
                       [title_evidence, reference])
    if "issuer" in cover.fields:
        for entry in builder.gazetteer.mentioned(cover.fields["issuer"].value,
                                                 kind="Organisation"):
            reference = builder.gazetteer_entity(entry)
            builder.relate(iri, "issuedBy", entry.iri, "cover", "candidate",
                           [cover_evidence["issuer"], reference])
    if cover.attributed_to is not None:
        attributed = span(cover.attributed_to, "attributed_to")
        for entry in builder.gazetteer.mentioned(cover.attributed_to.value, kind="Organisation"):
            reference = builder.gazetteer_entity(entry)
            builder.relate(iri, "attributedTo", entry.iri, "cover", "candidate",
                           [attributed, reference])
    if "project" in cover.fields:
        printed = cover.fields["project"].value
        project = builder.entity(
            f"urn:crv:project:{slug(printed, limit=60)}-{sha256_hex(fold(printed))[:8]}",
            "Project", printed[:200], "observed",
            attributes={"keys": [fold(printed)]}, identifiers=[("name", printed)],
            evidence=[cover_evidence["project"]],
        )
        builder.relate(iri, "aboutProject", project, "cover", "observed",
                       [cover_evidence["project"]])
        counts["project"] += 1
    counts["issuer"] += "issuer" in cover.fields
    return _Doc(iri, source, primary, names, primary_evidence, scope, context.collection)


def _date(
    names: FileNameFacts,
    cover: CoverFacts,
    codes: Mapping[str, tuple[ReportCode, str, str]],
    today: date,
) -> tuple[int | None, int | None, int | None, str | None]:
    if _year_ok(names.year, today) and names.date_kind in (
        "month_year", "month_name", "day_month_yy"
    ):
        return names.year, names.month, None, f"filename_{names.date_kind}"
    latest = cover.latest_date
    if latest is not None and _year_ok(latest[0].year, today):
        found = latest[0]
        return found.year, found.month, found.day, "cover"
    if cover.period is not None and _year_ok(cover.period[0], today):
        return cover.period[0], cover.period[1], None, "cover_period"
    if names.revision_date is not None and _year_ok(names.revision_date.year, today):
        found = names.revision_date
        return found.year, found.month, found.day, "revision_suffix"
    if _year_ok(names.year, today):
        return names.year, None, None, f"filename_{names.date_kind}"
    for _code, (parsed, _method, _evidence) in sorted(codes.items()):
        if _year_ok(parsed.year, today):
            return parsed.year, None, None, "report_code"
    return None, None, None, None


def _genre(
    names: FileNameFacts,
    codes: Mapping[str, tuple[ReportCode, str, str]],
    document_type: str,
) -> tuple[str | None, str | None]:
    if names.genre:
        return names.genre, "filename"
    for _code, (parsed, _method, _evidence) in sorted(codes.items()):
        if parsed.doc_type is not None and parsed.doc_type in CODE_TYPE_GENRES:
            return CODE_TYPE_GENRES[parsed.doc_type], "report_code"
    if document_type in DOCUMENT_TYPE_GENRES:
        return DOCUMENT_TYPE_GENRES[document_type], "path"
    return None, None


def _iso(year: int | None, month: int | None, day: int | None) -> str | None:
    if year is None:
        return None
    if month is None:
        return f"{year:04d}"
    return f"{year:04d}-{month:02d}" + (f"-{day:02d}" if day else "")


def _lineage(builder: _Builder, docs: list[_Doc], totals: Counter[str]) -> None:
    families: dict[str, list[_Doc]] = defaultdict(list)
    for doc in docs:
        key = doc.names.family_key
        if len(key.split()) >= 2 and key not in _GENERIC_FAMILY_KEYS:
            # Versions live together: chapters of two EIE studies share titles, not lineage.
            folder = fold("/".join(_folders(doc.primary.source_path)))
            families[f"{folder}|{key}"].append(doc)
    for key, members in sorted(families.items()):
        if len(members) < 2:
            continue
        family = builder.entity(
            f"urn:crv:family:{sha256_hex(key)[:16]}", "VersionFamily",
            min(members, key=lambda d: d.primary.source_path).names.title[:200], "observed",
            attributes={"family_key": key}, evidence=[d.path_evidence for d in members],
        )
        for doc in members:
            builder.relate(doc.iri, "memberOf", family, "title", "observed", [doc.path_evidence])
        totals["families"] += 1
        ranked = sorted(
            (doc for doc in members if doc.names.revision_rank is not None),
            key=lambda d: (d.names.revision_rank, d.names.revision_date or date.min,
                           d.primary.source_path),
        )
        for older, newer in pairwise(ranked):
            if (newer.names.revision_rank, newer.names.revision_date) == (
                older.names.revision_rank, older.names.revision_date
            ):
                continue
            builder.relate(newer.iri, "supersedes", older.iri, "revision", "observed",
                           [newer.path_evidence, older.path_evidence])
            totals["supersedes"] += 1
        canonical = max(
            members,
            key=lambda d: (d.names.revision_rank if d.names.revision_rank is not None else -1,
                           not d.names.copy, -len(d.primary.source_path)),
        )
        for doc in members:
            older_revision = (
                doc.names.revision_rank is not None
                and canonical.names.revision_rank is not None
                and doc.names.revision_rank != canonical.names.revision_rank
            )
            if doc is canonical or older_revision:
                continue
            builder.relate(doc.iri, "duplicateOf", canonical.iri, "title", "candidate",
                           [doc.path_evidence, canonical.path_evidence])
            totals["duplicates"] += 1
    for doc in docs:
        if doc.names.part is None:
            continue
        folder_path = "/".join(_folders(doc.primary.source_path))
        if not folder_path:
            continue
        folder = f"urn:crv:folder:{sha256_hex(fold(folder_path))[:16]}"
        builder.relate(doc.iri, "partOf", folder, "filename", "observed", [doc.path_evidence])
        totals["part_of"] += 1
