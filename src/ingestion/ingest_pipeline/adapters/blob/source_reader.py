"""Read one exact source version: properties by name, bytes pinned by ETag."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from pydantic import ValidationError

from ...domain.errors import InvalidInputError, StaleSourceError
from ...domain.models import SourceProvenance
from ...ports.storage import SourceProperties
from ..azure_errors import translate_azure_error


class BlobSourceReader:
    """``SourceReader`` over an ``azure.storage.blob.aio.ContainerClient``."""

    def __init__(self, container: Any) -> None:
        self._container = container

    async def properties(self, blob_name: str) -> SourceProperties:
        from azure.core.exceptions import ResourceNotFoundError

        blob = self._container.get_blob_client(blob_name)
        try:
            properties = await blob.get_blob_properties()
        except ResourceNotFoundError as error:
            raise InvalidInputError(f"Source blob not found: {blob_name}") from error
        except Exception as error:
            raise (translate_azure_error(error) or error) from error
        settings = getattr(properties, "content_settings", None)
        raw_provenance = (properties.metadata or {}).get("ingest_provenance")
        try:
            provenance = (
                SourceProvenance.model_validate_json(raw_provenance) if raw_provenance else None
            )
        except ValidationError as error:
            raise InvalidInputError(f"Invalid source provenance for {blob_name}") from error
        return SourceProperties(
            container_name=self._container.container_name,
            container_url=self._container.url,
            blob_name=blob_name,
            etag=properties.etag,
            size_bytes=int(properties.size),
            version_id=getattr(properties, "version_id", None),
            content_type=getattr(settings, "content_type", None),
            last_modified=getattr(properties, "last_modified", None),
            declared_content_hash=(properties.metadata or {}).get("content_hash"),
            provenance=provenance,
        )

    async def stream(self, blob_name: str, *, etag: str) -> AsyncIterator[bytes]:
        from azure.core import MatchConditions
        from azure.core.exceptions import ResourceModifiedError, ResourceNotFoundError

        blob = self._container.get_blob_client(blob_name)
        try:
            downloader = await blob.download_blob(
                etag=etag, match_condition=MatchConditions.IfNotModified, max_concurrency=1
            )
            async for chunk in downloader.chunks():
                yield chunk
        except ResourceModifiedError as error:
            raise StaleSourceError(f"Source {blob_name} changed after {etag}") from error
        except ResourceNotFoundError as error:
            raise StaleSourceError(f"Source {blob_name} was removed after {etag}") from error
        except Exception as error:
            raise (translate_azure_error(error) or error) from error
