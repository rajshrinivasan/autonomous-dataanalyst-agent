from typing import TypedDict


class AnalysisState(TypedDict):
    # Inputs
    question: str
    workspace_id: str | None
    datasource_id: str | None

    # Inter-node communication
    schema_summary: str
    sql: str
    lint_result: dict
    lint_revision_count: int

    # Analyst output
    query_results: list
    chart_path: str
    insights: str

    # Final output
    report: str

    # Metadata
    current_agent: str
    error: str | None
