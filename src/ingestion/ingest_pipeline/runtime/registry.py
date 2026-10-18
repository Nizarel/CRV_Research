"""Stage handler registry; the source of stage versions for job fingerprints."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from ..domain.errors import ConfigurationError
from ..domain.planning import JobPlanner
from ..domain.states import DEFAULT_STAGE_POOLS, StageName, WorkerPool
from ..stages.base import StageHandler


@dataclass(frozen=True)
class StageRegistration:
    stage: StageName
    handler: StageHandler
    timeout_seconds: float | None = None


class StageRegistry:
    def __init__(self, pools: Mapping[StageName, WorkerPool] = DEFAULT_STAGE_POOLS) -> None:
        self._pools = dict(pools)
        self._registrations: dict[StageName, StageRegistration] = {}

    def register(
        self, stage: StageName, handler: StageHandler, *, timeout_seconds: float | None = None
    ) -> StageRegistry:
        if stage in self._registrations:
            raise ConfigurationError(f"Stage {stage.value} is already registered")
        self._registrations[stage] = StageRegistration(stage, handler, timeout_seconds)
        return self

    def get(self, stage: StageName) -> StageRegistration:
        registration = self._registrations.get(stage)
        if registration is None:
            raise ConfigurationError(f"No handler registered for stage {stage.value}")
        return registration

    def pool_for(self, stage: StageName) -> WorkerPool:
        return self._pools[stage]

    def planner(self, *, max_failures: int) -> JobPlanner:
        return JobPlanner(
            versions={stage: reg.handler.version for stage, reg in self._registrations.items()},
            pools=self._pools,
            max_failures=max_failures,
        )
