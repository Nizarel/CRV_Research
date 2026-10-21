from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from crv_agent.orchestration.context import ResearchContext
from crv_agent.orchestration.nodes.answer import answer
from crv_agent.orchestration.nodes.research import reconcile, research, understand
from crv_agent.orchestration.state import ResearchInput, ResearchState


def build_graph(
    checkpointer: BaseCheckpointSaver[str] | None = None,
) -> CompiledStateGraph[ResearchState, ResearchContext, ResearchInput, ResearchState]:
    builder = StateGraph(
        ResearchState, context_schema=ResearchContext, input_schema=ResearchInput,
    )
    builder.add_node("understand", understand)
    builder.add_node("research", research)
    builder.add_node("reconcile", reconcile)
    builder.add_node("answer", answer)
    builder.add_edge(START, "understand")
    builder.add_edge("understand", "research")
    builder.add_edge("research", "reconcile")
    builder.add_edge("reconcile", "answer")
    builder.add_edge("answer", END)
    return builder.compile(checkpointer=checkpointer)
