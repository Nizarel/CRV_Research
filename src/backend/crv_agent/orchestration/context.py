from dataclasses import dataclass
from typing import Literal

from crv_agent.domain.execution import Budget
from crv_agent.orchestration.workers import Worker
from crv_agent.ports.knowledge import Knowledge
from crv_agent.ports.models import Models


@dataclass(frozen=True)
class ResearchContext:
    models: Models
    knowledge: Knowledge
    worker: Worker
    budget: Budget
    search_release: str
    kg_release: str
    trace_id: str
    retrieval_mode: Literal["kg", "search"] = "kg"
