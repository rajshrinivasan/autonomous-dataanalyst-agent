"""
Orchestration — Magentic-style delegation via openai-agents agent.as_tool()
---------------------------------------------------------------------------

Each specialist (data_explorer, sql_writer, analyst, writer) is wrapped with
Agent.as_tool() so the manager can call them as tools, receive their output,
and continue orchestrating.  This gives true round-trip delegation — the
manager retains control throughout the workflow.

Public interface
  run_analysis(question) -> AsyncIterator[dict]
      Async generator yielding structured event dicts for SSE streaming.

CLI usage
  python orchestration.py "What are the top 5 product categories by revenue?"
"""

import asyncio
import os
import re
import sys
from pathlib import Path
from typing import AsyncIterator

from agents import Agent, Runner
from agents.mcp import MCPServerStdio
from agents.stream_events import (
    AgentUpdatedStreamEvent,
    RawResponsesStreamEvent,
    RunItemStreamEvent,
)
from dotenv import load_dotenv
from openai.types.responses import ResponseTextDeltaEvent

from tools import run_python_code

load_dotenv()

MODEL    = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
_INS_DIR = Path(__file__).parent / "instructions"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _load(name: str) -> str:
    """Read an instruction file from the instructions/ directory."""
    return (_INS_DIR / f"{name}.md").read_text(encoding="utf-8")


def _mcp_server(datasource_id: str | None = None) -> MCPServerStdio:
    """Return a configured stdio MCP server.

    When datasource_id is provided, launches warehouse_server.py which looks
    up the datasource record in Postgres and connects to the target engine.
    Falls back to the original mcp_server.py (SQLite hardcoded) otherwise.
    """
    if datasource_id:
        script = Path(__file__).parent / "mcp_servers" / "warehouse_server.py"
        args = ["--datasource-id", datasource_id]
    else:
        script = Path(__file__).parent / "mcp_server.py"
        args = []
    return MCPServerStdio(
        params={
            "command": sys.executable,
            "args":    [str(script)] + args,
            "cwd":     str(Path(__file__).parent),
        },
        cache_tools_list=True,
    )


# ---------------------------------------------------------------------------
# Agent factory
# ---------------------------------------------------------------------------
def build_team(sqlite_mcp: MCPServerStdio) -> Agent:
    """
    Construct all five agents.

    Specialists are wrapped with .as_tool() so the manager can call them and
    receive their output back — true magentic-style delegation.
    """
    data_explorer = Agent(
        name="data_explorer",
        model=MODEL,
        instructions=_load("data_explorer"),
        mcp_servers=[sqlite_mcp],
    )

    sql_writer = Agent(
        name="sql_writer",
        model=MODEL,
        instructions=_load("sql_writer"),
        mcp_servers=[sqlite_mcp],
    )

    analyst = Agent(
        name="analyst",
        model=MODEL,
        instructions=_load("analyst"),
        mcp_servers=[sqlite_mcp],
        tools=[run_python_code],
    )

    writer = Agent(
        name="writer",
        model=MODEL,
        instructions=_load("writer"),
    )

    manager = Agent(
        name="manager",
        model=MODEL,
        instructions=_load("manager"),
        tools=[
            data_explorer.as_tool(
                tool_name="call_data_explorer",
                tool_description=(
                    "Discover the database schema. "
                    "Pass the business question as the input string."
                ),
            ),
            sql_writer.as_tool(
                tool_name="call_sql_writer",
                tool_description=(
                    "Write a SQLite SELECT query. "
                    "Pass: the business question + the schema summary returned by call_data_explorer."
                ),
            ),
            analyst.as_tool(
                tool_name="call_analyst",
                tool_description=(
                    "Execute the SQL query, interpret results, and generate a chart. "
                    "Pass: the business question + the SQL from call_sql_writer + "
                    "a suggestion for chart type (bar, line, etc.)."
                ),
            ),
            writer.as_tool(
                tool_name="call_writer",
                tool_description=(
                    "Write the final narrative report. "
                    "Pass: the business question + key insights from call_analyst + "
                    "the chart filename if one was produced."
                ),
            ),
        ],
    )

    return manager


# ---------------------------------------------------------------------------
# Streaming event types yielded to callers
# ---------------------------------------------------------------------------
# Each dict has a "type" key; app.py converts these to SSE data: fields.
#
#  {"type": "agent_switch", "agent": str}
#  {"type": "text_delta",   "agent": str, "delta": str}
#  {"type": "tool_call",    "agent": str, "tool": str}
#  {"type": "tool_result",  "agent": str, "tool": str, "output": str}
#  {"type": "chart",        "path": str}
#  {"type": "done",         "output": str}
#  {"type": "error",        "message": str}


