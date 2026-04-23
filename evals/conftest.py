import json
from pathlib import Path

import pytest

from evals.cases.schema import (
    AnalystChecks,
    DataChecks,
    GoldenCase,
    ReportChecks,
    SqlChecks,
)


def _load_cases() -> list[GoldenCase]:
    cases_path = Path(__file__).parent / "cases" / "golden_cases.json"
    raw = json.loads(cases_path.read_text(encoding="utf-8"))
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


ALL_CASES = _load_cases()


def pytest_addoption(parser):
    parser.addoption(
        "--no-llm-judge",
        action="store_true",
        default=False,
        help="Skip LLM-as-judge scoring (deterministic checks only, no API cost)",
    )
    parser.addoption(
        "--save-results",
        action="store_true",
        default=False,
        help="Save eval result JSON files to evals/results/",
    )
    parser.addoption(
        "--optimize",
        action="store_true",
        default=False,
        help="After eval run, trigger the bootstrap optimizer on evals/results/",
    )


@pytest.fixture(scope="session")
def use_llm_judge(request) -> bool:
    return not request.config.getoption("--no-llm-judge", default=False)


@pytest.fixture(scope="session")
def save_results(request) -> bool:
    return request.config.getoption("--save-results", default=False)


def pytest_sessionfinish(session, exitstatus):
    """After the eval session, run the bootstrap optimizer when --optimize is set."""
    if not session.config.getoption("--optimize", default=False):
        return
    try:
        from evals.optimizer import run_optimizer
        modified = run_optimizer()
        if modified:
            print(f"\n[optimizer] Updated instruction files: {modified}")
            print("[optimizer] Re-run evals to measure improvement.")
        else:
            print("\n[optimizer] No instruction files needed updating.")
    except Exception as exc:
        print(f"\n[optimizer] Optimizer failed: {exc}")
