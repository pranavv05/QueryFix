"""Stage 2 — Extract query candidates with source provenance.

The stage's contract is :func:`extract_source`: one
:class:`~queryfix.models.query.SourceFile` in, ``list[ExtractedQuery]`` out,
whatever language the file is written in. Everything else exported here is
either a language implementation or a narrower entry point kept for callers that
predate the contract.

To add a language: implement :class:`~queryfix.pipeline.extract.base.Extractor`
and call :func:`register_extractor`. Nothing in this package, and nothing
downstream of it, needs to change.
"""

from __future__ import annotations

from queryfix.pipeline.extract.base import DEFAULT_DIALECT, Extractor, query_id, symbol_query_id
from queryfix.pipeline.extract.derived import (
    DerivedOperation,
    DerivedQuery,
    EqualityPredicate,
    parse_derived_method,
    parse_derived_query,
    render_derived_query,
)
from queryfix.pipeline.extract.dispatcher import (
    default_registry,
    extract_queries,
    extract_source,
    register_extractor,
)
from queryfix.pipeline.extract.java import JavaExtractor, extract_java
from queryfix.pipeline.extract.registry import ExtractorRegistry
from queryfix.pipeline.extract.sql import SqlExtractor, extract_from_sql

__all__ = [
    "DEFAULT_DIALECT",
    "DerivedOperation",
    "DerivedQuery",
    "EqualityPredicate",
    "Extractor",
    "ExtractorRegistry",
    "JavaExtractor",
    "SqlExtractor",
    "default_registry",
    "extract_from_sql",
    "extract_java",
    "extract_queries",
    "extract_source",
    "parse_derived_method",
    "parse_derived_query",
    "query_id",
    "register_extractor",
    "render_derived_query",
    "symbol_query_id",
]
