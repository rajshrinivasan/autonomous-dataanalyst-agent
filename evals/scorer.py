"""
Two-layer scoring system for eval results.

Layer 1 — Deterministic (free, always runs):
  SQL structural checks via regex
  SQL execution against data/sample.db
  Data property checks (row count, column presence, value constraints)
  Chart presence
  Report section presence + word count

Layer 2 — LLM-as-judge (~$0.001/case with gpt-4o-mini, runs after Layer 1):
  SQL semantic quality
  Insight quality
  Report quality

Only deterministic check failures hard-fail pytest. LLM scores < 0.6 are
soft warnings printed but not raised as assertion errors.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from evals.cases.schema import GoldenCase
from evals.runner import EvalResult

_DB_PATH = Path(__file__).parent.parent / "data" / "sample.db"
_FORBIDDEN_DML = re.compile(
    r"\b(DELETE|UPDATE|INSERT|DROP|ALTER|CREATE|TRUNCATE|MERGE)\b",
    re.IGNORECASE,
)


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str = ""
    score: float | None = None   # set only for LLM-graded checks


@dataclass
class StageScore:
    stage: str
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def deterministic_pass_rate(self) -> float:
        det = [c for c in self.checks if c.score is None]
        if not det:
            return 1.0
        return sum(1 for c in det if c.passed) / len(det)

    @property
    def llm_avg_score(self) -> float | None:
        llm = [c for c in self.checks if c.score is not None]
        if not llm:
            return None
        return sum(c.score for c in llm) / len(llm)


@dataclass
class EvalScore:
    case_id: str
    stages: list[StageScore] = field(default_factory=list)
    overall_pass: bool = False


# ---------------------------------------------------------------------------
# SQL extraction from the event stream
# ---------------------------------------------------------------------------

def extract_sql(result: EvalResult) -> str:
    """Reconstruct the SQL query from sql_writer text_delta events.

    Tracks agent_switch events and collects text_delta content while the
    sql_writer agent is active, then applies the same extraction regex used
    in workflow/nodes.py::_extract_sql().
    If the sql_writer runs multiple times (governance revision loop), returns
    the last SQL block found.
    """
    capturing = False
    segments: list[list[str]] = [[]]   # one inner list per sql_writer activation

    for event in result.events:
        t = event.get("type", "")
        if t == "agent_switch":
            if event.get("agent") == "sql_writer":
                capturing = True
                segments.append([])
            else:
                capturing = False
        elif capturing and t == "text_delta":
            segments[-1].append(event.get("delta", ""))

    # Try each segment in reverse; return the first (last in time) valid SQL
    for seg in reversed(segments):
        text = "".join(seg)
        m = re.search(r"```(?:sql)?\s*\n(.*?)\n```", text, re.DOTALL | re.IGNORECASE)
        if m:
            return m.group(1).strip()
        m = re.search(r"(SELECT\b.*?)(?:;?\s*$)", text, re.DOTALL | re.IGNORECASE)
        if m:
            return m.group(1).strip()

    return ""


# ---------------------------------------------------------------------------
# Layer 1: Deterministic scorers
# ---------------------------------------------------------------------------

def _score_sql(result: EvalResult, case: GoldenCase) -> StageScore:
    checks: list[CheckResult] = []
    sql = extract_sql(result)
    c = case.sql_checks

    # DML/DDL guard
    has_dml = bool(_FORBIDDEN_DML.search(sql))
    checks.append(CheckResult("no_dml_ddl", not has_dml,
                               "Clean" if not has_dml else f"Forbidden keyword in SQL: {sql[:80]}"))

    # Required tables
    for table in c.must_contain_tables:
        found = bool(re.search(rf"\b{re.escape(table)}\b", sql, re.IGNORECASE))
        checks.append(CheckResult(f"table_{table}", found,
                                   f"Table '{table}' {'found' if found else 'MISSING'} in SQL"))

    # Required columns
    for col in c.must_contain_columns:
        found = bool(re.search(rf"\b{re.escape(col)}\b", sql, re.IGNORECASE))
        checks.append(CheckResult(f"column_{col}", found,
                                   f"Column '{col}' {'found' if found else 'MISSING'} in SQL"))

    # Structural patterns
    if c.must_use_aggregation:
        ok = bool(re.search(r"\b(SUM|COUNT|AVG|MIN|MAX)\b", sql, re.IGNORECASE))
        checks.append(CheckResult("uses_aggregation", ok,
                                   "Aggregation present" if ok else "No aggregation found"))

    if c.must_use_join:
        ok = bool(re.search(r"\bJOIN\b", sql, re.IGNORECASE))
        checks.append(CheckResult("uses_join", ok,
                                   "JOIN present" if ok else "No JOIN found"))

    if c.must_use_where:
        ok = bool(re.search(r"\bWHERE\b", sql, re.IGNORECASE))
        checks.append(CheckResult("uses_where", ok,
                                   "WHERE present" if ok else "No WHERE found"))

    if c.must_use_group_by:
        ok = bool(re.search(r"\bGROUP\s+BY\b", sql, re.IGNORECASE))
        checks.append(CheckResult("uses_group_by", ok,
                                   "GROUP BY present" if ok else "No GROUP BY found"))

    if c.must_use_strftime:
        ok = bool(re.search(r"\bstrftime\b", sql, re.IGNORECASE))
        checks.append(CheckResult("uses_strftime", ok,
                                   "strftime() present" if ok else "strftime() MISSING"))

    return StageScore(stage="sql", checks=checks)


def _score_data(result: EvalResult, case: GoldenCase) -> StageScore:
    """Re-execute the extracted SQL against the real DB and check data properties."""
    checks: list[CheckResult] = []
    c = case.data_checks
    sql = extract_sql(result)

    if not sql:
        checks.append(CheckResult("sql_extractable", False, "No SQL found in event stream"))
        return StageScore(stage="data", checks=checks)

    # Execute against the real (read-only) database
    try:
        conn = sqlite3.connect(f"file:{_DB_PATH.as_posix()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        cur = conn.execute(sql)
        rows = cur.fetchmany(60)
        col_names = [d[0] for d in cur.description] if cur.description else []
        data = [dict(row) for row in rows]
        conn.close()
        query_error = None
    except Exception as exc:
        query_error = str(exc)
        data = []
        col_names = []

    checks.append(CheckResult("query_executes", query_error is None,
                               query_error or "Executed successfully"))

    if query_error:
        return StageScore(stage="data", checks=checks)

    # Column presence
    for col in c.expected_columns_subset:
        found = any(col.lower() in cn.lower() or cn.lower() in col.lower()
                    for cn in col_names)
        checks.append(CheckResult(f"column_{col}", found,
                                   f"Column '{col}' {'present' if found else 'MISSING'} (got: {col_names})"))

    # Row counts
    n = len(data)
    if c.row_count_exact is not None:
        checks.append(CheckResult("row_count_exact", n == c.row_count_exact,
                                   f"Got {n} rows, expected {c.row_count_exact}"))
    if c.row_count_min is not None:
        checks.append(CheckResult("row_count_min", n >= c.row_count_min,
                                   f"Got {n} rows, expected >= {c.row_count_min}"))

    # First row value (substring match)
    if c.first_row_contains and data:
        for col, substring in c.first_row_contains.items():
            actual = str(data[0].get(col, ""))
            passed = substring.lower() in actual.lower()
            checks.append(CheckResult("first_row_contains", passed,
                                       f"First row '{col}': '{actual}' {'contains' if passed else 'does NOT contain'} '{substring}'"))

    # Numeric column range
    if c.numeric_column and data:
        col = _find_col(col_names, c.numeric_column)
        if col:
            vals = [float(row[col]) for row in data
                    if row.get(col) is not None and _is_numeric(row[col])]
            if c.numeric_min is not None and vals:
                ok = all(v >= c.numeric_min for v in vals)
                checks.append(CheckResult("numeric_min", ok,
                                           f"All '{col}' values >= {c.numeric_min}: min={min(vals):.1f}"))
            if c.numeric_max is not None and vals:
                ok = all(v <= c.numeric_max for v in vals)
                checks.append(CheckResult("numeric_max", ok,
                                           f"All '{col}' values <= {c.numeric_max}: max={max(vals):.1f}"))

    # Any numeric column exceeds threshold
    if c.any_numeric_gt is not None and data:
        found_any = False
        for row in data:
            for val in row.values():
                if _is_numeric(val) and float(val) > c.any_numeric_gt:
                    found_any = True
                    break
        checks.append(CheckResult("any_numeric_gt", found_any,
                                   f"Some value > {c.any_numeric_gt}: {found_any}"))

    # Allowed column values
    if c.allowed_column_values and data:
        for col, allowed in c.allowed_column_values.items():
            actual_col = _find_col(col_names, col)
            if actual_col:
                bad = [str(row[actual_col]) for row in data
                       if str(row.get(actual_col, "")) not in allowed]
                checks.append(CheckResult(f"allowed_values_{col}", len(bad) == 0,
                                           f"Unexpected values in '{col}': {bad[:5]}" if bad else f"All values in allowed set"))

    # Column pattern match
    if c.column_pattern and data:
        for col, pattern in c.column_pattern.items():
            actual_col = _find_col(col_names, col)
            if actual_col:
                mismatches = [str(row[actual_col]) for row in data
                              if not re.match(pattern, str(row.get(actual_col, "")))]
                checks.append(CheckResult(f"column_pattern_{col}", len(mismatches) == 0,
                                           f"Pattern '{pattern}' mismatches: {mismatches[:3]}" if mismatches else f"All '{col}' values match pattern"))

    return StageScore(stage="data", checks=checks)


def _score_analyst(result: EvalResult, case: GoldenCase) -> StageScore:
    checks: list[CheckResult] = []
    c = case.analyst_checks

    if c.chart_required:
        has_chart = len(result.chart_paths) > 0
        checks.append(CheckResult("chart_generated", has_chart,
                                   f"Chart paths: {result.chart_paths}" if has_chart else "No chart event received"))

    # Count bullet points in insights
    bullets = [line for line in result.insights_text.split("\n")
               if line.strip() and line.strip()[0] in "-*•123456789"]
    ok = len(bullets) >= c.min_insights
    checks.append(CheckResult("insight_count", ok,
                               f"{len(bullets)} bullets found (need {c.min_insights})"))

    for term in c.insights_must_mention:
        found = term.lower() in result.insights_text.lower()
        checks.append(CheckResult(f"insights_mention_{term}", found,
                                   f"'{term}' {'found' if found else 'MISSING'} in insights"))

    return StageScore(stage="analyst", checks=checks)


def _score_report(result: EvalResult, case: GoldenCase) -> StageScore:
    checks: list[CheckResult] = []
    c = case.report_checks
    report = result.report_text

    checks.append(CheckResult("pipeline_completed", result.pipeline_completed,
                               "done event received" if result.pipeline_completed else "Pipeline did not complete"))
    checks.append(CheckResult("no_error_events", len(result.error_events) == 0,
                               "; ".join(result.error_events) if result.error_events else "No errors"))

    for section in c.required_sections:
        found = section.lower() in report.lower()
        checks.append(CheckResult(f"section_{section.lower().replace(' ', '_')}", found,
                                   f"Section '{section}' {'present' if found else 'MISSING'}"))

    word_count = len(report.split())
    checks.append(CheckResult("word_count_ok", word_count <= c.max_words,
                               f"{word_count} words (limit: {c.max_words})"))

    if c.must_mention_numbers:
        has_nums = bool(re.search(r"\d[\d,.]*", report))
        checks.append(CheckResult("report_has_numbers", has_nums,
                                   "Numbers present" if has_nums else "No numbers found in report"))

    return StageScore(stage="report", checks=checks)


# ---------------------------------------------------------------------------
# Layer 2: LLM-as-judge
# ---------------------------------------------------------------------------

async def _llm_judge(result: EvalResult, case: GoldenCase) -> list[StageScore]:
    from evals.judge_prompts import (
        SQL_QUALITY_PROMPT, INSIGHT_QUALITY_PROMPT, REPORT_QUALITY_PROMPT
    )
    import openai

    client = openai.AsyncOpenAI()
    sql = extract_sql(result)
    stages: list[StageScore] = []

    async def judge(prompt: str, stage_name: str, check_name: str) -> StageScore:
        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=200,
        )
        raw = resp.choices[0].message.content or ""
        m = re.search(r"SCORE:\s*([0-9.]+)", raw)
        score = max(0.0, min(1.0, float(m.group(1)))) if m else 0.5
        rationale = re.sub(r"SCORE:\s*[0-9.]+\s*", "", raw).replace("RATIONALE:", "").strip()
        passed = score >= 0.6
        return StageScore(stage=stage_name, checks=[
            CheckResult(check_name, passed, rationale, score=score)
        ])

    if sql:
        stages.append(await judge(
            SQL_QUALITY_PROMPT.format(question=case.question, sql=sql),
            "sql_quality", "sql_semantic_correctness",
        ))

    if result.insights_text:
        stages.append(await judge(
            INSIGHT_QUALITY_PROMPT.format(
                question=case.question,
                insights=result.insights_text[:2000],
            ),
            "analyst_quality", "insight_quality",
        ))

    if result.report_text:
        stages.append(await judge(
            REPORT_QUALITY_PROMPT.format(
                question=case.question,
                report=result.report_text[:3000],
            ),
            "report_quality", "report_answers_question",
        ))

    return stages


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def score_result(
    result: EvalResult,
    case: GoldenCase,
    use_llm_judge: bool = True,
) -> EvalScore:
    score = EvalScore(case_id=case.id)

    det_stages = [
        _score_sql(result, case),
        _score_data(result, case),
        _score_analyst(result, case),
        _score_report(result, case),
    ]
    score.stages.extend(det_stages)

    if use_llm_judge and result.pipeline_completed:
        llm_stages = await _llm_judge(result, case)
        score.stages.extend(llm_stages)

    all_det = [c for s in det_stages for c in s.checks]
    score.overall_pass = all(c.passed for c in all_det)
    return score


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_col(col_names: list[str], target: str) -> str | None:
    for cn in col_names:
        if cn.lower() == target.lower():
            return cn
    return None


def _is_numeric(val) -> bool:
    try:
        float(val)
        return True
    except (TypeError, ValueError):
        return False
