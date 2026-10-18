"""Publish a verified offline manifest; originals and manifests remain immutable."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ...domain.errors import IntegrityError, InvalidInputError
from ...domain.ids import sha256_hex
from ...domain.intake import IntakeManifest
from ...domain.models import ArtifactRef, SourceProvenance
from ...domain.sniff import FileKind
from ...fileio import sha256_file
from ..local_intake import long_path, verify_object
from .artifact_store import BlobArtifactStore


async def publish_intake(
    container: Any, manifest_path: Path, *, prefix: str, max_source_bytes: int
) -> AsyncIterator[tuple[str, str, str]]:
    from azure.core import MatchConditions
    from azure.core.exceptions import ResourceNotFoundError
    from azure.storage.blob import ContentSettings

    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", prefix):
        raise InvalidInputError("Intake prefix must be a lowercase collection identifier")
    manifest_path = long_path(manifest_path)
    try:
        manifest = IntakeManifest.model_validate_json(manifest_path.read_bytes())
    except ValidationError as error:
        raise InvalidInputError("Invalid intake manifest") from error
    if not manifest.complete or manifest.failed:
        raise InvalidInputError("Only complete manifests with zero failed inputs can be published")
    paths: set[str] = set()
    verified: dict[str, Path] = {}
    for item in manifest.items:
        if item.source_path in paths:
            raise InvalidInputError(f"Duplicate intake source path: {item.source_path}")
        paths.add(item.source_path)
        if item.status in ("ready", "converted") and (
            not item.content_hash or not item.input_hash
            or item.file_kind not in {kind.value for kind in FileKind}
            or not 0 < item.size_bytes <= max_source_bytes
        ):
            raise InvalidInputError(f"Invalid ready intake item: {item.source_path}")
        for digest in (item.input_hash, *item.ancestor_hashes, item.content_hash):
            if digest and digest not in verified:
                try:
                    verified[digest] = await asyncio.to_thread(
                        verify_object, manifest_path.parent, digest
                    )
                except (OSError, ValueError) as error:
                    raise IntegrityError(f"Invalid intake object: {digest}") from error
                if verified[digest].stat().st_size > max_source_bytes:
                    raise InvalidInputError("Intake object exceeds source byte limit")
        if item.content_hash and verified[item.content_hash].stat().st_size != item.size_bytes:
            raise IntegrityError(f"Intake size mismatch: {item.source_path}")
    store = BlobArtifactStore(container, max_bytes=max_source_bytes)
    manifest_ref = await store.put(
        manifest.model_dump_json().encode(), media_type="application/json",
        schema_name=manifest.schema_name,
    )
    uploaded: dict[str, ArtifactRef] = {}
    for item in manifest.items:
        for digest in (item.input_hash, *item.ancestor_hashes, item.content_hash):
            if digest and digest not in uploaded:
                path = await asyncio.to_thread(verify_object, manifest_path.parent, digest)
                ref = await store.put_file(path, media_type="application/octet-stream")
                if ref.sha256 != digest:
                    raise IntegrityError("Intake object changed during publication")
                uploaded[digest] = ref
        if item.status not in ("ready", "converted"):
            continue
        if not item.content_hash or not item.input_hash:
            raise InvalidInputError("Ready intake item has no content identity")
        path = await asyncio.to_thread(verify_object, manifest_path.parent, item.content_hash)
        provenance = SourceProvenance(
            source_path=item.source_path, original_hash=item.input_hash,
            original_blob=uploaded[item.input_hash].uri, archive_members=item.archive_members,
            converter=item.converter, manifest_blob=manifest_ref.uri,
        )
        metadata = {
            "content_hash": item.content_hash,
            "ingest_provenance": json.dumps(provenance.model_dump(mode="json"), ensure_ascii=True),
        }
        if sum(len(k) + len(v) for k, v in metadata.items()) > 7000:
            raise InvalidInputError("Intake lineage exceeds blob metadata size budget")
        extension = item.file_kind
        if extension == "text":
            original_extension = Path(item.source_path).suffix.lower().removeprefix(".")
            extension = (
                original_extension if original_extension in ("csv", "tsv", "md") else "txt"
            )
        name = f"{prefix}/sources/{sha256_hex(item.source_path)}.{extension}"
        blob = container.get_blob_client(name)
        try:
            existing = await blob.get_blob_properties()
        except ResourceNotFoundError:
            existing = None
        if existing is None or existing.metadata != metadata:
            options = (
                {"etag": existing.etag, "match_condition": MatchConditions.IfNotModified}
                if existing else {}
            )
            with path.open("rb") as handle:
                await blob.upload_blob(
                    handle, length=item.size_bytes, overwrite=existing is not None,
                    max_concurrency=1, metadata=metadata,
                    content_settings=ContentSettings(content_type=(
                        "application/pdf" if item.file_kind == "pdf" else "application/octet-stream"
                    )),
                    **options,
                )
            if (await asyncio.to_thread(sha256_file, path))[0] != item.content_hash:
                raise IntegrityError("Local source changed during upload")
        properties = await blob.get_blob_properties()
        yield name, properties.etag, item.content_hash
