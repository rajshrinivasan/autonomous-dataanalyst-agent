"""
LangGraph StateGraph for the Autonomous Data Analyst.

Topology:
    data_explorer → sql_writer → governance_check
                                       │
                         errors ───────┘ (loops back up to 3 times)
                         clean  ──────→ analyst → writer → END

The graph is compiled lazily (first call to get_graph()) so the async
checkpointer can be awaited at startup rather than at import time.
"""

from langgraph.graph import END, StateGraph

from workflow.nodes import (
    analyst_node,
    data_explorer_node,
    governance_check_node,
    sql_writer_node,
    writer_node,
)
from workflow.state import AnalysisState

_graph = None


def _route_governance(state: AnalysisState) -> str:
    lint = state.get("lint_result") or {}
    if lint.get("errors") and state.get("lint_revision_count", 0) < 3:
        return "sql_writer"
    return "analyst"


def _build_graph() -> StateGraph:
    g = StateGraph(AnalysisState)

    g.add_node("data_explorer_node", data_explorer_node)
    g.add_node("sql_writer_node", sql_writer_node)
    g.add_node("governance_check_node", governance_check_node)
    g.add_node("analyst_node", analyst_node)
    g.add_node("writer_node", writer_node)

    g.set_entry_point("data_explorer_node")

    g.add_edge("data_explorer_node", "sql_writer_node")
    g.add_edge("sql_writer_node", "governance_check_node")
    g.add_conditional_edges(
        "governance_check_node",
        _route_governance,
        {"sql_writer": "sql_writer_node", "analyst": "analyst_node"},
    )
    g.add_edge("analyst_node", "writer_node")
    g.add_edge("writer_node", END)

    return g


async def get_graph():
    """Return the compiled graph, initialising the checkpointer on first call."""
    global _graph
    if _graph is None:
        from workflow.checkpointer import get_checkpointer

        checkpointer = await get_checkpointer()
        compiled = _build_graph().compile(checkpointer=checkpointer)
        compiled.name = "AnalysisGraph"
        _graph = compiled
    return _graph
