"""Content-addressed, write-once artifact store on Azure Blob."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Any

from ...domain.errors import IntegrityError, InvalidInputError
from ...domain.ids import artifact_key, sha256_hex
from ...domain.models import ArtifactRef
from ...fileio import sha256_file
from ..azure_errors import translate_azure_error


class BlobArtifactStore:
    """``ArtifactStore`` over an ``azure.storage.blob.aio.ContainerClient``.

    Uploads use ``If-None-Match: *``; an existing object is accepted only after its size and
    digest are verified, so an orphan from an interrupted run is never trusted blindly.
    """

    def __init__(self, container: Any, *, max_bytes: int) -> None:
        self._container = container
        self._max_bytes = max_bytes

    async def put(
        self, data: bytes, *, media_type: str, schema_name: str | None = None
    ) -> ArtifactRef:
        from azure.core.exceptions import ResourceExistsError
        from azure.storage.blob import ContentSettings

        if len(data) > self._max_bytes:
            raise InvalidInputError(f"Artifact of {len(data)} bytes exceeds {self._max_bytes}")
        digest = sha256_hex(data)
        key = artifact_key(digest)
        blob = self._container.get_blob_client(key)
        try:
            await blob.upload_blob(
                data,
                overwrite=False,
                content_settings=ContentSettings(content_type=media_type),
                metadata={"sha256": digest},
            )
        except ResourceExistsError:
            await self._verify_existing(blob, digest, len(data))
        except Exception as error:
            raise (translate_azure_error(error) or error) from error
        return ArtifactRef(
            sha256=digest,
            uri=key,
            size_bytes=len(data),
            media_type=media_type,
            schema_name=schema_name,
        )

    async def _verify_existing(self, blob: Any, digest: str, size: int) -> None:
        properties = await blob.get_blob_properties()
        if int(properties.size) != size:
            raise IntegrityError(f"Existing artifact {digest} has a different size")
        if (properties.metadata or {}).get("sha256") == digest:
            return
        hasher = hashlib.sha256()
        async for chunk in (await blob.download_blob()).chunks():
            hasher.update(chunk)
        if hasher.hexdigest() != digest:
            raise IntegrityError(f"Existing artifact {digest} does not match its digest")

    async def put_file(
        self, path: Path, *, media_type: str, schema_name: str | None = None
    ) -> ArtifactRef:
        from azure.core.exceptions import ResourceExistsError
        from azure.storage.blob import ContentSettings

        digest, size = await asyncio.to_thread(sha256_file, path)
        if size > self._max_bytes:
            raise InvalidInputError(f"Artifact of {size} bytes exceeds {self._max_bytes}")
        key = artifact_key(digest)
        blob = self._container.get_blob_client(key)
        try:
            with path.open("rb") as handle:
                await blob.upload_blob(
                    handle,
                    length=size,
                    overwrite=False,
                    max_concurrency=1,
                    content_settings=ContentSettings(content_type=media_type),
                    metadata={"sha256": digest},
                )
        except ResourceExistsError:
            await self._verify_existing(blob, digest, size)
        except Exception as error:
            raise (translate_azure_error(error) or error) from error
        if (await asyncio.to_thread(sha256_file, path))[0] != digest:
            raise IntegrityError(f"Local file {path.name} changed while it was uploaded")
        return ArtifactRef(
            sha256=digest, uri=key, size_bytes=size, media_type=media_type, schema_name=schema_name
        )

    async def download_to(self, ref: ArtifactRef, path: Path) -> None:
        from azure.core.exceptions import ResourceNotFoundError

        blob = self._container.get_blob_client(ref.uri)
        hasher = hashlib.sha256()
        size = 0
        try:
            downloader = await blob.download_blob()
            handle = await asyncio.to_thread(path.open, "wb")
            with handle:
                async for chunk in downloader.chunks():
                    size += len(chunk)
                    if size > ref.size_bytes:
                        break
                    hasher.update(chunk)
                    handle.write(chunk)
        except ResourceNotFoundError as error:
            raise IntegrityError(f"Artifact {ref.uri} is missing") from error
        except Exception as error:
            raise (translate_azure_error(error) or error) from error
        if size != ref.size_bytes or hasher.hexdigest() != ref.sha256:
            await asyncio.to_thread(path.unlink, True)
            raise IntegrityError(f"Artifact {ref.uri} failed digest verification")

    async def get(self, ref: ArtifactRef) -> bytes:
        from azure.core.exceptions import ResourceNotFoundError

        blob = self._container.get_blob_client(ref.uri)
        try:
            data: bytes = await (await blob.download_blob()).readall()
        except ResourceNotFoundError as error:
            raise IntegrityError(f"Artifact {ref.uri} is missing") from error
        except Exception as error:
            raise (translate_azure_error(error) or error) from error
        if len(data) != ref.size_bytes or sha256_hex(data) != ref.sha256:
            raise IntegrityError(f"Artifact {ref.uri} failed digest verification")
        return data
