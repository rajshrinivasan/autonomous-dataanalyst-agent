from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any


@dataclass
class SqlChecks:
    must_contain_tables: list[str] = field(default_factory=list)
    must_contain_columns: list[str] = field(default_factory=list)
    must_use_aggregation: bool = False
    must_use_join: bool = False
    must_use_where: bool = False
    must_use_group_by: bool = False
    must_use_strftime: bool = False
    must_not_use_dml: bool = True


@dataclass
class DataChecks:
    expected_columns_subset: list[str] = field(default_factory=list)
    row_count_exact: int | None = None
    row_count_min: int | None = None
    first_row_contains: dict[str, Any] | None = None   # {column: substring}
    numeric_column: str | None = None
    numeric_min: float | None = None
    numeric_max: float | None = None
    any_numeric_gt: float | None = None                # any numeric column has a value > threshold
    allowed_column_values: dict[str, list] | None = None
    column_pattern: dict[str, str] | None = None       # {column: regex pattern}


@dataclass
class AnalystChecks:
    chart_required: bool = True
    min_insights: int = 3
    insights_must_mention: list[str] = field(default_factory=list)


@dataclass
class ReportChecks:
    required_sections: list[str] = field(default_factory=list)
    must_mention_numbers: bool = True
    max_words: int = 450


@dataclass
class GoldenCase:
    id: str
    description: str
    question: str
    tags: list[str]
    sql_checks: SqlChecks
    data_checks: DataChecks
    analyst_checks: AnalystChecks
    report_checks: ReportChecks
