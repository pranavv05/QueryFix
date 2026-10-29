"""Tests for the pipeline orchestrator.

The endpoint tests cover what a caller sees. These cover the contracts the runner
owns and the HTTP surface cannot show: that each stage fails soft into a named
degradation, and that the rule engine is driven exactly once over the whole query
set rather than once per source or once per rule.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from queryfix.models import Finding, Severity, SourceFile, SqlSource
from queryfix.models.query import ExtractedQuery
from queryfix.pipeline import runner as runner_module
from queryfix.pipeline.extract import extract_source
from queryfix.pipeline.report import DEFAULT_MAX_FINDINGS
from queryfix.pipeline.runner import STATIC_RULES_STAGE, AnalysisRunner
from queryfix.pipeline.static_rules import RuleEngine

#: The structured payload every completed run must carry. Asserted as a whole rather
#: than field by field, so a field that silently stops being logged fails the test.
RUN_LOG_FIELDS = (
    "run_id",
    "repo",
    "pr_number",
    "number_of_queries",
    "number_of_findings",
    "degraded_stages",
)


def structured(record: logging.LogRecord, *fields: str) -> dict[str, Any]:
    """The ``extra`` payload of a log record, which is otherwise untyped."""
    return {field: getattr(record, field, None) for field in fields}


SELECT_STAR = SqlSource(path="a.sql", content="SELECT * FROM orders")
UNPARSEABLE = SqlSource(path="b.sql", content="SELECT FROM WHERE")


class RecordingEngine(RuleEngine):
    """A rule engine that remembers how it was called."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[list[str]] = []

    def analyze(self, queries: list[ExtractedQuery]) -> list[Finding]:
        self.calls.append([query.id for query in queries])
        return super().analyze(queries)


class ExplodingEngine(RuleEngine):
    """A rule engine whose own dispatch loop fails, not merely one of its rules."""

    def analyze(self, queries: list[ExtractedQuery]) -> list[Finding]:
        raise RuntimeError("engine dispatch failed")


def test_the_engine_is_driven_once_over_every_extracted_query() -> None:
    engine = RecordingEngine()

    report = AnalysisRunner(engine).run(
        repo="acme/billing-service",
        pr_number=42,
        sources=[SELECT_STAR, SqlSource(path="c.sql", content="DELETE FROM customers")],
    )

    # One call carrying both queries: parsing happens once per query, and findings
    # from different sources are ranked against each other rather than concatenated.
    assert engine.calls == [["a.sql:1", "c.sql:1"]]
    assert [finding.severity for finding in report.findings] == [
        Severity.CRITICAL,
        Severity.HIGH,
        Severity.MEDIUM,
    ]


def test_an_unparseable_source_costs_only_itself() -> None:
    report = AnalysisRunner().run(
        repo="acme/billing-service",
        pr_number=42,
        sources=[UNPARSEABLE, SELECT_STAR],
    )

    assert report.degraded_stages == ["extract:b.sql"]
    assert {finding.provenance.file for finding in report.findings} == {"a.sql"}

    # The unanalyzable candidate is still reported, with the reason attached.
    unparseable = [query for query in report.queries if query.parse_error is not None]
    assert [query.provenance.file for query in unparseable] == ["b.sql"]


def test_an_extractor_that_raises_is_contained_to_its_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stage boundary that malformed SQL alone cannot exercise.

    The extractor turns unreadable SQL into an unanalyzable candidate rather than
    raising, so the only way to reach the runner's catch-all — and prove it costs one
    source rather than the run — is to make the extractor fail outright.
    """
    real_extract = extract_source

    def explode(source: SourceFile) -> list[ExtractedQuery]:
        if source.path == "b.sql":
            raise RuntimeError("unexpected extractor failure")
        return real_extract(source)

    monkeypatch.setattr("queryfix.pipeline.runner.extract_source", explode)

    report = AnalysisRunner().run(
        repo="acme/billing-service",
        pr_number=42,
        sources=[UNPARSEABLE, SELECT_STAR],
    )

    assert report.degraded_stages == ["extract:b.sql"]
    assert [query.id for query in report.queries] == ["a.sql:1"]
    assert report.findings


def test_a_failing_rule_engine_degrades_the_stage_and_keeps_the_queries() -> None:
    report = AnalysisRunner(ExplodingEngine()).run(
        repo="acme/billing-service",
        pr_number=42,
        sources=[SELECT_STAR],
    )

    # The extract stage's work survives: the report still says which queries the PR
    # touched, it just cannot say anything about them.
    assert [query.id for query in report.queries] == ["a.sql:1"]
    assert report.findings == []
    assert report.degraded_stages == [STATIC_RULES_STAGE]


def test_findings_beyond_the_default_cap_are_omitted_not_lost() -> None:
    sources = [
        SqlSource(path=f"f{i}.sql", content="SELECT * FROM t WHERE id = 1")
        for i in range(DEFAULT_MAX_FINDINGS + 1)
    ]

    report = AnalysisRunner().run(repo="acme/billing-service", pr_number=42, sources=sources)

    assert len(report.findings) == DEFAULT_MAX_FINDINGS
    assert report.omitted_findings == 1


def test_max_findings_is_configurable_per_run() -> None:
    sources = [
        SqlSource(path=f"f{i}.sql", content="SELECT * FROM t WHERE id = 1") for i in range(5)
    ]

    report = AnalysisRunner().run(
        repo="acme/billing-service", pr_number=42, sources=sources, max_findings=2
    )

    assert len(report.findings) == 2
    assert report.omitted_findings == 3


def test_no_sources_is_a_clean_empty_run() -> None:
    report = AnalysisRunner().run(repo="acme/billing-service", pr_number=42, sources=[])

    assert report.queries == []
    assert report.findings == []
    assert report.degraded_stages == []
    assert report.context.repo == "acme/billing-service"


def test_a_supplied_run_id_is_used_verbatim() -> None:
    report = AnalysisRunner().run(
        repo="acme/billing-service",
        pr_number=42,
        sources=[],
        run_id="delivery-7f3a",
    )

    assert report.context.run_id == "delivery-7f3a"


def test_the_run_is_logged_once_with_its_shape(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger=runner_module.__name__):
        AnalysisRunner().run(
            repo="acme/billing-service",
            pr_number=42,
            sources=[SELECT_STAR],
            run_id="run-1",
        )

    (record,) = [
        record
        for record in caplog.records
        if record.name == runner_module.__name__ and record.levelno == logging.INFO
    ]
    assert structured(record, *RUN_LOG_FIELDS) == {
        "run_id": "run-1",
        "repo": "acme/billing-service",
        "pr_number": 42,
        "number_of_queries": 1,
        "number_of_findings": 2,
        "degraded_stages": [],
    }
    # Wall-clock, so only its presence and sign can be asserted deterministically.
    assert structured(record, "processing_time_ms")["processing_time_ms"] >= 0


def test_a_degraded_run_names_what_it_lost_in_the_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger=runner_module.__name__):
        AnalysisRunner().run(
            repo="acme/billing-service",
            pr_number=42,
            sources=[UNPARSEABLE],
            run_id="run-2",
        )

    # Filtered by logger name: caplog captures at the root, so asserting on "the only
    # warning" would make this test hostage to anything else that warns.
    (record,) = [
        record
        for record in caplog.records
        if record.name == runner_module.__name__ and record.levelno == logging.WARNING
    ]
    assert structured(record, "run_id", "source_path") == {
        "run_id": "run-2",
        "source_path": "b.sql",
    }
