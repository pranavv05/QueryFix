"""Tests for selecting a source-language extractor.

These drive the dispatcher through its injected registry rather than by patching
module globals. That is the seam the stage actually offers callers, so testing
through it proves the extension point works instead of proving that
`monkeypatch` works.
"""

from __future__ import annotations

import pytest

from queryfix.models import ExtractedQuery, Provenance, QueryKind, SourceFile, SqlSource
from queryfix.pipeline.extract import dispatcher
from queryfix.pipeline.extract.registry import ExtractorRegistry


def query() -> ExtractedQuery:
    """A distinctive query object used to prove dispatcher pass-through."""
    return ExtractedQuery(
        id="migration.sql:3",
        kind=QueryKind.RAW_SQL,
        text="SELECT id FROM orders",
        provenance=Provenance(file="migration.sql", line=3),
    )


class RecordingExtractor:
    """An extractor that records what it was handed and returns a fixed result."""

    def __init__(self, result: list[ExtractedQuery] | None = None) -> None:
        self.result = result if result is not None else []
        self.calls: list[SourceFile] = []

    def extract(self, source: SourceFile) -> list[ExtractedQuery]:
        self.calls.append(source)
        return self.result


class ExplodingExtractor:
    """An extractor that must never be reached."""

    def extract(self, source: SourceFile) -> list[ExtractedQuery]:
        raise AssertionError("this extractor must not receive an unsupported file")


@pytest.mark.parametrize(
    ("path", "content"),
    [
        ("migration.sql", "SELECT 1"),
        ("MIGRATION.SQL", "SELECT 1"),
        ("db/migrations/V3__orders.Sql", ""),
    ],
)
def test_sql_files_are_delegated_without_altering_extractor_output(path: str, content: str) -> None:
    expected = [query()]
    extractor = RecordingExtractor(expected)
    registry = ExtractorRegistry()
    registry.register(".sql", extractor)

    source = SourceFile(path=path, content=content)

    assert dispatcher.extract_source(source, registry) is expected
    assert extractor.calls == [source]


def test_java_files_are_delegated_to_the_java_extractor() -> None:
    expected = [query()]
    extractor = RecordingExtractor(expected)
    registry = ExtractorRegistry()
    registry.register(".java", extractor)

    source = SourceFile(path="src/Repository.JAVA", content="")

    assert dispatcher.extract_source(source, registry) is expected
    assert extractor.calls == [source]


@pytest.mark.parametrize("path", ["README.md", "script.kt", "Dockerfile", "query.sql.bak"])
def test_unsupported_extensions_return_no_queries(path: str) -> None:
    assert dispatcher.extract_queries(path, "SELECT * FROM orders") == []


def test_empty_sql_file_is_delegated_to_the_sql_extractor() -> None:
    extractor = RecordingExtractor()
    registry = ExtractorRegistry()
    registry.register(".sql", extractor)

    source = SourceFile(path="empty.sql", content="")

    assert dispatcher.extract_source(source, registry) == []
    assert extractor.calls == [source]


def test_dispatcher_does_not_parse_java_as_sql() -> None:
    registry = ExtractorRegistry()
    registry.register(".sql", ExplodingExtractor())
    registry.register(".java", RecordingExtractor())

    source = SourceFile(path="src/Repository.java", content="SELECT * FROM orders")

    assert dispatcher.extract_source(source, registry) == []


def test_the_default_registry_covers_the_documented_languages() -> None:
    # A language that stops being registered is a language that silently stops
    # being analyzed, with no error anywhere to say so.
    assert dispatcher.default_registry().extensions() == frozenset({".sql", ".java"})


def test_a_new_language_needs_no_change_to_the_dispatcher() -> None:
    # The open/closed property stated in the module docstring, as a test: routing
    # to a language the dispatcher has never heard of takes one registration.
    extractor = RecordingExtractor([query()])
    registry = ExtractorRegistry()
    registry.register(".kt", extractor)

    source = SourceFile(path="src/OrderRepository.kt", content="")

    assert dispatcher.extract_source(source, registry) == [query()]
    assert extractor.calls == [source]


def test_the_pair_shaped_entry_point_still_extracts_sql() -> None:
    # Backwards compatibility: `extract_queries(path, content)` predates
    # `SourceFile` and callers outside this repository may still use it.
    (extracted,) = dispatcher.extract_queries("migrations/001.sql", "SELECT id FROM orders")

    assert extracted.id == "migrations/001.sql:1"
    assert extracted.dialect == "postgres"


def test_a_sources_dialect_reaches_the_sql_extractor() -> None:
    # `SqlSource.dialect` is a public field; before the stage took whole source
    # models it was silently dropped, and valid MySQL came back unanalyzable.
    (extracted,) = dispatcher.extract_source(
        SqlSource(path="migrations/001.sql", content="SELECT `id` FROM orders", dialect="mysql")
    )

    assert extracted.dialect == "mysql"
    assert extracted.parse_error is None


def test_a_dialect_free_source_falls_back_to_the_default() -> None:
    (extracted,) = dispatcher.extract_source(
        SourceFile(path="migrations/001.sql", content="SELECT id FROM orders")
    )

    assert extracted.dialect == "postgres"
