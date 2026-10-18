"""Read-only, backend-neutral corpus discovery. Never executes or converts source files."""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import re
from collections import Counter, defaultdict
from pathlib import Path, PurePosixPath
from typing import Any

from ..domain.document_classes import classify_document_category
from ..domain.ids import stable_urn as stable_id

SCHEMA_VERSION = "1.0.0"
TAG_PATTERN = re.compile(r"(?<![A-Z0-9])\d{4}-[A-Z]{1,4}-\d{3,4}[A-Z]?(?![A-Z0-9])", re.I)
NATIVE = {".dwg", ".dxf", ".ifc", ".nwd", ".nwc", ".rtd", ".step", ".stp", ".zm2", ".smlx"}
FABRICATION = {".nc", ".nc1", ".nc2", ".upd", ".dstv"}
TEXT = {".txt", ".md", ".xml", ".json", ".yaml", ".yml", ".log"}

MAX_DOCUMENT_INTELLIGENCE_BYTES = 500 * 1024 * 1024
DOCUMENT_INTELLIGENCE_EXTENSIONS = {
    ".bmp",
    ".docx",
    ".heif",
    ".html",
    ".htm",
    ".jpeg",
    ".jpg",
    ".pdf",
    ".png",
    ".pptx",
    ".tif",
    ".tiff",
}
SPREADSHEET_EXTENSIONS = {".csv", ".ods", ".tsv", ".xls", ".xlsb", ".xlsm", ".xlsx"}
ARCHIVE_EXTENSIONS = {".7z", ".rar", ".zip"}
NATIVE_CONVERSION_EXTENSIONS = {".dwg", ".dxf", ".ifc", ".rtd", ".step", ".stp", ".zm2"}
EXCLUDED_EXTENSIONS = {".dll", ".drv", ".exe", ".msi", ".sys"}


def classify(extension: str, size_bytes: int) -> tuple[str, str | None]:
    """v1 extraction route by extension and size, without opening the content."""

    extension = extension.lower()
    if size_bytes == 0:
        return "excluded", "zero_byte"
    if extension in EXCLUDED_EXTENSIONS:
        return "excluded", "unsupported_executable_or_driver"
    if extension in ARCHIVE_EXTENSIONS:
        return "archive", None
    if extension in SPREADSHEET_EXTENSIONS:
        return "spreadsheet", None
    if extension in NATIVE_CONVERSION_EXTENSIONS:
        return "native_conversion", None
    if extension in DOCUMENT_INTELLIGENCE_EXTENSIONS:
        if size_bytes > MAX_DOCUMENT_INTELLIGENCE_BYTES:
            return "excluded", "exceeds_document_intelligence_size_limit"
        return "document_intelligence", None
    return "excluded", "unsupported_extension"


def local_path(path: Path) -> Path:
    resolved = str(path.absolute())
    if os.name != "nt" or resolved.startswith("\\\\?\\"):
        return Path(resolved)
    if resolved.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + resolved[2:])
    return Path("\\\\?\\" + resolved)


def file_hash(path: Path) -> str:
    with local_path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def path_context(relative_path: str) -> dict:
    folders = PurePosixPath(relative_path).parts[:-1]
    owner_index = next(
        (index for index, folder in enumerate(folders) if TAG_PATTERN.search(folder)), None
    )
    tags = (
        []
        if owner_index is None
        else sorted(set(TAG_PATTERN.findall(folders[owner_index].upper())))
    )
    category = None
    if owner_index is not None and owner_index + 1 < len(folders):
        category = folders[owner_index + 1]
    return {
        "domain_raw": folders[0] if folders else None,
        "equipment_folder_raw": folders[owner_index] if owner_index is not None else None,
        "equipment_tag_candidates": tags,
        "primary_equipment_tag_candidate": tags[0] if len(tags) == 1 else None,
        "document_category_raw": category,
        "document_class_candidate": classify_document_category(category),
        "metadata_basis": "folder_heuristic",
        "review_status": "unreviewed",
    }


