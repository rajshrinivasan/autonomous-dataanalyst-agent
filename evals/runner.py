"""
Run one golden case through the analysis pipeline and return a structured EvalResult.

Usage (standalone):
    python -m evals.runner --case order_status_breakdown

Usage (in pytest):
    Imported by evals/test_evals.py via run_case().
"""
from __future__ import annotations

import asyncio
import sys
import time
from dataclasses import dataclass, field

from evals.cases.schema import GoldenCase


@dataclass
class EvalResult:
    case_id: str
    question: str
    elapsed_seconds: float = 0.0

    events: list[dict] = field(default_factory=list)
    agent_switches: list[str] = field(default_factory=list)

    # Extracted outputs
    chart_paths: list[str] = field(default_factory=list)
    report_text: str = ""
    insights_text: str = ""     # accumulated text_delta from analyst agent

    pipeline_completed: bool = False
    error_events: list[str] = field(default_factory=list)


async def run_case(case: GoldenCase, timeout_seconds: int = 180) -> EvalResult:
    """Run one golden case end-to-end. Never raises — errors go into result."""
    from orchestration import run_analysis

    result = EvalResult(case_id=case.id, question=case.question)
    start = time.monotonic()

    current_agent = ""
    insights_deltas: list[str] = []

    try:
        async with asyncio.timeout(timeout_seconds):
            async for event in run_analysis(case.question):
                result.events.append(event)
                t = event.get("type", "")

                if t == "agent_switch":
                    current_agent = event.get("agent", "")
                    result.agent_switches.append(current_agent)

                elif t == "text_delta":
                    if current_agent == "analyst":
                        insights_deltas.append(event.get("delta", ""))

                elif t == "chart":
                    path = event.get("path", "")
                    if path:
                        result.chart_paths.append(path)

                elif t == "done":
                    result.report_text = event.get("output", "")
                    result.pipeline_completed = True

                elif t == "error":
                    result.error_events.append(event.get("message", ""))

    except TimeoutError:
        result.error_events.append(f"Pipeline timed out after {timeout_seconds}s")
    except Exception as exc:
        result.error_events.append(f"Runner exception: {exc}")

    result.elapsed_seconds = time.monotonic() - start
    result.insights_text = "".join(insights_deltas)
    return result


# ---------------------------------------------------------------------------
# Standalone CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    import json as _json
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).parent.parent))

    from evals.cases.schema import (
        GoldenCase, SqlChecks, DataChecks, AnalystChecks, ReportChecks
    )

    def _load_cases() -> list[GoldenCase]:
        cases_path = Path(__file__).parent / "cases" / "golden_cases.json"
        raw = _json.loads(cases_path.read_text(encoding="utf-8"))
        result = []
        for item in raw["cases"]:
            result.append(GoldenCase(
                id=item["id"],
                description=item.get("description", ""),
                question=item["question"],
                tags=item.get("tags", []),
                sql_checks=SqlChecks(**item.get("sql_checks", {})),
                data_checks=DataChecks(**item.get("data_checks", {})),
                analyst_checks=AnalystChecks(**item.get("analyst_checks", {})),
                report_checks=ReportChecks(**item.get("report_checks", {})),
            ))
        return result

    parser = argparse.ArgumentParser(description="Run a single eval case")
    parser.add_argument("--case", required=True, help="Case ID from golden_cases.json")
    parser.add_argument("--no-llm", action="store_true", help="Skip LLM-as-judge scoring")
    args = parser.parse_args()

    all_cases = _load_cases()
    case = next((c for c in all_cases if c.id == args.case), None)
    if not case:
        ids = [c.id for c in all_cases]
        print(f"Case '{args.case}' not found.\nAvailable: {ids}")
        sys.exit(1)

    async def _main():
        from evals.scorer import score_result
        result = await run_case(case)
        score = await score_result(result, case, use_llm_judge=not args.no_llm)

        print(f"\n{'='*60}")
        print(f"EVAL: {case.id}")
        print(f"Elapsed: {result.elapsed_seconds:.1f}s  |  Completed: {result.pipeline_completed}")
        print(f"Overall pass: {score.overall_pass}")

        for stage in score.stages:
            print(f"\n  Stage: {stage.stage}")
            print(f"    Deterministic pass rate: {stage.deterministic_pass_rate:.0%}")
            if stage.llm_avg_score is not None:
                print(f"    LLM avg score: {stage.llm_avg_score:.2f}")
            for check in stage.checks:
                icon = "✓" if check.passed else "✗"
                score_str = f" [{check.score:.2f}]" if check.score is not None else ""
                print(f"    {icon} {check.name}{score_str}: {check.detail}")

        if result.error_events:
            print(f"\nErrors: {result.error_events}")

    asyncio.run(_main())
