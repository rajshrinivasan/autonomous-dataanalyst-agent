"""
Layer 2: Integration Tests — full LangGraph graph with mocked LLM and MCP.

Verifies the graph wires together correctly and that routing logic (including
the governance feedback loop) behaves as expected — without any real API calls.
"""
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage

sys.path.insert(0, str(Path(__file__).parent.parent))

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CLEAN_LINT = json.dumps({"passes": True, "errors": [], "warnings": []})
ERROR_LINT = json.dumps({"passes": False, "errors": ["Forbidden keyword DELETE"], "warnings": []})

INITIAL_STATE = {
    "question": "What are the top products by revenue?",
    "workspace_id": None,
    "datasource_id": None,
    "schema_summary": "",
    "sql": "",
    "lint_result": {},
    "lint_revision_count": 0,
    "query_results": [],
    "chart_path": "",
    "insights": "",
    "report": "",
    "current_agent": "",
    "error": None,
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_model(responses: list[AIMessage]) -> MagicMock:
    """Mock ChatOpenAI that returns AIMessage objects from a pre-scripted list.

    Uses MagicMock (not AsyncMock) so that bind_tools() returns a sync value
    rather than a coroutine — ChatOpenAI.bind_tools is a regular method call.
    """
    it = iter(responses)

    async def _ainvoke(messages, config=None):
        return next(it)

    model = MagicMock()
    model.bind_tools.return_value = model
    model.ainvoke = _ainvoke
    return model


def _make_lint_tool(responses: list[str]) -> AsyncMock:
    """Mock lint_sql MCP tool returning pre-scripted JSON strings."""
    it = iter(responses)
    tool = AsyncMock()
    tool.name = "lint_sql"

    async def _ainvoke(args, config=None):
        return next(it)

    tool.ainvoke = _ainvoke
    return tool


def _make_mcp_cm(lint_tool: AsyncMock):
    """Return an async context manager that yields a client with get_tools() = [lint_tool]."""
    client = MagicMock()
    client.get_tools.return_value = [lint_tool]
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=client)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


# ---------------------------------------------------------------------------
# Fixture: compiled graph using MemorySaver (no real DB needed)
# ---------------------------------------------------------------------------

@pytest.fixture()
def compiled_graph():
    from langgraph.checkpoint.memory import MemorySaver
    from workflow.graph import _build_graph
    return _build_graph().compile(checkpointer=MemorySaver())


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

async def test_graph_happy_path_produces_report(compiled_graph):
    model = _make_model([
        AIMessage(content="Schema: products, orders tables with revenue data"),  # data_explorer
        AIMessage(content="```sql\nSELECT id, name FROM products\n```"),         # sql_writer
        # governance uses lint_tool, not LLM
        AIMessage(content="- Revenue is $100K\n- Top product: Monitor"),         # analyst
        AIMessage(content="## Executive Summary\nRevenue is strong."),           # writer
    ])
    lint_tool = _make_lint_tool([CLEAN_LINT])
    mcp_cm = _make_mcp_cm(lint_tool)

    with patch("workflow.nodes.ChatOpenAI", return_value=model), \
         patch("workflow.nodes.MultiServerMCPClient", return_value=mcp_cm):
        state = await compiled_graph.ainvoke(
            INITIAL_STATE, config={"configurable": {"thread_id": "happy-1"}}
        )

    assert state["sql"] == "SELECT id, name FROM products"
    assert "Revenue" in state["insights"]
    assert state["report"].startswith("## Executive Summary")
    assert state["lint_revision_count"] == 0


async def test_graph_final_state_contains_all_pipeline_keys(compiled_graph):
    model = _make_model([
        AIMessage(content="Schema summary"),
        AIMessage(content="```sql\nSELECT 1\n```"),
        AIMessage(content="- Insight 1\n- Insight 2"),
        AIMessage(content="## Report\nFindings here."),
    ])
    lint_tool = _make_lint_tool([CLEAN_LINT])
    mcp_cm = _make_mcp_cm(lint_tool)

    with patch("workflow.nodes.ChatOpenAI", return_value=model), \
         patch("workflow.nodes.MultiServerMCPClient", return_value=mcp_cm):
        state = await compiled_graph.ainvoke(
            INITIAL_STATE, config={"configurable": {"thread_id": "keys-1"}}
        )

    for key in ("report", "sql", "insights", "schema_summary", "lint_result", "lint_revision_count"):
        assert key in state, f"Missing key in final state: {key}"


