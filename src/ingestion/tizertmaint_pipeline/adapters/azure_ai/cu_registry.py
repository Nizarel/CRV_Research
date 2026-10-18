"""Verify the reviewed analyzer registry against the live Content Understanding resource.

Workers never create or change analyzers. At startup the resource model defaults and every
expected analyzer are read back and compared (subset match) with the reviewed definitions. An
analyzer that is missing, not ready or different is left out of the catalog, so a job that
needs it fails with a configuration error instead of silently using another analyzer. Auth and
transport errors propagate: a worker that cannot verify does not start.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field, replace
from pathlib import Path

from ...domain.cu import AnalyzerRegistry, service_definition, subset_mismatches
from ...domain.errors import ConfigurationError, InvalidInputError
from ...ports.providers import ContentAnalysisProvider


@dataclass(frozen=True)
class RegistryCheck:
    catalog: AnalyzerRegistry
    problems: dict[str, list[str]] = field(default_factory=dict)

    @property
    def verified(self) -> list[str]:
        return sorted(self.catalog.profiles)


async def load_registry(path: Path) -> AnalyzerRegistry:
    text = await asyncio.to_thread(path.read_text, encoding="utf-8")
    return AnalyzerRegistry.load(json.loads(text))


async def verify_registry(
    client: ContentAnalysisProvider, registry: AnalyzerRegistry
) -> RegistryCheck:
    """Return a catalog holding only the analyzers that match the reviewed registry."""

    if client.api_version != registry.api_version:
        raise ConfigurationError(
            f"CU client API {client.api_version} differs from registry {registry.api_version}"
        )
    problems: dict[str, list[str]] = {}
    defaults = await client.defaults()
    mismatched = subset_mismatches(
        registry.deployments, defaults.get("modelDeployments") or {}, "$.modelDeployments"
    )
    if mismatched:
        problems["defaults"] = mismatched
        return RegistryCheck(replace(registry, profiles={}), problems)
    verified = {}
    for name, expected in registry.definitions.items():
        try:
            actual = await client.analyzer(name)
        except InvalidInputError as error:
            problems[name] = [str(error)]
            continue
        found = subset_mismatches(expected, service_definition(actual))
        status = actual.get("status")
        if status is not None and str(status).lower() != "ready":
            found.append(f"$.status={status}")
        if found:
            problems[name] = found
        else:
            verified[name] = registry.profiles[name]
    catalog = replace(registry, profiles=verified)
    for name in registry.prebuilt:
        try:
            catalog.add_prebuilt(name, await client.analyzer(name))
        except (InvalidInputError, ConfigurationError) as error:
            problems[name] = [str(error)]
    return RegistryCheck(catalog, problems)
