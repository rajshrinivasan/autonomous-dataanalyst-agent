"""
Orchestration — LangGraph-based analysis pipeline
--------------------------------------------------

Public interface
  run_analysis(question, workspace_id, datasource_id) -> AsyncIterator[dict]
      Async generator yielding structured event dicts for SSE streaming.
      Identical event format to the previous openai-agents implementation so
      app.py and static/index.html require zero changes.

CLI usage
  python orchestration.py "What are the top 5 product categories by revenue?"
"""

import asyncio
import sys
import uuid
from typing import AsyncIterator

from dotenv import load_dotenv

load_dotenv()


# ---------------------------------------------------------------------------
# Public: run_analysis
# ---------------------------------------------------------------------------

async def run_analysis(
    question: str,
    workspace_id: str | None = None,
    datasource_id: str | None = None,
) -> AsyncIterator[dict]:
    """
    Async generator.  Runs the LangGraph analysis workflow for *question* and
    yields structured event dicts for SSE streaming.

    Event types (identical to the previous openai-agents version):
      {"type": "agent_switch", "agent": str}
      {"type": "text_delta",   "agent": str, "delta": str}
      {"type": "tool_call",    "agent": str, "tool": str}
      {"type": "tool_result",  "agent": str, "output": str}
      {"type": "chart",        "path": str}
      {"type": "done",         "output": str}
      {"type": "error",        "message": str}
    """
    from workflow.graph import get_graph
    from workflow.sse_adapter import stream_as_sse

    initial_state = {
        "question": question,
        "workspace_id": workspace_id,
        "datasource_id": datasource_id,
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

    config = {"configurable": {"thread_id": str(uuid.uuid4())}}

    try:
        graph = await get_graph()
        events = graph.astream_events(initial_state, config=config, version="v2")
        async for sse_event in stream_as_sse(events):
            yield sse_event

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
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    if len(sys.argv) < 2:
        q = "What are the top 5 product categories by total revenue, and how does each perform on gross margin?"
    else:
        q = " ".join(sys.argv[1:])

    asyncio.run(_cli_main(q))
