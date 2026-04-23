"""
Unit tests for workflow/sse_adapter.py::stream_as_sse().

Creates async event sequences and asserts the correct SSE event dicts
are produced.  No LLM calls, no MCP servers.
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from workflow.sse_adapter import stream_as_sse


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _collect(events: list[dict]) -> list[dict]:
    """Run stream_as_sse over a list of events and collect all SSE dicts."""
    async def _gen():
        for e in events:
            yield e
    return [e async for e in stream_as_sse(_gen())]


def _chain_start(name: str, langgraph_node: str = "") -> dict:
    return {
        "event": "on_chain_start",
        "name": name,
        "data": {},
        "metadata": {"langgraph_node": langgraph_node or name},
    }


def _stream_chunk(content, langgraph_node: str = "sql_writer_node") -> dict:
    return {
        "event": "on_chat_model_stream",
        "name": "ChatOpenAI",
        "data": {"chunk": SimpleNamespace(content=content)},
        "metadata": {"langgraph_node": langgraph_node},
    }


def _tool_start(tool_name: str, langgraph_node: str = "analyst_node") -> dict:
    return {
        "event": "on_tool_start",
        "name": tool_name,
        "data": {},
        "metadata": {"langgraph_node": langgraph_node},
    }


def _tool_end(output: str, langgraph_node: str = "analyst_node") -> dict:
    return {
        "event": "on_tool_end",
        "name": "run_query",
        "data": {"output": output},
        "metadata": {"langgraph_node": langgraph_node},
    }


def _chain_end(output: dict) -> dict:
    return {
        "event": "on_chain_end",
        "name": "AnalysisGraph",
        "data": {"output": output},
        "metadata": {},
    }


# ---------------------------------------------------------------------------
# Agent switch
# ---------------------------------------------------------------------------

async def test_agent_switch_on_chain_start():
    results = await _collect([_chain_start("data_explorer_node")])
    switches = [e for e in results if e["type"] == "agent_switch"]
    assert len(switches) == 1
    assert switches[0]["agent"] == "data_explorer"


async def test_governance_node_maps_to_sql_writer_agent():
    results = await _collect([_chain_start("governance_check_node")])
    switches = [e for e in results if e["type"] == "agent_switch"]
    assert switches[0]["agent"] == "sql_writer"


@pytest.mark.parametrize("node_name,expected_agent", [
    ("data_explorer_node", "data_explorer"),
    ("sql_writer_node", "sql_writer"),
    ("governance_check_node", "sql_writer"),
    ("analyst_node", "analyst"),
    ("writer_node", "writer"),
])
async def test_all_known_nodes_produce_agent_switch(node_name, expected_agent):
    results = await _collect([_chain_start(node_name)])
    switches = [e for e in results if e["type"] == "agent_switch"]
    assert switches[0]["agent"] == expected_agent


async def test_unknown_node_does_not_produce_agent_switch():
    results = await _collect([_chain_start("unknown_node_xyz")])
    switches = [e for e in results if e["type"] == "agent_switch"]
    assert len(switches) == 0


# ---------------------------------------------------------------------------
# Text delta
# ---------------------------------------------------------------------------

async def test_text_delta_from_chat_model_stream():
    results = await _collect([_stream_chunk("hello")])
    deltas = [e for e in results if e["type"] == "text_delta"]
    assert len(deltas) == 1
    assert deltas[0]["delta"] == "hello"


async def test_text_delta_empty_content_not_emitted():
    results = await _collect([_stream_chunk("")])
    deltas = [e for e in results if e["type"] == "text_delta"]
    assert len(deltas) == 0


async def test_text_delta_list_content_joined():
    chunk = SimpleNamespace(content=[{"text": "part1"}, {"text": "part2"}])
    results = await _collect([_stream_chunk(chunk.content)])
    deltas = [e for e in results if e["type"] == "text_delta"]
    assert deltas[0]["delta"] == "part1part2"


async def test_text_delta_none_chunk_not_emitted():
    event = {
        "event": "on_chat_model_stream",
        "name": "ChatOpenAI",
        "data": {"chunk": None},
        "metadata": {"langgraph_node": "sql_writer_node"},
    }
    results = await _collect([event])
    deltas = [e for e in results if e["type"] == "text_delta"]
    assert len(deltas) == 0


async def test_text_delta_agent_from_langgraph_node():
    results = await _collect([_stream_chunk("hi", langgraph_node="writer_node")])
    deltas = [e for e in results if e["type"] == "text_delta"]
    assert deltas[0]["agent"] == "writer"


# ---------------------------------------------------------------------------
# Tool call
# ---------------------------------------------------------------------------

async def test_tool_call_start_emitted():
    results = await _collect([_tool_start("run_query")])
    calls = [e for e in results if e["type"] == "tool_call"]
    assert len(calls) == 1
    assert calls[0]["tool"] == "run_query"
    assert calls[0]["agent"] == "analyst"


async def test_tool_call_agent_from_langgraph_node():
    results = await _collect([_tool_start("get_schema", langgraph_node="data_explorer_node")])
    calls = [e for e in results if e["type"] == "tool_call"]
    assert calls[0]["agent"] == "data_explorer"


# ---------------------------------------------------------------------------
# Tool result
# ---------------------------------------------------------------------------

async def test_tool_result_emitted():
    results = await _collect([_tool_end("result data")])
    tool_results = [e for e in results if e["type"] == "tool_result"]
    assert len(tool_results) == 1
    assert tool_results[0]["output"] == "result data"


async def test_tool_result_truncated_at_400_chars():
    long_output = "x" * 500
    results = await _collect([_tool_end(long_output)])
    output = [e for e in results if e["type"] == "tool_result"][0]["output"]
    assert output.endswith("...")
    assert len(output) == 403  # 400 chars + "..."


async def test_tool_result_not_truncated_at_exactly_400_chars():
    exact_output = "x" * 400
    results = await _collect([_tool_end(exact_output)])
    output = [e for e in results if e["type"] == "tool_result"][0]["output"]
    assert not output.endswith("...")
    assert len(output) == 400


# ---------------------------------------------------------------------------
# Chart detection
# ---------------------------------------------------------------------------

async def test_chart_detected_via_chart_saved_prefix():
    output = "CHART_SAVED:/output/charts/chart_abc12345.png\nsome other output"
    results = await _collect([_tool_end(output)])
    charts = [e for e in results if e["type"] == "chart"]
    assert len(charts) == 1
    assert charts[0]["path"] == "/output/charts/chart_abc12345.png"


async def test_chart_event_precedes_tool_result():
    output = "CHART_SAVED:/output/charts/chart_abc12345.png"
    results = await _collect([_tool_end(output)])
    types = [e["type"] for e in results]
    chart_idx = types.index("chart")
    result_idx = types.index("tool_result")
    assert chart_idx < result_idx


async def test_chart_not_duplicated_across_two_tool_ends():
    output = "CHART_SAVED:/output/charts/chart_abc12345.png"
    results = await _collect([_tool_end(output), _tool_end(output)])
    charts = [e for e in results if e["type"] == "chart"]
    assert len(charts) == 1


async def test_chart_not_emitted_for_regular_output():
    results = await _collect([_tool_end("plain query results, no chart here")])
    charts = [e for e in results if e["type"] == "chart"]
    assert len(charts) == 0


# ---------------------------------------------------------------------------
# Done event
# ---------------------------------------------------------------------------

async def test_done_event_on_chain_end_with_report():
    results = await _collect([_chain_end({"report": "final report text"})])
    done_events = [e for e in results if e["type"] == "done"]
    assert len(done_events) == 1
    assert done_events[0]["output"] == "final report text"


async def test_safety_net_done_emitted_on_empty_stream():
    results = await _collect([])
    assert len(results) == 1
    assert results[0]["type"] == "done"
    assert results[0]["output"] == ""


async def test_safety_net_done_emitted_when_no_chain_end():
    results = await _collect([
        _chain_start("data_explorer_node"),
        _stream_chunk("some streaming text"),
    ])
    assert results[-1]["type"] == "done"


async def test_done_has_empty_output_when_chain_end_lacks_report():
    # on_chain_end without "report" key → safety net fires with empty output
    results = await _collect([_chain_end({})])
    done_events = [e for e in results if e["type"] == "done"]
    assert len(done_events) == 1
    assert done_events[0]["output"] == ""


async def test_done_not_duplicated():
    results = await _collect([_chain_end({"report": "text"})])
    done_events = [e for e in results if e["type"] == "done"]
    assert len(done_events) == 1


async def test_unknown_event_type_produces_only_safety_net_done():
    results = await _collect([
        {"event": "on_custom_unknown", "name": "x", "data": {}, "metadata": {}},
    ])
    types = [e["type"] for e in results]
    assert len(results) == 1
    assert results[0]["type"] == "done"
