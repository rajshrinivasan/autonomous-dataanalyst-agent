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


@pytest.fixture(scope="session")
def use_llm_judge(request) -> bool:
    return not request.config.getoption("--no-llm-judge", default=False)


@pytest.fixture(scope="session")
def save_results(request) -> bool:
    return request.config.getoption("--save-results", default=False)
