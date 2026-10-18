"""Byte- and count-bounded Search uploads with per-key outcomes and visibility checks.

Batches stay far below the 16 MB request limit on purpose: ``azure-search-documents``
12.0.0 has a broken automatic split-on-413 path, so a 413 must never be provoked.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from ...domain.batching import pack
from ...domain.errors import AuthError, InvalidInputError, ThrottledError, TransientError
from ...domain.projection import ItemDisposition, ProjectionReport, classify_item
from ..azure_errors import translate_azure_error

SEARCH_KEY = re.compile(r"^(?!_)[A-Za-z0-9_\-=]{1,1024}$")
VERIFY_KEYS_PER_QUERY = 50


def _serialized_size(document: Mapping[str, Any]) -> int:
    return len(json.dumps(document, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


class AzureSearchProjector:
    """``SearchProjector`` over ``azure.search.documents.aio.SearchClient``."""

    def __init__(self, client: Any, *, max_batch_documents: int, max_batch_bytes: int) -> None:
        self._client = client
        self._max_documents = max_batch_documents
        self._max_bytes = max_batch_bytes

    async def upload(
        self, documents: Sequence[Mapping[str, Any]], *, key_field: str
    ) -> ProjectionReport:
        keys = [str(document.get(key_field, "")) for document in documents]
        invalid = [key for key in keys if not SEARCH_KEY.match(key)]
        if invalid:
            raise InvalidInputError(f"{len(invalid)} document key(s) are missing or malformed")
        if len(set(keys)) != len(keys):
            raise InvalidInputError("Duplicate document keys in one projection request")

        packed = pack(
            [_serialized_size(document) for document in documents],
            max_items=self._max_documents,
            max_total=self._max_bytes,
        )
        report = ProjectionReport(
            permanent={
                keys[index]: "document exceeds the batch byte limit" for index in packed.oversized
            }
        )
        for batch in packed.batches:
            report = report.merge(
                await self._upload_batch(
                    [documents[index] for index in batch], [keys[i] for i in batch]
                )
            )
        return report

    async def _upload_batch(
        self, documents: list[Mapping[str, Any]], keys: list[str]
    ) -> ProjectionReport:
        try:
            results = await self._client.upload_documents(documents=documents)
        except Exception as error:
            translated = translate_azure_error(error)
            if translated is None or isinstance(translated, AuthError):
                raise (translated or error) from error
            if isinstance(translated, TransientError | ThrottledError):
                return ProjectionReport(retryable=dict.fromkeys(keys, str(translated)))
            return ProjectionReport(permanent=dict.fromkeys(keys, str(translated)))

        accepted: list[str] = []
        retryable: dict[str, str] = {}
        permanent: dict[str, str] = {}
        for result in results:
            disposition = classify_item(
                succeeded=bool(result.succeeded), status_code=int(result.status_code)
            )
            reason = f"HTTP {result.status_code}: {result.error_message or 'no message'}"
            if disposition is ItemDisposition.ACCEPTED:
                accepted.append(result.key)
            elif disposition is ItemDisposition.RETRYABLE:
                retryable[result.key] = reason
            else:
                permanent[result.key] = reason
        reported = set(accepted) | set(retryable) | set(permanent)
        for key in keys:
            if key not in reported:
                retryable[key] = "no per-document result returned"
        return ProjectionReport(tuple(accepted), retryable, permanent)

    async def delete(self, keys: Sequence[str], *, key_field: str) -> ProjectionReport:
        # A key the index refuses can never have been stored, so it is already absent.
        report = ProjectionReport(accepted=tuple(key for key in keys if not SEARCH_KEY.match(key)))
        keys = [key for key in keys if SEARCH_KEY.match(key)]
        for start in range(0, len(keys), self._max_documents):
            chunk = list(keys[start : start + self._max_documents])
            try:
                results = await self._client.delete_documents(
                    documents=[{key_field: key} for key in chunk]
                )
            except Exception as error:
                translated = translate_azure_error(error)
                if translated is None or isinstance(translated, AuthError):
                    raise (translated or error) from error
                reasons = dict.fromkeys(chunk, str(translated))
                if isinstance(translated, TransientError | ThrottledError):
                    report = report.merge(ProjectionReport(retryable=reasons))
                else:
                    report = report.merge(ProjectionReport(permanent=reasons))
                continue
            accepted: list[str] = []
            retryable: dict[str, str] = {}
            permanent: dict[str, str] = {}
            for result in results:
                code = int(result.status_code)
                # Deleting an absent key is a success: the row is gone either way.
                if code == 404 or classify_item(
                    succeeded=bool(result.succeeded), status_code=code
                ) is ItemDisposition.ACCEPTED:
                    accepted.append(result.key)
                elif classify_item(succeeded=False, status_code=code) is ItemDisposition.RETRYABLE:
                    retryable[result.key] = f"HTTP {code}: {result.error_message or 'no message'}"
                else:
                    permanent[result.key] = f"HTTP {code}: {result.error_message or 'no message'}"
            for key in chunk:
                if key not in accepted and key not in retryable and key not in permanent:
                    retryable[key] = "no per-document result returned"
            report = report.merge(ProjectionReport(tuple(accepted), retryable, permanent))
        return report

    async def verify(
        self, expected: Mapping[str, str], *, key_field: str, digest_field: str
    ) -> dict[str, str]:
        """Query back each key; acknowledgement can precede query visibility."""

        keys = list(expected)
        if any(not SEARCH_KEY.match(key) for key in keys):
            raise InvalidInputError("Malformed key in verification request")
        found: dict[str, str | None] = {}
        for start in range(0, len(keys), VERIFY_KEYS_PER_QUERY):
            chunk = keys[start : start + VERIFY_KEYS_PER_QUERY]
            try:
                results = await self._client.search(
                    search_text="*",
                    filter=f"search.in({key_field}, '{'|'.join(chunk)}', '|')",
                    select=[key_field, digest_field],
                    top=len(chunk),
                )
                async for result in results:
                    found[str(result[key_field])] = result.get(digest_field)
            except Exception as error:
                raise (translate_azure_error(error) or error) from error
        problems: dict[str, str] = {}
        for key, digest in expected.items():
            if key not in found:
                problems[key] = "missing"
            elif found[key] != digest:
                problems[key] = "digest_mismatch"
        return problems
