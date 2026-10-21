"""Provisional gazetteer (KG-0 seed): sites and organisations, their aliases, the folders that
file documents about them, and report-code segment-3 values. Every entry stays ``candidate``
until a domain expert approves it (Docs/KNOWLEDGE_GRAPH_DESIGN.md §1.3, §10).
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from ..errors import InvalidInputError
from ..ids import sha256_hex
from .vocab import fold

GAZETTEER_KINDS = frozenset({"Site", "Organisation"})
ENTRY_STATUSES = frozenset({"candidate", "approved"})


@dataclass(frozen=True)
class GazetteerEntry:
    iri: str
    kind: str
    label: str
    subtype: str
    aliases: tuple[str, ...]
    folders: tuple[str, ...]
    """Folded folder names whose documents concern this entry."""
    codes: tuple[str, ...]
    """Report-code segment 3 values (``TZ``) meaning this entry."""
    operated_by: str | None
    status: str
    note: str | None = None

    @property
    def keys(self) -> tuple[str, ...]:
        """Folded name and aliases: what the read API matches in free text."""

        return tuple(sorted({fold(name) for name in (self.label, *self.aliases) if fold(name)}))


@dataclass(frozen=True)
class Gazetteer:
    version: str
    input_hash: str
    entries: tuple[GazetteerEntry, ...]

    def by_iri(self) -> dict[str, GazetteerEntry]:
        return {entry.iri: entry for entry in self.entries}

    def for_folders(self, folders: Iterable[str]) -> list[GazetteerEntry]:
        """Entries named by folder components (folded), in folder order, without repeats."""

        found: dict[str, GazetteerEntry] = {}
        for folder in folders:
            key = fold(folder)
            for entry in self.entries:
                if key in entry.folders:
                    found.setdefault(entry.iri, entry)
        return list(found.values())

    def for_code(self, s3: str | None) -> GazetteerEntry | None:
        if not s3:
            return None
        return next((entry for entry in self.entries if s3.upper() in entry.codes), None)

    def mentioned(self, text: str, *, kind: str | None = None) -> list[GazetteerEntry]:
        """Entries whose folded name or alias appears as whole words in ``text``."""

        padded = f" {fold(text)} "
        hits = [
            entry for entry in self.entries
            if (kind is None or entry.kind == kind)
            and any(f" {key} " in padded for key in entry.keys)
        ]
        return sorted(hits, key=lambda entry: entry.iri)


def parse_gazetteer(data: bytes) -> Gazetteer:
    """Parse and cross-check; any defect rejects the whole file."""

    try:
        document: Any = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InvalidInputError(f"Gazetteer is not UTF-8 JSON ({error})") from error
    if not isinstance(document, dict) or not isinstance(document.get("entries"), list):
        raise InvalidInputError("Gazetteer needs a version and an entries list")
    version = document.get("version")
    if not isinstance(version, str) or not version:
        raise InvalidInputError("Gazetteer needs a version")
    entries: dict[str, GazetteerEntry] = {}
    for number, item in enumerate(document["entries"], start=1):
        where = f"entry {number}"
        if not isinstance(item, dict):
            raise InvalidInputError(f"{where}: must be an object")
        iri, kind, label = item.get("iri"), item.get("kind"), item.get("label")
        if not (isinstance(iri, str) and iri.startswith(("urn:crv:site:", "urn:crv:org:"))):
            raise InvalidInputError(f"{where}: iri must be urn:crv:site: or urn:crv:org:")
        if kind not in GAZETTEER_KINDS or (kind == "Site") != iri.startswith("urn:crv:site:"):
            raise InvalidInputError(f"{where}: kind {kind!r} does not match {iri}")
        if not isinstance(label, str) or not label.strip():
            raise InvalidInputError(f"{where}: label is required")
        status = item.get("status", "candidate")
        if status not in ENTRY_STATUSES:
            raise InvalidInputError(f"{where}: status must be candidate or approved")
        if status == "approved" and not item.get("decision_id"):
            raise InvalidInputError(f"{where}: an approved entry needs a decision_id")
        if iri in entries:
            raise InvalidInputError(f"{where}: duplicate iri {iri}")
        lists = {}
        for key in ("aliases", "folders", "codes"):
            value = item.get(key, [])
            if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
                raise InvalidInputError(f"{where}: {key} must be a list of text")
            lists[key] = value
        operated_by = item.get("operated_by")
        entries[iri] = GazetteerEntry(
            iri=iri,
            kind=str(kind),
            label=label.strip(),
            subtype=str(item.get("subtype") or ""),
            aliases=tuple(lists["aliases"]),
            folders=tuple(sorted({fold(folder) for folder in lists["folders"]})),
            codes=tuple(sorted({code.upper() for code in lists["codes"]})),
            operated_by=operated_by if isinstance(operated_by, str) else None,
            status=str(status),
            note=item.get("note") if isinstance(item.get("note"), str) else None,
        )
    for entry in entries.values():
        if entry.operated_by and entry.operated_by not in entries:
            raise InvalidInputError(f"{entry.iri}: operated_by {entry.operated_by} is unknown")
        if entry.operated_by and not entry.operated_by.startswith("urn:crv:org:"):
            raise InvalidInputError(f"{entry.iri}: operated_by must be an organisation")
    codes = [code for entry in entries.values() for code in entry.codes]
    if len(codes) != len(set(codes)):
        raise InvalidInputError("A report-code segment maps to two gazetteer entries")
    return Gazetteer(version, sha256_hex(data), tuple(entries[iri] for iri in sorted(entries)))


def review_rows(gazetteer: Gazetteer) -> list[dict[str, str]]:
    """One row per entry for the expert review sheet (KG-0)."""

    by_iri = gazetteer.by_iri()
    return [
        {
            "iri": entry.iri,
            "kind": entry.kind,
            "subtype": entry.subtype,
            "label": entry.label,
            "aliases": "; ".join(entry.aliases),
            "folders": "; ".join(entry.folders),
            "report_code_segment3": "; ".join(entry.codes),
            "operated_by": by_iri[entry.operated_by].label if entry.operated_by else "",
            "status": entry.status,
            "note": entry.note or "",
            "expert_decision": "",
            "expert_comment": "",
        }
        for entry in gazetteer.entries
    ]
