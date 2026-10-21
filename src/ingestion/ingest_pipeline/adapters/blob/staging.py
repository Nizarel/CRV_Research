"""Copy selected raw-corpus blobs (and single ZIP members) to a local staging volume.

Used inside the VNet by the pilot staging job: the raw corpus copy stays read-only, staged
paths mirror the corpus layout so intake classifies them exactly as the local corpus, and every
file is verified against the blob's size and Content-MD5 when the blob carries one.
"""

from __future__ import annotations

import asyncio
import hashlib
import zipfile
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from typing import Any

from ...domain.errors import IntegrityError, InvalidInputError, UnsupportedContentError
from ..local_intake import safe_member

MEMBER_SEPARATOR = "!/"
MEMBERS_SUFFIX = ".members"
_WINDOWS_ILLEGAL = str.maketrans({char: "_" for char in ':*?"<>|'})


def windows_key(name: str) -> str:
    """How a Windows download renames a path: illegal characters become ``_``."""

    return name.translate(_WINDOWS_ILLEGAL)


async def blob_index(container: Any, prefix: str) -> dict[str, list[str]]:
    """Corpus-relative blob names under ``prefix``, grouped by their Windows-safe form."""

    index: dict[str, list[str]] = {}
    async for blob in container.list_blobs(name_starts_with=prefix):
        relative = str(blob.name)[len(prefix):]
        index.setdefault(windows_key(relative), []).append(relative)
    return index


def resolve(entry: str, index: dict[str, list[str]]) -> str:
    """Map a locally listed path to the original blob name; exact names win, ambiguity fails."""

    archive, separator, member = entry.partition(MEMBER_SEPARATOR)
    candidates = index.get(windows_key(archive), [])
    if archive in candidates:
        resolved = archive
    elif len(candidates) == 1:
        [resolved] = candidates
    elif candidates:
        raise InvalidInputError(f"ambiguous source name; candidates: {sorted(candidates)}")
    else:
        raise InvalidInputError("blob not found")
    return resolved + separator + member


def _relative(name: str) -> PurePosixPath:
    path = PurePosixPath(name.replace("\\", "/"))
    if not name or path.is_absolute() or ".." in path.parts or "\x00" in name:
        raise InvalidInputError(f"Unsafe staging path: {name!r}")
    return path


def staged_path(destination: Path, entry: str) -> Path:
    """Where an entry lands: corpus layout, ZIP members under ``<archive>.members/``."""

    archive, _, member = entry.partition(MEMBER_SEPARATOR)
    path = destination.joinpath(*_relative(archive).parts)
    if member:
        path = path.with_name(path.name + MEMBERS_SUFFIX).joinpath(*_relative(member).parts)
    return path


async def _download(blob: Any, target: Path, *, max_bytes: int) -> dict[str, Any]:
    from azure.core.exceptions import ResourceNotFoundError

    try:
        properties = await blob.get_blob_properties()
    except ResourceNotFoundError as error:
        raise InvalidInputError("blob not found") from error
    size = int(properties.size)
    if size > max_bytes:
        raise UnsupportedContentError(f"{size} bytes exceeds the staging limit {max_bytes}")
    settings = getattr(properties, "content_settings", None)
    expected_md5 = bytes(getattr(settings, "content_md5", None) or b"")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".part")
    sha256, md5, written = hashlib.sha256(), hashlib.md5(usedforsecurity=False), 0
    downloader = await blob.download_blob(
        etag=properties.etag, match_condition=_if_not_modified(), max_concurrency=1
    )
    handle = await asyncio.to_thread(temporary.open, "wb")
    with handle:
        async for chunk in downloader.chunks():
            written += len(chunk)
            if written > size:
                break
            sha256.update(chunk)
            md5.update(chunk)
            await asyncio.to_thread(handle.write, chunk)
    if written != size or (expected_md5 and md5.digest() != expected_md5):
        temporary.unlink(missing_ok=True)
        raise IntegrityError("downloaded bytes do not match the blob size or Content-MD5")
    temporary.replace(target)
    return {
        "bytes": size, "sha256": sha256.hexdigest(), "etag": properties.etag,
        "md5_verified": bool(expected_md5),
    }


def _if_not_modified() -> Any:
    from azure.core import MatchConditions

    return MatchConditions.IfNotModified


def _extract(archive: Path, member: str, target: Path, *, max_bytes: int) -> dict[str, Any]:
    with zipfile.ZipFile(archive) as bundle:
        matches = [
            info for info in bundle.infolist()
            if not info.is_dir() and safe_member(info) == member
        ]
        if len(matches) != 1:
            raise InvalidInputError(f"archive has {len(matches)} entries named {member!r}")
        [info] = matches
        if info.file_size > max_bytes:
            raise UnsupportedContentError(f"member exceeds the staging limit {max_bytes}")
        target.parent.mkdir(parents=True, exist_ok=True)
        digest, written = hashlib.sha256(), 0
        with bundle.open(info) as stream, target.open("wb") as copy:
            while chunk := stream.read(1024 * 1024):
                written += len(chunk)
                if written > max_bytes:
                    raise UnsupportedContentError("member expanded past the staging limit")
                digest.update(chunk)
                copy.write(chunk)
    return {"bytes": written, "sha256": digest.hexdigest(), "md5_verified": False}


async def stage_blobs(
    container: Any,
    entries: Sequence[str] | None,
    destination: Path,
    *,
    prefix: str,
    max_bytes: int,
) -> dict[str, Any]:
    """Stage ``entries`` (paths relative to ``prefix``; ``a.zip!/m`` for one member).

    ``None`` stages every blob under ``prefix`` (the full corpus) under its original name.
    Entries listed from a Windows copy are resolved to the original blob names, and files are
    staged under those original names. Every entry gets an explicit outcome; one failure never
    hides another.
    """

    destination.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240 - one-time, local volume
    archives = destination.parent / (destination.name + ".archives")
    index = await blob_index(container, prefix)
    if entries is None:
        entries = sorted(name for names in index.values() for name in names)
    staged: list[dict[str, Any]] = []
    failed: list[dict[str, str]] = []
    downloaded: dict[str, Path] = {}
    for entry in dict.fromkeys(entries):
        try:
            source = resolve(entry, index)
            archive, _, member = source.partition(MEMBER_SEPARATOR)
            target = staged_path(destination, source)
            if member:
                local = downloaded.get(archive)
                if local is None:
                    local = archives / f"{len(downloaded):04d}.zip"
                    await _download(
                        container.get_blob_client(prefix + archive), local, max_bytes=max_bytes
                    )
                    downloaded[archive] = local
                outcome = await asyncio.to_thread(
                    _extract, local, member, target, max_bytes=max_bytes
                )
            else:
                outcome = await _download(
                    container.get_blob_client(prefix + source), target, max_bytes=max_bytes
                )
            staged.append({"entry": entry, "source": source, **outcome})
        except (InvalidInputError, UnsupportedContentError, IntegrityError, zipfile.BadZipFile,
                OSError) as error:
            failed.append({"entry": entry, "reason": str(error)})
    for local in downloaded.values():
        local.unlink(missing_ok=True)
    return {
        "prefix": prefix, "staged": staged, "failed": failed,
        "bytes": sum(int(item["bytes"]) for item in staged),
    }
