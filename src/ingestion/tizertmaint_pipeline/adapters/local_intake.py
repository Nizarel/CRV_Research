"""Long-path-safe offline intake with bounded archive expansion and content-addressed output."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import stat
import tempfile
import zipfile
from collections.abc import Iterator
from pathlib import Path, PurePosixPath
from typing import IO

from ..domain.errors import PipelineError, UnsupportedContentError
from ..domain.intake import IntakeItem, IntakeLimits, IntakeManifest
from ..domain.sniff import FileKind, ZipLimits, check_zip_safety, sniff
from .parsers.office import OfficeConverter

logger = logging.getLogger(__name__)
_READY = {
    FileKind.PDF, FileKind.DOCX, FileKind.PPTX, FileKind.XLSX, FileKind.XLSB, FileKind.XLS,
    FileKind.ODS, FileKind.TEXT, FileKind.HTML, FileKind.PNG, FileKind.JPEG, FileKind.TIFF,
}


def long_path(path: Path) -> Path:
    value = str(path.absolute())
    if os.name == "nt" and not value.startswith("\\\\?\\"):
        value = "\\\\?\\UNC\\" + value[2:] if value.startswith("\\\\") else "\\\\?\\" + value
    return Path(value)


def reparse(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def object_path(output: Path, digest: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise ValueError("Invalid object digest")
    return output / "objects" / digest


def verify_object(output: Path, digest: str) -> Path:
    path = object_path(output, digest)
    if reparse(path):
        raise ValueError("Intake objects must not be links")
    with path.open("rb") as handle:
        if hashlib.file_digest(handle, "sha256").hexdigest() != digest:
            raise ValueError(f"Intake object failed digest verification: {digest}")
    return path


def safe_member(info: zipfile.ZipInfo) -> str:
    name = info.filename.replace("\\", "/")
    path = PurePosixPath(name)
    if (
        path.is_absolute() or ".." in path.parts or ":" in name or "\x00" in name
        or stat.S_ISLNK(info.external_attr >> 16)
    ):
        raise UnsupportedContentError(f"Unsafe archive member: {name}")
    return name


class CorpusIntake:
    def __init__(
        self, output: Path, limits: IntakeLimits, converter: OfficeConverter | None = None
    ) -> None:
        self.output = long_path(output)
        self.limits = limits
        self.converter = converter
        self.items: list[IntakeItem] = []
        self.total_bytes = 0
        self.members = 0
        self.previous: dict[tuple[str | None, str | None], IntakeItem] = {}

    def _store(self, handle: IO[bytes]) -> tuple[str, int]:
        digest = hashlib.sha256()
        size = 0
        with tempfile.NamedTemporaryFile(dir=self.output, delete=False) as temp:
            temporary = Path(temp.name)
            try:
                while chunk := handle.read(1024 * 1024):
                    size += len(chunk)
                    self.total_bytes += len(chunk)
                    if size > self.limits.max_source_bytes:
                        raise UnsupportedContentError("Source exceeds intake byte limit")
                    if self.total_bytes > self.limits.max_total_bytes:
                        raise UnsupportedContentError("Intake exceeds total byte budget")
                    temp.write(chunk)
                    digest.update(chunk)
            except BaseException:
                temp.close()
                temporary.unlink()
                raise
        key = digest.hexdigest()
        destination = object_path(self.output, key)
        if destination.exists():
            verify_object(self.output, key)
            temporary.unlink()
        else:
            temporary.replace(destination)
        return key, size

    def _failed(self, name: str, error: Exception, **fields: object) -> None:
        logger.error("Corpus intake failed", extra={"source_path": name, "error": str(error)})
        self.items.append(IntakeItem(
            source_path=name, status="failed", reason=str(error), **fields
        ))

    def _visit(
        self, handle: IO[bytes], name: str, members: tuple[str, ...] = (),
        ancestors: tuple[str, ...] = (),
    ) -> None:
        digest, size = self._store(handle)
        source = object_path(self.output, digest)
        with source.open("rb") as stored:
            head = stored.read(8192)
        names = None
        if zipfile.is_zipfile(source):
            with zipfile.ZipFile(source) as archive:
                entries = archive.infolist()
                check_zip_safety(
                    [(i.filename, i.compress_size, i.file_size) for i in entries],
                    compressed_size=size,
                    limits=ZipLimits(
                        max_entries=self.limits.max_members,
                        max_uncompressed_bytes=self.limits.max_archive_bytes,
                        max_ratio=self.limits.max_ratio,
                    ),
                )
                names = [safe_member(i) for i in entries]
        kind = sniff(head, name=name, zip_entries=names).kind
        common = {
            "source_path": name, "input_hash": digest, "content_hash": digest,
            "file_kind": kind.value, "size_bytes": size,
            "archive_members": members, "ancestor_hashes": ancestors,
        }
        if kind is FileKind.ZIP:
            if len(members) >= self.limits.max_depth:
                raise UnsupportedContentError("Archive nesting limit reached")
            self.items.append(IntakeItem(status="expanded", **common))
            with zipfile.ZipFile(source) as archive:
                for info in archive.infolist():
                    if info.is_dir():
                        continue
                    self.members += 1
                    member = safe_member(info)
                    child = f"{name}!/{member}"
                    try:
                        if self.members > self.limits.max_members:
                            raise UnsupportedContentError("Global archive member limit reached")
                        with archive.open(info) as stream:
                            self._visit(stream, child, (*members, member), (*ancestors, digest))
                    except (
                        OSError, ValueError, RuntimeError, zipfile.BadZipFile, PipelineError
                    ) as error:
                        self._failed(child, error, archive_members=(*members, member),
                                     ancestor_hashes=(*ancestors, digest))
            return
        if kind is FileKind.CFB_OTHER and Path(name).suffix.lower() in (".doc", ".ppt"):
            if self.converter is None:
                self._failed(
                    name, UnsupportedContentError("Legacy Office needs a pinned --office-image"),
                    input_hash=digest, archive_members=members, ancestor_hashes=ancestors,
                )
                return
            cached = self.previous.get((digest, self.converter.image))
            if cached and cached.content_hash:
                verify_object(self.output, cached.content_hash)
                converted_hash, converted_size = cached.content_hash, cached.size_bytes
            else:
                with tempfile.TemporaryDirectory(dir=self.output) as directory:
                    converted = Path(directory) / "converted.pdf"
                    self.converter.convert(source, converted, Path(name).suffix.lower())
                    with converted.open("rb") as stream:
                        converted_hash, converted_size = self._store(stream)
            self.items.append(IntakeItem(
                **{**common, "content_hash": converted_hash, "size_bytes": converted_size,
                   "file_kind": FileKind.PDF.value},
                status="converted", converter=self.converter.image,
            ))
            return
        if kind not in _READY or not head.strip():
            self.items.append(IntakeItem(
                status="excluded", reason=f"Empty or unsupported {kind.value} input", **common
            ))
            return
        self.items.append(IntakeItem(status="ready", **common))

    def prepare(self, root: Path) -> IntakeManifest:
        self.items = []
        self.total_bytes = self.members = 0
        root = long_path(root)
        if not root.is_dir() or reparse(root):
            raise ValueError("Corpus root must be a real directory")
        if self.output == root or root in self.output.parents:
            raise ValueError("Intake output must be outside the corpus")
        self.output.mkdir(parents=True, exist_ok=True)
        (self.output / "objects").mkdir(exist_ok=True)
        manifest_path = self.output / "manifest.json"
        if manifest_path.exists():
            old = IntakeManifest.model_validate_json(manifest_path.read_bytes())
            self.previous = {
                (item.input_hash, item.converter): item
                for item in old.items if item.status == "converted"
            }
        complete = False
        try:
            for source in self._files(root):
                name = source.relative_to(root).as_posix()
                try:
                    if reparse(source):
                        raise UnsupportedContentError("Links/reparse points are not ingested")
                    with source.open("rb") as handle:
                        self._visit(handle, name)
                except (
                    OSError, ValueError, RuntimeError, zipfile.BadZipFile, PipelineError
                ) as error:
                    self._failed(name, error)
            complete = True
        finally:
            manifest = IntakeManifest(root=str(root), complete=complete, items=tuple(self.items))
            temporary = self.output / "manifest.json.tmp"
            temporary.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
            temporary.replace(manifest_path)
        return manifest

    def _files(self, root: Path) -> Iterator[Path]:
        def on_error(error: OSError) -> None:
            self._failed(str(error.filename), error)

        for folder, directories, files in os.walk(root, onerror=on_error, followlinks=False):
            directories.sort()
            for name in directories[:]:
                directory = Path(folder) / name
                if reparse(directory):
                    directories.remove(name)
                    self._failed(str(directory), ValueError("Directory links are not ingested"))
            for name in sorted(files):
                yield Path(folder) / name
