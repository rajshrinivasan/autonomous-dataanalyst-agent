"""
End-to-end eval suite.

Run all cases (deterministic + LLM judge):
    pytest evals/ -m eval -v --tb=short

Run without LLM judge (no API cost):
    pytest evals/ -m eval --no-llm-judge -v

Run one case:
    pytest evals/ -m eval -k order_status_breakdown -v

Save JSON results to evals/results/:
    pytest evals/ -m eval --save-results -v
"""
import json
import os
import warnings
from datetime import datetime
from pathlib import Path

import pytest

from evals.cases.schema import GoldenCase
from evals.conftest import ALL_CASES
from evals.runner import EvalResult, run_case
from evals.scorer import EvalScore, score_result

pytestmark = [
    pytest.mark.eval,
    pytest.mark.skipif(
        not os.getenv("OPENAI_API_KEY"),
        reason="OPENAI_API_KEY not set — skipping eval suite",
    ),
]


@pytest.mark.parametrize("case", ALL_CASES, ids=[c.id for c in ALL_CASES])
async def test_golden_case(
    case: GoldenCase,
    use_llm_judge: bool,
    save_results: bool,
) -> None:
    result = await run_case(case)
    score = await score_result(result, case, use_llm_judge=use_llm_judge)

    if save_results:
        _persist_result(result, score)

    # Deterministic failures → hard fail
    failures = [
        f"  [{s.stage}] {c.name}: {c.detail}"
        for s in score.stages
        for c in s.checks
        if not c.passed and c.score is None
    ]

    # LLM scores below threshold → soft warning only
    low_scores = [
        f"  [{s.stage}] {c.name}: score={c.score:.2f} — {c.detail}"
        for s in score.stages
        for c in s.checks
        if c.score is not None and c.score < 0.6
    ]

    if low_scores:
        warnings.warn(
            f"Case '{case.id}' has low LLM quality scores:\n" + "\n".join(low_scores),
            UserWarning,
            stacklevel=2,
        )

    assert not failures, (
        f"\nCase '{case.id}' failed {len(failures)} deterministic check(s):\n"
        + "\n".join(failures)
    )


def _persist_result(result: EvalResult, score: EvalScore) -> None:
    results_dir = Path(__file__).parent / "results"
    results_dir.mkdir(exist_ok=True)
    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%S")
    out = results_dir / f"{result.case_id}_{ts}.json"
    data = {
        "case_id": result.case_id,
        "elapsed_seconds": result.elapsed_seconds,
        "pipeline_completed": result.pipeline_completed,
        "error_events": result.error_events,
        "chart_paths": result.chart_paths,
        "overall_pass": score.overall_pass,
        "stages": [
            {
                "stage": s.stage,
                "deterministic_pass_rate": s.deterministic_pass_rate,
                "llm_avg_score": s.llm_avg_score,
                "checks": [
                    {
                        "name": c.name,
                        "passed": c.passed,
                        "detail": c.detail,
                        "score": c.score,
                    }
                    for c in s.checks
                ],
            }
            for s in score.stages
        ],
    }
    out.write_text(json.dumps(data, indent=2), encoding="utf-8")
