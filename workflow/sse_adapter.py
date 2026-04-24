"""
Map LangGraph astream_events() → existing SSE event dict format.

LangGraph event               SSE event
─────────────────────────     ────────────────────────────────────────
on_chain_start  (node)    →   {"type": "agent_switch", "agent": ...}
on_chat_model_stream      →   {"type": "text_delta",   "agent": ..., "delta": ...}
on_tool_start             →   {"type": "tool_call",    "agent": ..., "tool": ...}
on_tool_end               →   {"type": "tool_result",  "agent": ..., "output": ...}
                              {"type": "chart",        "path": ...}  (when CHART_SAVED found)
on_chain_end (graph)      →   {"type": "done",         "output": report}

governance_check_node is shown in the UI as "sql_writer" so the activity feed
remains unchanged from the openai-agents version.
"""

import json
import os
import re
from pathlib import Path
from typing import AsyncIterator

NODE_TO_AGENT: dict[str, str] = {
    "data_explorer_node": "data_explorer",
    "sql_writer_node": "sql_writer",
    "governance_check_node": "sql_writer",
    "analyst_node": "analyst",
    "writer_node": "writer",
}

_CHART_PATTERN = re.compile(r"chart_[0-9a-f]{8,}\.png")


def _output_to_str(output) -> str:
    if output is None:
        return ""
    if isinstance(output, str):
        return output
    if hasattr(output, "content"):
        c = output.content
        if isinstance(c, list):
            return "".join(
                item.get("text", "") if isinstance(item, dict) else str(item)
                for item in c
            )
        return str(c)
    if isinstance(output, (dict, list)):
        return json.dumps(output)
    return str(output)


async def stream_as_sse(events: AsyncIterator) -> AsyncIterator[dict]:
    """Yield SSE event dicts from a LangGraph astream_events() iterator."""
    current_agent = "data_explorer"
    emitted_charts: set[str] = set()
    done_emitted = False
    final_report = ""

    async for event in events:
        event_type: str = event.get("event", "")
        name: str = event.get("name", "")
        data: dict = event.get("data", {})
        metadata: dict = event.get("metadata", {})
        langgraph_node: str = metadata.get("langgraph_node", "")

        # ── Agent switch ──────────────────────────────────────────────────────
        if event_type == "on_chain_start" and name in NODE_TO_AGENT:
            current_agent = NODE_TO_AGENT[name]
            yield {"type": "agent_switch", "agent": current_agent}

        # ── Streaming text delta ──────────────────────────────────────────────
        elif event_type == "on_chat_model_stream":
            chunk = data.get("chunk")
            if chunk is not None:
                content = getattr(chunk, "content", "") or ""
                if isinstance(content, list):
                    content = "".join(
                        c.get("text", "") if isinstance(c, dict) else str(c)
                        for c in content
                    )
                if content:
                    agent = NODE_TO_AGENT.get(langgraph_node, current_agent)
                    yield {"type": "text_delta", "agent": agent, "delta": content}

        # ── Tool call start ───────────────────────────────────────────────────
        elif event_type == "on_tool_start":
            agent = NODE_TO_AGENT.get(langgraph_node, current_agent)
            yield {"type": "tool_call", "agent": agent, "tool": name}

        # ── Tool call end ─────────────────────────────────────────────────────
        elif event_type == "on_tool_end":
            agent = NODE_TO_AGENT.get(langgraph_node, current_agent)
            output_str = _output_to_str(data.get("output"))

            # Chart detection — strategy 1: explicit CHART_SAVED: prefix
            chart_found = False
            for line in output_str.splitlines():
                if line.startswith("CHART_SAVED:"):
                    chart_path = line[len("CHART_SAVED:"):].strip()
                    if chart_path not in emitted_charts:
                        emitted_charts.add(chart_path)
                        yield {"type": "chart", "path": chart_path}
                    chart_found = True
                    break

            # Chart detection — strategy 2: regex scan for chart filename
            if not chart_found:
                charts_dir = Path(os.getenv("CHARTS_DIR", "output/charts"))
                for m in _CHART_PATTERN.finditer(output_str):
                    candidate = charts_dir / m.group()
                    key = str(candidate)
                    if candidate.exists() and key not in emitted_charts:
                        emitted_charts.add(key)
                        yield {"type": "chart", "path": key}
                        break

            yield {
                "type": "tool_result",
                "agent": agent,
                "output": output_str[:400] + ("..." if len(output_str) > 400 else ""),
            }

        # ── Graph / chain completion ──────────────────────────────────────────
        elif event_type == "on_chain_end":
            output = data.get("output", {})
            # The top-level graph emits on_chain_end with the final AnalysisState
            if isinstance(output, dict) and "report" in output and not done_emitted:
                final_report = output.get("report", "")
                done_emitted = True
                yield {"type": "done", "output": final_report}

    # Safety net: ensure done is always emitted
    if not done_emitted:
        yield {"type": "done", "output": final_report}
