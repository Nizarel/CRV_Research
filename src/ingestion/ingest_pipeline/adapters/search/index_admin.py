"""Create or update the reviewed evidence index from its JSON definition (REST, Entra auth).

The schema file is applied as reviewed: only ``${...}`` placeholders are substituted, every
placeholder must be supplied, and the index name must match the configured target.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ...domain.errors import ConfigurationError, IntegrityError

API_VERSION = "2025-09-01"
SCOPE = "https://search.azure.com/.default"
_PLACEHOLDER = re.compile(r"\$\{([A-Z0-9_]+)\}")


def render_schema(path: Path, values: Mapping[str, str], *, index_name: str) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    missing = sorted(set(_PLACEHOLDER.findall(text)) - set(values))
    if missing:
        raise ConfigurationError(f"Missing index schema values: {', '.join(missing)}")
    for key, value in values.items():
        if not value or any(char in value for char in '"\\\r\n'):
            raise ConfigurationError(f"Invalid value for {key}")
        text = text.replace("${" + key + "}", value)
    schema: dict[str, Any] = json.loads(text)
    if schema.get("name") != index_name:
        raise ConfigurationError(
            f"Schema defines index {schema.get('name')!r}, configured target is {index_name!r}"
        )
    return schema


def schema_drift(expected: Mapping[str, Any], actual: Mapping[str, Any]) -> list[str]:
    """Field differences that matter for projection: names, types, keys and vector sizes."""

    def shape(document: Mapping[str, Any]) -> dict[str, tuple[Any, ...]]:
        return {
            str(item["name"]): (
                item.get("type"), bool(item.get("key")), item.get("dimensions"),
                item.get("vectorSearchProfile"),
            )
            for item in document.get("fields") or ()
        }

    want, have = shape(expected), shape(actual)
    problems = [f"missing field {name}" for name in sorted(set(want) - set(have))]
    problems += [f"unexpected field {name}" for name in sorted(set(have) - set(want))]
    problems += [
        f"field {name} differs: {have[name]} != {want[name]}"
        for name in sorted(set(want) & set(have)) if want[name] != have[name]
    ]
    return problems


async def ensure_index(
    endpoint: str, schema: Mapping[str, Any], credential: Any, *, apply: bool
) -> dict[str, Any]:
    """Compare the deployed index with ``schema``; with ``apply`` create or update it first."""

    import aiohttp

    name = str(schema["name"])
    url = f"{endpoint.rstrip('/')}/indexes('{name}')?api-version={API_VERSION}"
    token = await credential.get_token(SCOPE)
    headers = {"Authorization": f"Bearer {token.token}", "Content-Type": "application/json"}
    timeout = aiohttp.ClientTimeout(total=120)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        if apply:
            async with session.put(
                url, headers={**headers, "Prefer": "return=representation"}, json=schema
            ) as response:
                if response.status not in (200, 201, 204):
                    raise IntegrityError(
                        f"Index update failed ({response.status}): {(await response.text())[:500]}"
                    )
        async with session.get(url, headers=headers) as response:
            if response.status == 404:
                return {"index": name, "exists": False, "applied": apply, "drift": []}
            if response.status != 200:
                raise IntegrityError(
                    f"Index read failed ({response.status}): {(await response.text())[:500]}"
                )
            deployed = await response.json()
    drift = schema_drift(schema, deployed)
    return {"index": name, "exists": True, "applied": apply, "drift": drift}
