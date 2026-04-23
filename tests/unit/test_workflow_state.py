"""
Unit tests for workflow/state.py — AnalysisState TypedDict schema.

Pure Python, no async, no mocks.  Validates the state schema matches
the initial_state dict in orchestration.py and that all expected keys exist.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from workflow.state import AnalysisState

_ANN = AnalysisState.__annotations__

EXPECTED_KEYS = {
    # Inputs
    "question", "workspace_id", "datasource_id",
    # Inter-node
    "schema_summary", "sql", "lint_result", "lint_revision_count",
    # Analyst output
    "query_results", "chart_path", "insights",
    # Final output
    "report",
    # Metadata
    "current_agent", "error",
}


def test_input_keys_present():
    for key in ("question", "workspace_id", "datasource_id"):
        assert key in _ANN, f"Missing input key: {key}"


def test_inter_node_keys_present():
    for key in ("schema_summary", "sql", "lint_result", "lint_revision_count"):
        assert key in _ANN, f"Missing inter-node key: {key}"


def test_analyst_output_keys_present():
    for key in ("query_results", "chart_path", "insights"):
        assert key in _ANN, f"Missing analyst output key: {key}"


def test_final_output_and_metadata_keys_present():
    for key in ("report", "current_agent", "error"):
        assert key in _ANN, f"Missing output/metadata key: {key}"


def test_total_key_count():
    """Exact count catches accidental additions or removals."""
    assert len(_ANN) == 13, (
        f"Expected 13 keys, got {len(_ANN)}: {sorted(_ANN)}"
    )


def test_lint_result_annotation_is_dict():
    assert _ANN["lint_result"] is dict


def test_lint_revision_count_annotation_is_int():
    assert _ANN["lint_revision_count"] is int


def test_query_results_annotation_is_list():
    assert _ANN["query_results"] is list


def test_no_unexpected_keys():
    unexpected = set(_ANN) - EXPECTED_KEYS
    assert not unexpected, f"Unexpected keys in AnalysisState: {unexpected}"


def test_initial_state_shape_matches_orchestration():
    """Replicate orchestration.py's initial_state — catches key drift."""
    initial_state = {
        "question": "test question",
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
    schema_keys = set(_ANN)
    state_keys = set(initial_state)
    assert schema_keys == state_keys, (
        f"Schema/initial_state mismatch — "
        f"in schema only: {schema_keys - state_keys}, "
        f"in initial_state only: {state_keys - schema_keys}"
    )
