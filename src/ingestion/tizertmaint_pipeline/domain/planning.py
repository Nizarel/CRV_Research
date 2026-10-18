"""Build ``JobSpec``s with the right pool, stage version and failure budget."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .errors import ConfigurationError
from .models import JobSpec
from .states import DEFAULT_STAGE_POOLS, StageName, WorkerPool


class JobPlanner:
    """Stage versions are part of every fingerprint, so a new handler version means new jobs."""

    def __init__(
        self,
        *,
        versions: Mapping[StageName, str],
        pools: Mapping[StageName, WorkerPool] = DEFAULT_STAGE_POOLS,
        max_failures: int = 5,
    ) -> None:
        self._versions = dict(versions)
        self._pools = dict(pools)
        self._max_failures = max_failures

    def spec(
        self,
        stage: StageName,
        *,
        unit_id: str,
        inputs: dict[str, Any] | None = None,
        **fields: Any,
    ) -> JobSpec:
        version = self._versions.get(stage)
        if version is None:
            raise ConfigurationError(f"No handler version registered for stage {stage.value}")
        fields.setdefault("max_failures", self._max_failures)
        return JobSpec.create(
            stage=stage,
            pool=self._pools[stage],
            unit_id=unit_id,
            stage_version=version,
            inputs=inputs,
            **fields,
        )
