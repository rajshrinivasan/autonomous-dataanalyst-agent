"""
LangGraph node functions — one per specialist agent.

Each node:
  1. Loads its system prompt from instructions/<name>.md
  2. Opens a MultiServerMCPClient for the MCP servers it needs
  3. Runs a ChatOpenAI agent loop (model → tool calls → results → model …)
  4. Returns only the state keys it updates (partial dict)

The RunnableConfig received by each node carries LangGraph's callback context,
so streaming events (on_chat_model_stream, on_tool_start/end) are automatically
captured by astream_events() in the caller.
"""

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_openai import ChatOpenAI

from tools import _execute_python_code
from workflow.state import AnalysisState

_INS_DIR = Path(__file__).parent.parent / "instructions"
_MCP_DIR = Path(__file__).parent.parent / "mcp_servers"
_ROOT = Path(__file__).parent.parent


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _model() -> str:
    return os.getenv("OPENAI_MODEL", "gpt-4o-mini")


def _load_instruction(name: str) -> str:
    return (_INS_DIR / f"{name}.md").read_text(encoding="utf-8")


def _warehouse_config(state: AnalysisState) -> dict:
    """Build stdio MCP config for the warehouse (or legacy SQLite) server."""
    datasource_id = state.get("datasource_id")
    workspace_id = state.get("workspace_id")
    if datasource_id:
        script = _MCP_DIR / "warehouse_server.py"
        args = [str(script), "--datasource-id", datasource_id]
        if workspace_id:
            args += ["--workspace-id", workspace_id]
    else:
        script = _ROOT / "mcp_server.py"
        args = [str(script)]
    return {
        "transport": "stdio",
        "command": sys.executable,
        "args": args,
        "cwd": str(_ROOT),
    }


def _governance_config() -> dict:
    """Build stdio MCP config for the stateless governance server."""
    return {
        "transport": "stdio",
        "command": sys.executable,
        "args": [str(_MCP_DIR / "governance_server.py")],
        "cwd": str(_ROOT),
    }


@tool
def run_python_code_tool(code: str) -> str:
    """Execute Python matplotlib/pandas code in a Docker sandbox.

    The variable _CHART_PATH is pre-injected — use it to save the figure.
    Returns CHART_SAVED:<path> if a chart was successfully produced.
    """
    return _execute_python_code(code)


async def _agent_loop(
    model: Any,
    messages: list,
    tool_by_name: dict,
    config: RunnableConfig,
) -> tuple[str, str]:
    """Run the ChatOpenAI tool-calling loop until the model stops calling tools.

    Returns (final_text_content, chart_path_if_any).
    Passes config to every model and tool invocation so LangGraph's callback
    system can capture streaming and tool events.
    """
    chart_path = ""

    while True:
        response = await model.ainvoke(messages, config=config)
        messages.append(response)

        if not response.tool_calls:
            return response.content or "", chart_path

        for tc in response.tool_calls:
            name = tc["name"]
            args = tc["args"]
            tool_obj = tool_by_name.get(name)
            if tool_obj is not None:
                result = await tool_obj.ainvoke(args, config=config)
            else:
                result = f"Unknown tool: {name}"

            result_str = str(result)

            for line in result_str.splitlines():
                if line.startswith("CHART_SAVED:"):
                    chart_path = line[len("CHART_SAVED:"):].strip()
                    break

            messages.append(
                ToolMessage(content=result_str, tool_call_id=tc["id"])
            )


# ---------------------------------------------------------------------------
# Node: data_explorer
# ---------------------------------------------------------------------------

async def data_explorer_node(state: AnalysisState, config: RunnableConfig) -> dict:
    messages = [
        SystemMessage(content=_load_instruction("data_explorer")),
        HumanMessage(content=state["question"]),
    ]

    client = MultiServerMCPClient({"warehouse": _warehouse_config(state)})
    tools = await client.get_tools()
    model = ChatOpenAI(model=_model(), streaming=True).bind_tools(tools)
    tool_by_name = {t.name: t for t in tools}
    content, _ = await _agent_loop(model, messages, tool_by_name, config)

    return {"schema_summary": content}


# ---------------------------------------------------------------------------
# Node: sql_writer
# ---------------------------------------------------------------------------