def disposition(relative_path: str, size: int) -> tuple[str, str]:
    path = PurePosixPath(relative_path)
    extension = path.suffix.lower()
    if size == 0:
        return "quarantine", "zero_byte"
    if (
        path.name.lower() in {"thumbs.db", ".ds_store"}
        or path.name.startswith(("~$", "._"))
        or "__MACOSX" in path.parts
    ):
        return "quarantine", "os_or_lock_artifact"
    if extension in {".exe", ".dll", ".sys", ".msi", ".drv", ".lnk", ".bat", ".cmd", ".ps1"}:
        return "quarantine", "executable_or_shortcut"
    if extension in NATIVE:
        return "native_conversion", "converter_required"
    if extension in FABRICATION:
        return "fabrication_review", "maintenance_value_unconfirmed"
    if extension in TEXT:
        return "native_text", "parser_required"
    if extension in {".doc", ".ppt", ".odt", ".odp", ".rtf", ".msg", ".eml"}:
        return "office_conversion", "sandboxed_converter_required"
    if extension in {".mp3", ".wav", ".mp4", ".avi", ".mov", ".flac"}:
        return "media", "transcription_or_video_analysis_required"
    route, reason = classify(extension, size)
    if reason == "exceeds_document_intelligence_size_limit":
        return "document_split", reason
    if reason:
        return "unsupported_review", reason
    return route, "candidate_route_not_extraction_success"


