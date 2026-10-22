from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from langgraph.graph.state import CompiledStateGraph

from crv_agent.api.identity import Identity
from crv_agent.orchestration.context import ResearchContext
from crv_agent.orchestration.state import ResearchInput, ResearchState
from crv_agent.orchestration.workers import Worker
from crv_agent.ports.knowledge import Knowledge
from crv_agent.ports.models import Models
from crv_agent.ports.persistence import Persistence


@dataclass(frozen=True)
class AppServices:
    persistence: Persistence
    identity: Identity
    models: Models
    worker: Worker
    knowledge: Knowledge
    graph: CompiledStateGraph[ResearchState, ResearchContext, ResearchInput, ResearchState]
    search_release: str
    kg_release: str
    ready: Callable[[], Awaitable[None]]
    model_calls: int = 24
    turn_tokens: int = 300_000
    turn_seconds: float = 120
    facet_concurrency: int = 4
    retrieval_mode: Literal["kg", "search"] = "kg"
    # Evaluation only (loopback local owner): stream ledger and exposed passages per turn.
    diagnostics: bool = False
    repair_mode: Literal["recompose", "prune"] = "recompose"
