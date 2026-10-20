"""Legacy Office conversion only inside a network-isolated, resource-limited container."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ...domain.errors import ConfigurationError, UnsupportedContentError

_PINNED_IMAGE = re.compile(r"(?:[a-zA-Z0-9./:_-]+@)?sha256:[a-f0-9]{64}")


class DocumentConverter(Protocol):
    @property
    def image(self) -> str:
        """Pinned converter identity, recorded in intake lineage and conversion caches."""
        ...

    def convert(self, source: Path, destination: Path, extension: str) -> None: ...


def _check_pdf(path: Path, max_bytes: int) -> None:
    if path.stat().st_size > max_bytes:
        raise UnsupportedContentError("Converted PDF exceeds the source limit")
    with path.open("rb") as handle:
        if not handle.read(8).startswith(b"%PDF-"):
            raise UnsupportedContentError("Office converter did not produce a PDF")


@dataclass(frozen=True)
class PrecomputedOfficeConverter:
    """Conversions produced earlier by the isolated converter job, keyed by input SHA-256.

    ``directory`` holds ``<input-sha256>.pdf`` and the job's ``results.json``. The image is the
    exact digest that produced them; a missing or failed result is an explicit failure.
    """

    image: str
    directory: Path
    max_output_bytes: int = 1024**3

    def convert(self, source: Path, destination: Path, extension: str) -> None:
        if not _PINNED_IMAGE.fullmatch(self.image):
            raise ConfigurationError("Office image must be pinned by sha256 digest")
        if extension not in (".doc", ".ppt"):
            raise UnsupportedContentError("Only legacy Word and PowerPoint use Office conversion")
        with source.open("rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        converted = self.directory / f"{digest}.pdf"
        if not converted.is_file():
            reason = self._reason(digest) or "no converted PDF for this input"
            raise UnsupportedContentError(f"Office conversion failed: {reason}")
        _check_pdf(converted, self.max_output_bytes)
        shutil.copyfile(converted, destination)

    def _reason(self, digest: str) -> str | None:
        results = self.directory / "results.json"
        if not results.is_file():
            return None
        entry = json.loads(results.read_text(encoding="utf-8")).get(digest) or {}
        return str(entry["reason"]) if entry.get("reason") else None


def _mount_path(path: Path) -> str:
    value = str(path)
    if value.startswith("\\\\?\\UNC\\"):
        return "\\\\" + value[8:]
    return value.removeprefix("\\\\?\\")


@dataclass(frozen=True)
class OfficeConverter:
    image: str
    timeout_seconds: float = 120
    max_output_bytes: int = 1024**3

    def convert(self, source: Path, destination: Path, extension: str) -> None:
        if not _PINNED_IMAGE.fullmatch(self.image):
            raise ConfigurationError("Office image must be pinned by sha256 digest")
        if extension not in (".doc", ".ppt"):
            raise UnsupportedContentError("Only legacy Word and PowerPoint use Office conversion")
        docker = shutil.which("docker")
        if docker is None:
            raise ConfigurationError("Docker is required for isolated Office conversion")
        name = f"crv-office-{uuid.uuid4().hex}"
        with tempfile.TemporaryDirectory(prefix="office-", dir=destination.parent) as folder:
            work = Path(folder).resolve()
            inputs, outputs = work / "input", work / "output"
            inputs.mkdir()
            outputs.mkdir()
            shutil.copyfile(source, inputs / f"source{extension}")
            command = [
                docker, "run", "--rm", "--pull=never", "--name", name, "--network=none",
                "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
                "--memory=1536m", "--cpus=1", "--pids-limit=128",
                "--tmpfs=/tmp:rw,noexec,nosuid,size=256m",
                "--mount", f"type=bind,src={_mount_path(inputs)},dst=/input,readonly",
                "--mount", f"type=bind,src={_mount_path(outputs)},dst=/output",
                self.image, f"/input/source{extension}",
            ]
            with (work / "conversion.log").open("wb") as log:
                try:
                    result = subprocess.run(  # noqa: S603 -- validated image, fixed argv, no shell
                        command, stdout=log, stderr=subprocess.STDOUT,
                        timeout=self.timeout_seconds, check=False,
                    )
                except subprocess.TimeoutExpired as error:
                    subprocess.run(  # noqa: S603 -- remove only this invocation's container
                        [docker, "rm", "-f", name], capture_output=True, timeout=30, check=True
                    )
                    raise UnsupportedContentError("Office conversion timed out") from error
            converted = outputs / "source.pdf"
            if result.returncode or not converted.is_file():
                with (work / "conversion.log").open("rb") as log:
                    log.seek(max(0, (work / "conversion.log").stat().st_size - 1500))
                    detail = log.read().decode("utf-8", errors="replace")
                raise UnsupportedContentError(
                    f"Office conversion failed (exit {result.returncode}): {detail}"
                )
            _check_pdf(converted, self.max_output_bytes)
            shutil.move(str(converted), destination)