def _extract_sql(text: str) -> str:
    """Pull the raw SQL out of a markdown-fenced or plain response."""
    m = re.search(r"```(?:sql)?\s*\n(.*?)\n```", text, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()
    m = re.search(r"(SELECT\b.*?)(?:;?\s*$)", text, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return text.strip()


async def sql_writer_node(state: AnalysisState, config: RunnableConfig) -> dict:
    context_parts = [
        f"Question: {state['question']}",
        f"Schema:\n{state.get('schema_summary', '')}",
    ]

    lint_result = state.get("lint_result") or {}
    if lint_result.get("errors"):
        context_parts.append(
            f"Previous SQL:\n{state.get('sql', '')}\n\n"
            f"Lint errors to fix: {'; '.join(lint_result['errors'])}"
        )

    messages = [
        SystemMessage(content=_load_instruction("sql_writer")),
        HumanMessage(content="\n\n".join(context_parts)),
    ]

    server_config = {
        "warehouse": _warehouse_config(state),
        "governance": _governance_config(),
    }
    client = MultiServerMCPClient(server_config)
    tools = await client.get_tools()
    model = ChatOpenAI(model=_model(), streaming=True).bind_tools(tools)
    tool_by_name = {t.name: t for t in tools}
    content, _ = await _agent_loop(model, messages, tool_by_name, config)

    return {"sql": _extract_sql(content)}


# ---------------------------------------------------------------------------
# Node: governance_check
# ---------------------------------------------------------------------------

async def governance_check_node(state: AnalysisState, config: RunnableConfig) -> dict:
    """Call lint_sql via MCP; no LLM involved — direct tool invocation."""
    sql = state.get("sql", "")
    datasource_id = state.get("datasource_id") or "default"

    client = MultiServerMCPClient({"governance": _governance_config()})
    tools = await client.get_tools()
    lint_tool = next((t for t in tools if t.name == "lint_sql"), None)

    if lint_tool is None:
        lint_result: dict = {"passes": True, "warnings": [], "errors": []}
    else:
        raw = await lint_tool.ainvoke(
            {"sql": sql, "datasource_id": datasource_id},
            config=config,
        )
        raw_str = raw if isinstance(raw, str) else getattr(raw, "content", str(raw))
        try:
            lint_result = json.loads(raw_str)
        except (json.JSONDecodeError, TypeError):
            lint_result = {"passes": True, "warnings": [], "errors": []}

    revision_count = state.get("lint_revision_count", 0)
    if lint_result.get("errors"):
        revision_count += 1

    return {"lint_result": lint_result, "lint_revision_count": revision_count}


# ---------------------------------------------------------------------------
# Node: analyst
# ---------------------------------------------------------------------------

async def analyst_node(state: AnalysisState, config: RunnableConfig) -> dict:
    lint_result = state.get("lint_result") or {}
    context_parts = [
        f"Question: {state['question']}",
        f"SQL query:\n{state.get('sql', '')}",
        "Chart suggestion: use a bar chart for categories/rankings, "
        "a line chart for time series.",
    ]
    if lint_result.get("warnings"):
        context_parts.append(
            f"Data caveats (mention in your response): "
            f"{'; '.join(lint_result['warnings'])}"
        )

    messages = [
        SystemMessage(content=_load_instruction("analyst")),
        HumanMessage(content="\n\n".join(context_parts)),
    ]

    server_config = {"warehouse": _warehouse_config(state)}
    client = MultiServerMCPClient(server_config)
    mcp_tools = await client.get_tools()
    all_tools = mcp_tools + [run_python_code_tool]
    model = ChatOpenAI(model=_model(), streaming=True).bind_tools(all_tools)
    tool_by_name = {t.name: t for t in all_tools}
    content, chart_path = await _agent_loop(model, messages, tool_by_name, config)

    return {
        "insights": content,
        "chart_path": chart_path,
        "query_results": [],
    }


# ---------------------------------------------------------------------------
# Node: writer
# ---------------------------------------------------------------------------

async def writer_node(state: AnalysisState, config: RunnableConfig) -> dict:
    context_parts = [
        f"Question: {state['question']}",
        f"Key insights:\n{state.get('insights', '')}",
    ]
    if state.get("chart_path"):
        context_parts.append(f"Chart filename: {Path(state['chart_path']).name}")

    lint_result = state.get("lint_result") or {}
    if lint_result.get("warnings"):
        context_parts.append(
            f"Data caveats to include: {'; '.join(lint_result['warnings'])}"
        )

    messages = [
        SystemMessage(content=_load_instruction("writer")),
        HumanMessage(content="\n\n".join(context_parts)),
    ]

    model = ChatOpenAI(model=_model(), streaming=True)
    response = await model.ainvoke(messages, config=config)

    return {"report": response.content or ""}