# ---------------------------------------------------------------------------
# Public: run_analysis
# ---------------------------------------------------------------------------
async def run_analysis(
    question: str,
    workspace_id: str | None = None,
    datasource_id: str | None = None,
) -> AsyncIterator[dict]:
    """
    Async generator.  Run the full magentic workflow for *question* and yield
    structured event dicts suitable for forwarding as SSE.
    """
    current_agent: str = "manager"
    emitted_charts: set[str] = set()   # deduplicate chart events

    try:
        async with _mcp_server(datasource_id=datasource_id) as sqlite_mcp:
            manager = build_team(sqlite_mcp)

            result = Runner.run_streamed(manager, question)

            async for event in result.stream_events():

                # ── Agent switch ─────────────────────────────────────────
                if isinstance(event, AgentUpdatedStreamEvent):
                    current_agent = event.new_agent.name
                    yield {"type": "agent_switch", "agent": current_agent}

                # ── Streaming text delta ─────────────────────────────────
                elif isinstance(event, RawResponsesStreamEvent):
                    raw = event.data
                    if isinstance(raw, ResponseTextDeltaEvent):
                        yield {
                            "type":  "text_delta",
                            "agent": current_agent,
                            "delta": raw.delta,
                        }

                # ── Tool calls / results ─────────────────────────────────
                elif isinstance(event, RunItemStreamEvent):
                    item = event.item

                    if item.type == "tool_call_item":
                        tool_name = getattr(item.raw_item, "name", "tool")
                        yield {
                            "type":  "tool_call",
                            "agent": current_agent,
                            "tool":  tool_name,
                        }

                    elif item.type == "tool_call_output_item":
                        # item.output may be a string or list of MCP content blocks
                        raw_out = item.output
                        if isinstance(raw_out, str):
                            output = raw_out
                        elif isinstance(raw_out, list):
                            output = " ".join(
                                (b.text if hasattr(b, "text") else str(b))
                                for b in raw_out
                            )
                        else:
                            output = str(raw_out) if raw_out else ""

                        # Detect chart files in the analyst's output (deduplicated).
                        # Strategy 1: explicit CHART_SAVED: prefix
                        chart_found = False
                        for line in output.splitlines():
                            if line.startswith("CHART_SAVED:"):
                                chart_path = line[len("CHART_SAVED:"):].strip()
                                if chart_path not in emitted_charts:
                                    emitted_charts.add(chart_path)
                                    yield {"type": "chart", "path": chart_path}
                                chart_found = True
                                break

                        if not chart_found:
                            # Strategy 2: regex scan for chart_<hex>.png anywhere in text
                            charts_dir = Path(os.getenv("CHARTS_DIR", "output/charts"))
                            for match in re.finditer(r"chart_[0-9a-f]{8,}\.png", output):
                                candidate = charts_dir / match.group()
                                key = str(candidate)
                                if candidate.exists() and key not in emitted_charts:
                                    emitted_charts.add(key)
                                    yield {"type": "chart", "path": key}
                                    break

                        yield {
                            "type":   "tool_result",
                            "agent":  current_agent,
                            "output": output[:400] + ("..." if len(output) > 400 else ""),
                        }

            # ── Final synthesised output ─────────────────────────────────
            yield {"type": "done", "output": result.final_output}

    except Exception as exc:
        yield {"type": "error", "message": str(exc)}


# ---------------------------------------------------------------------------
# CLI runner — for local testing without the web UI
# ---------------------------------------------------------------------------
async def _cli_main(question: str) -> None:
    RESET  = "\033[0m"
    BOLD   = "\033[1m"
    CYAN   = "\033[36m"
    YELLOW = "\033[33m"
    GREEN  = "\033[32m"
    GREY   = "\033[90m"

    print(f"\n{BOLD}Question:{RESET} {question}\n{'-' * 60}")

    async for event in run_analysis(question):
        t = event["type"]

        if t == "agent_switch":
            print(f"\n{CYAN}{BOLD}[{event['agent'].upper()}]{RESET}")

        elif t == "text_delta":
            print(event["delta"], end="", flush=True)

        elif t == "tool_call":
            print(f"\n  {YELLOW}>> {event['tool']}(){RESET}", flush=True)

        elif t == "tool_result":
            snippet = event["output"].replace("\n", " ")[:160]
            print(f"  {GREY}  {snippet}{RESET}", flush=True)

        elif t == "chart":
            print(f"\n  {GREEN}Chart saved: {event['path']}{RESET}")

        elif t == "done":
            print(f"\n\n{'=' * 60}\n{BOLD}FINAL OUTPUT{RESET}\n{'=' * 60}")
            print(event["output"])

        elif t == "error":
            print(f"\n[ERROR] {event['message']}", file=sys.stderr)


if __name__ == "__main__":
    # Force UTF-8 output so agent text (which may contain non-ASCII chars) prints cleanly.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    if len(sys.argv) < 2:
        q = "What are the top 5 product categories by total revenue, and how does each perform on gross margin?"
    else:
        q = " ".join(sys.argv[1:])

    asyncio.run(_cli_main(q))
