import re
from collections.abc import AsyncIterator

from azure.core.exceptions import ResourceNotFoundError
from azure.storage.blob.aio import ContainerClient

from crv_agent.ports.sources import SourceInfo, SourceNotFound

DIGEST = re.compile(r"[0-9a-f]{64}")


class BlobSources:
    """Source PDFs stored as ``crv/sources/<sha256>.pdf`` in the ingestion source container."""

    def __init__(self, container: ContainerClient) -> None:
        self.container = container

    @staticmethod
    def blob_name(digest: str) -> str:
        if not DIGEST.fullmatch(digest):
            raise SourceNotFound(digest)
        return f"crv/sources/{digest}.pdf"

    async def stat(self, digest: str) -> SourceInfo:
        blob = self.container.get_blob_client(self.blob_name(digest))
        try:
            properties = await blob.get_blob_properties()
        except ResourceNotFoundError as error:
            raise SourceNotFound(digest) from error
        return SourceInfo(size=int(properties.size), etag=str(properties.etag).strip('"'))

    async def read(self, digest: str, start: int, end: int) -> AsyncIterator[bytes]:
        blob = self.container.get_blob_client(self.blob_name(digest))
        try:
            download = await blob.download_blob(offset=start, length=end - start)
        except ResourceNotFoundError as error:
            raise SourceNotFound(digest) from error
        async for chunk in download.chunks():
            yield chunk
