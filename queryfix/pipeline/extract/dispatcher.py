"""Stage 2 — route one source file to the extractor that understands it.

This module is deliberately almost empty, and that is the point. Adding a
language means writing an :class:`~queryfix.pipeline.extract.base.Extractor`
and registering it; nothing here changes, because nothing here names a language
except the two registration lines below. The stage's input and output contracts
(``SourceFile`` in, ``list[ExtractedQuery]`` out) are fixed regardless of how
many languages exist, so no downstream stage can tell the difference between a
run over ``.sql`` files and a run over a polyglot diff.
"""

from __future__ import annotations

from queryfix.models.query import ExtractedQuery, SourceFile
from queryfix.pipeline.extract.base import Extractor
from queryfix.pipeline.extract.java import JavaExtractor
from queryfix.pipeline.extract.registry import ExtractorRegistry
from queryfix.pipeline.extract.sql import SqlExtractor

__all__ = ["default_registry", "extract_queries", "extract_source", "register_extractor"]

_EXTRACTORS = ExtractorRegistry()
_EXTRACTORS.register(".sql", SqlExtractor())
_EXTRACTORS.register(".java", JavaExtractor())


def default_registry() -> ExtractorRegistry:
    """The process-wide registry :func:`extract_source` consults by default.

    Exposed so an integration can add a language at import time without editing
    this module, and so a caller that needs a different set — a test, or a run
    restricted to one language — can read the default and build from it.
    """
    return _EXTRACTORS


def register_extractor(extension: str, extractor: Extractor) -> None:
    """Add a language to the default registry. Raises if the extension is taken."""
    _EXTRACTORS.register(extension, extractor)


def extract_source(
    source: SourceFile,
    registry: ExtractorRegistry | None = None,
) -> list[ExtractedQuery]:
    """Extract every query candidate from one source file.

    The stage entry point. A file whose extension no extractor claims yields no
    queries rather than an error: a pull request contains READMEs and lock files,
    and "nothing to analyze here" is the correct, quiet answer for them.

    ``registry`` is injectable so callers can substitute the language set without
    patching module state — the same seam the rule engine offers for rules.
    """
    resolved = registry if registry is not None else _EXTRACTORS
    extractor = resolved.for_path(source.path)
    return extractor.extract(source) if extractor is not None else []


def extract_queries(path: str, content: str) -> list[ExtractedQuery]:
    """Extract queries from a path and its text.

    Retained as the pair-shaped entry point that predates
    :class:`~queryfix.models.query.SourceFile`. Prefer :func:`extract_source`:
    a bare pair cannot carry per-source options, so SQL extracted through here is
    always read as the default dialect.
    """
    return extract_source(SourceFile(path=path, content=content))
