"""Preflight: identify content from its bytes and measure the signals that drive routing.

Runs once per content hash. Folder class is only a prior; the decision comes from the bytes
(signature, openability, page geometry, text-layer density, sheet visibility).
"""

from __future__ import annotations

import asyncio
import zipfile
from pathlib import Path, PurePosixPath

from ..domain.errors import UnsupportedContentError
from ..domain.models import ArtifactLink, StageJob, StageOutcome
from ..domain.routing import (
    FAMILY_BY_KIND,
    Family,
    PageSignals,
    PreflightReport,
    SheetSignals,
)
from ..domain.sniff import (
    SNIFF_BYTES,
    ZipLimits,
    check_zip_safety,
    needs_container_listing,
    sniff,
)
from ..domain.states import StageName
from .base import StageContext
from .common import fetch_content, parse_inputs, put_json, scratch
from .links import PlanInputs, PreflightInputs, content_unit

STAGE_VERSION = "3"


def _container(path: Path, limits: ZipLimits) -> tuple[list[str], str | None]:
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            check_zip_safety(
                [(info.filename, info.compress_size, info.file_size) for info in infos],
                compressed_size=path.stat().st_size,
                limits=limits,
            )
            mimetype = None
            if "mimetype" in archive.namelist():
                mimetype = archive.read("mimetype")[:200].decode("ascii", "replace").strip()
            return [info.filename for info in infos], mimetype
    except zipfile.BadZipFile as error:
        raise UnsupportedContentError("Corrupt zip container") from error


class PreflightStage:
    def __init__(self, *, max_source_bytes: int, zip_limits: ZipLimits | None = None) -> None:
        self._max_source_bytes = max_source_bytes
        self._zip_limits = zip_limits or ZipLimits()

    @property
    def version(self) -> str:
        return STAGE_VERSION

    async def execute(self, context: StageContext, job: StageJob) -> StageOutcome:
        inputs = parse_inputs(PreflightInputs, job)
        services = context.services
        async with scratch(services) as work:
            source = work / "source"
            snapshot = await fetch_content(
                services, inputs.content_hash, source, max_bytes=self._max_source_bytes
            )
            with source.open("rb") as handle:
                head = handle.read(SNIFF_BYTES)
            entries: list[str] | None = None
            odf_mimetype = None
            if needs_container_listing(head):
                entries, odf_mimetype = await asyncio.to_thread(
                    _container, source, self._zip_limits
                )
            sniffed = sniff(
                head, name=snapshot.blob_name, zip_entries=entries, odf_mimetype=odf_mimetype
            )
            family = FAMILY_BY_KIND.get(sniffed.kind, Family.UNKNOWN)
            pages: tuple[PageSignals, ...] = ()
            sheets: tuple[SheetSignals, ...] = ()
            page_count: int | None = None
            if family is Family.PDF:
                profile = await services.require_pdf().profile(source)
                pages, page_count = profile.pages, profile.page_count
            elif family is Family.SPREADSHEET:
                sheets = tuple(await services.require_sheets().profile(source))

        notes = []
        if sniffed.mismatch and sniffed.declared is not None:
            notes.append(f"extension suggests {sniffed.declared.value}")
        folder = PurePosixPath(snapshot.blob_name).parts
        report = PreflightReport(
            content_hash=inputs.content_hash,
            size_bytes=snapshot.size_bytes,
            snapshot_id=snapshot.snapshot_id,
            blob_name=snapshot.blob_name,
            extension=sniffed.extension,
            file_kind=sniffed.kind,
            declared_kind=sniffed.declared,
            family=family,
            page_count=page_count,
            pages=pages,
            sheets=sheets,
            folder_hint=folder[0] if len(folder) > 1 else None,
            notes=tuple(notes),
        )
        artifact = await put_json(services, report, schema_name=report.schema_name)
        successor = services.planner.spec(
            StageName.PLAN,
            unit_id=content_unit(inputs.content_hash),
            inputs=PlanInputs(preflight=artifact, policy=inputs.policy).model_dump(mode="json"),
            content_hash=inputs.content_hash,
        )
        return StageOutcome.succeeded(
            artifacts=(ArtifactLink(role="preflight", artifact=artifact),),
            next_jobs=(successor,),
            checkpoint={"family": family.value, "file_kind": sniffed.kind.value},
        )
