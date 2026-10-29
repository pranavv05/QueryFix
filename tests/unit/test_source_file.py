"""Tests for language-neutral source-file contracts."""

from __future__ import annotations

from queryfix.models import SourceFile, SqlSource
from queryfix.pipeline.runner import AnalysisRunner


def test_sql_source_remains_a_source_file_with_its_dialect() -> None:
    source = SqlSource(path="migrations/001.sql", content="SELECT 1", dialect="mysql")

    assert isinstance(source, SourceFile)
    assert source.dialect == "mysql"


def test_runner_accepts_a_language_neutral_java_source() -> None:
    report = AnalysisRunner().run(
        repo="acme/billing-service",
        pr_number=42,
        sources=[
            SourceFile(
                path="src/CustomerRepository.java",
                content="public interface CustomerRepository { Customer findById(Long id); }",
            )
        ],
    )

    assert [query.kind.value for query in report.queries] == ["spring_data_derived"]