def inventory(root: Path, collection: str):
    root = local_path(root)
    if not root.is_dir():
        raise ValueError(f"Not a directory: {root}")

    def walk(directory: Path):
        try:
            with os.scandir(directory) as entries:
                children = sorted(entries, key=lambda entry: entry.name)
        except OSError as error:
            yield {
                "record_type": "error",
                "relative_path": directory.relative_to(root).as_posix(),
                "error": str(error),
            }
            return
        for entry in children:
            path = Path(entry.path)
            relative = path.relative_to(root).as_posix()
            try:
                stat = entry.stat(follow_symlinks=False)
                if entry.is_symlink() or getattr(stat, "st_file_attributes", 0) & 0x400:
                    yield {
                        "record_type": "error",
                        "relative_path": relative,
                        "error": "link_or_reparse_point_not_followed",
                    }
                    continue
                if entry.is_dir(follow_symlinks=False):
                    yield from walk(path)
                    continue
                digest = file_hash(path)
                after = path.stat()
                if (stat.st_size, stat.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    raise OSError("source_changed_during_hash")
                route, reason = disposition(relative, stat.st_size)
                yield {
                    "record_type": "source",
                    "schema_version": SCHEMA_VERSION,
                    "source_id": stable_id("source", collection, relative),
                    "placement_id": stable_id("placement", collection, relative),
                    "collection": collection,
                    "relative_path": relative,
                    "content_id": f"urn:sha256:{digest}",
                    "content_hash": digest,
                    "size_bytes": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                    "extension": path.suffix.lower(),
                    "mime_hint": mimetypes.guess_type(path.name)[0],
                    "route": route,
                    "route_reason": reason,
                    "context": path_context(relative),
                    "access_policy": "unassigned",
                }
            except OSError as error:
                yield {"record_type": "error", "relative_path": relative, "error": str(error)}

    yield from walk(root)


def profile(path: Path, extension: str) -> dict:
    if extension != ".pdf":
        return {"status": "not_profiled", "reason": "metadata_only_in_phase1"}
    try:
        import pymupdf

        with pymupdf.open(str(local_path(path))) as document:
            if document.needs_pass:
                return {"status": "password_required"}
            pages = []
            for ordinal in range(min(3, len(document))):
                page = document[ordinal]
                pages.append(
                    {
                        "page_number": ordinal + 1,
                        "width_points": page.rect.width,
                        "height_points": page.rect.height,
                        "text_characters": len(page.get_text()),
                        "image_references": len(page.get_images()),
                    }
                )
            return {"status": "profiled", "page_count": len(document), "sampled_pages": pages}
    except ImportError:
        return {"status": "unavailable", "reason": "install_discovery_extra_for_pdf_profiles"}
    except Exception as error:
        return {"status": "error", "reason": str(error)}


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_record(stream, value) -> None:
    stream.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")


def discover(root: Path, output: Path, collection: str, sample_per_stratum: int = 2) -> dict:
    root, output = root.resolve(), output.resolve()
    if not collection.strip() or sample_per_stratum < 0:
        raise ValueError("A collection key and nonnegative sample size are required")
    if output == root or root in output.parents:
        raise ValueError("Output must be outside the source corpus")
    if not local_path(root).is_dir():
        raise ValueError("Source corpus does not exist")
    if output.exists():
        raise ValueError("Use a new output directory; existing evidence is never overwritten")
    output.mkdir(parents=True)
    counts: Counter[str] = Counter()
    extensions: Counter[str] = Counter()
    routes: Counter[str] = Counter()
    classes: Counter[str] = Counter()
    domains: Counter[str] = Counter()
    contents: dict[str, dict[str, Any]] = {}
    samples: Counter[tuple[str | None, str, str]] = Counter()
    sampled_hashes = set()
    manifest_digest = hashlib.sha256()
    with (
        (output / "sources.jsonl").open("w", encoding="utf-8", newline="\n") as sources,
        (output / "issues.jsonl").open("w", encoding="utf-8", newline="\n") as issues,
        (output / "profiles.jsonl").open("w", encoding="utf-8", newline="\n") as profiles,
    ):
        for record in inventory(root, collection):
            if record["record_type"] == "error":
                counts["errors"] += 1
                write_record(issues, record)
                continue
            write_record(sources, record)
            manifest_digest.update(
                json.dumps(record, sort_keys=True, ensure_ascii=False).encode("utf-8") + b"\n"
            )
            counts["files"] += 1
            counts["bytes"] += record["size_bytes"]
            extensions[record["extension"]] += 1
            routes[record["route"]] += 1
            context = record["context"]
            classes[context["document_class_candidate"]] += 1
            domains[context["domain_raw"] or "(root)"] += 1
            digest = record["content_hash"]
            content = contents.setdefault(
                digest,
                {
                    "content_id": record["content_id"],
                    "content_hash": digest,
                    "size_bytes": record["size_bytes"],
                    "source_ids": [],
                    "paths": [],
                    "routes": set(),
                },
            )
            content["source_ids"].append(record["source_id"])
            content["paths"].append(record["relative_path"])
            content["routes"].add(record["route"])
            stratum = (
                context["domain_raw"],
                record["extension"],
                context["document_class_candidate"],
            )
            if (
                record["extension"] == ".pdf"
                and digest not in sampled_hashes
                and samples[stratum] < sample_per_stratum
            ):
                samples[stratum] += 1
                sampled_hashes.add(digest)
                write_record(
                    profiles,
                    {
                        "source_id": record["source_id"],
                        "relative_path": record["relative_path"],
                        **profile(root / record["relative_path"], record["extension"]),
                    },
                )
    distinct_routes = defaultdict(set)
    with (output / "contents.jsonl").open("w", encoding="utf-8", newline="\n") as stream:
        for digest, content in sorted(contents.items()):
            for route in content["routes"]:
                distinct_routes[route].add(digest)
            content["routes"] = sorted(content["routes"])
            write_record(stream, content)
    summary = {
        "schema_version": SCHEMA_VERSION,
        "collection": collection,
        "root_locator": str(root),
        "manifest_digest": manifest_digest.hexdigest(),
        "counts": dict(counts),
        "complete": counts["errors"] == 0,
        "distinct_contents": len(contents),
        "distinct_bytes": sum(content["size_bytes"] for content in contents.values()),
        "duplicate_groups": sum(len(content["source_ids"]) > 1 for content in contents.values()),
        "redundant_placements_preserved": counts["files"] - len(contents),
        "extensions": dict(sorted(extensions.items())),
        "routes": dict(sorted(routes.items())),
        "distinct_by_route_nonadditive": {
            route: len(hashes) for route, hashes in sorted(distinct_routes.items())
        },
        "document_classes": dict(sorted(classes.items())),
        "domains": dict(sorted(domains.items())),
        "profile_samples": len(sampled_hashes),
        "sample_per_zone_extension_class": sample_per_stratum,
        "limitations": [
            "No extraction, OCR, embeddings or cloud parity check",
            "MIME is an extension hint, not signature validation",
            "PDF profiles inspect at most three pages; not a statistical population estimate",
            "Archive lineage and deleted placements need the historical audit manifests",
            "ACLs are unassigned: not authorized for production retrieval",
        ],
    }
    write_json(output / "summary.json", summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--collection", required=True)
    parser.add_argument("--sample-per-stratum", type=int, default=2)
    args = parser.parse_args()
    summary = discover(args.root, args.output, args.collection, args.sample_per_stratum)
    print(json.dumps(summary, ensure_ascii=True, indent=2))
    if not summary["complete"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