async def test_graph_sql_extracted_from_code_fence(compiled_graph):
    model = _make_model([
        AIMessage(content="Schema"),
        AIMessage(content="```sql\nSELECT name, price FROM products ORDER BY price DESC\n```\nThis query fetches products."),
        AIMessage(content="- Price insight"),
        AIMessage(content="## Report"),
    ])
    lint_tool = _make_lint_tool([CLEAN_LINT])
    mcp_cm = _make_mcp_cm(lint_tool)

    with patch("workflow.nodes.ChatOpenAI", return_value=model), \
         patch("workflow.nodes.MultiServerMCPClient", return_value=mcp_cm):
        state = await compiled_graph.ainvoke(
            INITIAL_STATE, config={"configurable": {"thread_id": "sql-fence-1"}}
        )

    assert state["sql"] == "SELECT name, price FROM products ORDER BY price DESC"


async def test_graph_governance_retry_once_increments_count(compiled_graph):
    model = _make_model([
        AIMessage(content="Schema summary"),
        AIMessage(content="```sql\nDELETE FROM products\n```"),  # sql_writer 1st (bad)
        AIMessage(content="```sql\nSELECT id FROM products\n```"),  # sql_writer retry
        AIMessage(content="- Key insight here"),
        AIMessage(content="## Executive Summary\nGood results."),
    ])
    lint_tool = _make_lint_tool([ERROR_LINT, CLEAN_LINT])
    mcp_cm = _make_mcp_cm(lint_tool)

    with patch("workflow.nodes.ChatOpenAI", return_value=model), \
         patch("workflow.nodes.MultiServerMCPClient", return_value=mcp_cm):
        state = await compiled_graph.ainvoke(
            INITIAL_STATE, config={"configurable": {"thread_id": "retry-1"}}
        )

    assert state["lint_revision_count"] == 1
    assert state["sql"] == "SELECT id FROM products"
    assert state["report"]


async def test_graph_governance_max_retries_caps_at_3(compiled_graph):
    model = _make_model([
        AIMessage(content="Schema summary"),
        AIMessage(content="SELECT bad_1 FROM products"),  # sql_writer 1st
        AIMessage(content="SELECT bad_2 FROM products"),  # sql_writer retry 1
        AIMessage(content="SELECT bad_3 FROM products"),  # sql_writer retry 2
        AIMessage(content="- Insights produced despite persistent lint errors"),
        AIMessage(content="## Summary\nCompleted despite governance errors."),
    ])
    lint_tool = _make_lint_tool([ERROR_LINT, ERROR_LINT, ERROR_LINT])
    mcp_cm = _make_mcp_cm(lint_tool)

    with patch("workflow.nodes.ChatOpenAI", return_value=model), \
         patch("workflow.nodes.MultiServerMCPClient", return_value=mcp_cm):
        state = await compiled_graph.ainvoke(
            INITIAL_STATE, config={"configurable": {"thread_id": "max-retry-1"}}
        )

    assert state["lint_revision_count"] == 3
    assert state["report"], "Graph must complete even when governance keeps erroring"


async def test_graph_clean_governance_does_not_increment_count(compiled_graph):
    model = _make_model([
        AIMessage(content="Schema"),
        AIMessage(content="```sql\nSELECT 1\n```"),
        AIMessage(content="- Insight"),
        AIMessage(content="## Report"),
    ])
    lint_tool = _make_lint_tool([CLEAN_LINT])
    mcp_cm = _make_mcp_cm(lint_tool)

    with patch("workflow.nodes.ChatOpenAI", return_value=model), \
         patch("workflow.nodes.MultiServerMCPClient", return_value=mcp_cm):
        state = await compiled_graph.ainvoke(
            INITIAL_STATE, config={"configurable": {"thread_id": "no-retry-1"}}
        )

    assert state["lint_revision_count"] == 0
