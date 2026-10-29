"""Tests for source-language extractor registration."""

from __future__ import annotations

import pytest

from queryfix.models import ExtractedQuery, Provenance, QueryKind, SourceFile
from queryfix.pipeline.extract.base import Extractor
from queryfix.pipeline.extract.registry import ExtractorRegistry


class DemoExtractor:
    """A typed test extractor with a recognizably distinct result."""

    def extract(self, source: SourceFile) -> list[ExtractedQuery]:
        return [
            ExtractedQuery(
                id=f"{source.path}:1",
                kind=QueryKind.RAW_SQL,
                text=source.content,
                provenance=Provenance(file=source.path, line=1),
            )
        ]


def test_registry_resolves_extensions_case_insensitively() -> None:
    extractor = DemoExtractor()
    registry = ExtractorRegistry()
    registry.register(".demo", extractor)

    resolved = registry.for_path("src/Example.DEMO")

    assert resolved is extractor


def test_registry_returns_none_for_an_unregistered_extension() -> None:
    assert ExtractorRegistry().for_path("src/Example.java") is None


@pytest.mark.parametrize("extension", ["", "java", "."])
def test_registry_rejects_invalid_extensions(extension: str) -> None:
    with pytest.raises(ValueError, match="extension"):
        ExtractorRegistry().register(extension, DemoExtractor())


def test_registry_rejects_duplicate_extensions_after_normalization() -> None:
    registry = ExtractorRegistry()
    registry.register(".java", DemoExtractor())

    with pytest.raises(ValueError, match="already registered"):
        registry.register(".JAVA", DemoExtractor())


def test_a_rejected_duplicate_leaves_the_original_in_place() -> None:
    # Rejection must not be half-applied: the language that was already working
    # has to keep working after a failed registration.
    original = DemoExtractor()
    registry = ExtractorRegistry()
    registry.register(".java", original)

    with pytest.raises(ValueError, match="already registered"):
        registry.register(".java", DemoExtractor())

    assert registry.for_path("X.java") is original


def test_registry_reports_the_extensions_it_covers() -> None:
    registry = ExtractorRegistry()
    registry.register(".sql", DemoExtractor())
    registry.register(".JAVA", DemoExtractor())

    assert registry.extensions() == frozenset({".sql", ".java"})


def test_an_extractor_satisfies_the_protocol_structurally() -> None:
    # The point of the protocol: a new language implements `extract` and is
    # accepted, with no base class to inherit and no import of ours to add.
    assert isinstance(DemoExtractor(), Extractor)
