"""Stage 2 — the extraction contract every source language implements.

One protocol, one input model, one output model. A new language is a new class
satisfying :class:`Extractor` plus one :meth:`registration
<queryfix.pipeline.extract.registry.ExtractorRegistry.register>` call; no
module downstream of extraction changes, and no module upstream of it learns a
new name. That is the "open for extension, closed for modification" property the
extract stage is meant to have, expressed as a type rather than as a convention.

The input is a :class:`~queryfix.models.query.SourceFile` rather than a
``(path, content)`` pair on purpose. A pair cannot carry per-source options, and
the moment one language needs one — SQL already does, in ``SqlSource.dialect`` —
the choice is to widen every extractor's signature or to smuggle the option past
the contract. The first breaks every implementation for one language's benefit;
the second is how ``SqlSource.dialect`` came to be a public field that nothing
read. A model subtype carries the option to the extractor that understands it and
stays invisible to the ones that do not.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from queryfix.models.query import ExtractedQuery, SourceFile

__all__ = ["DEFAULT_DIALECT", "Extractor", "query_id", "symbol_query_id"]

#: Dialect assumed for a source that does not declare one. Postgres, because the
#: reference database is Postgres 16 (CLAUDE.md's tech-stack table).
DEFAULT_DIALECT = "postgres"


@runtime_checkable
class Extractor(Protocol):
    """Pulls query candidates out of one source file.

    Implementations must be stateless and safe to share: one instance is
    registered per language at import time and reused for every run, including
    concurrent ones (``POST /analyze`` is a plain ``def``, so FastAPI serves it
    from a threadpool).

    An implementation reports what it could not read rather than raising:
    unparseable input becomes a candidate carrying ``parse_error``, and input in
    a shape this extractor does not support yet becomes no candidate at all. The
    runner treats a raised exception as a degraded source, so raising still fails
    soft — it just costs the whole file instead of one query.
    """

    def extract(self, source: SourceFile) -> list[ExtractedQuery]:
        """Return every query candidate in ``source``, in source order."""
        ...


def query_id(path: str, ordinal: int) -> str:
    """The identifier for the ``ordinal``-th query extracted from ``path``.

    Centralized because three extractors mint these and the format is a contract:
    findings reference a query by ID, so two extractors disagreeing about the
    shape means a finding that anchors to nothing. ``ordinal`` is 1-based and
    counts queries actually produced from the file, whatever language they came
    from, so the Nth ID is the Nth query a reviewer sees from that file.

    Not yet unique across a run — the same path submitted twice yields the same
    IDs. See the deferred-debt table in STATUS.md; fixing it means qualifying by
    run, which is the report renderer's problem to state a need for.
    """
    return f"{path}:{ordinal}"


def symbol_query_id(path: str, symbol: str) -> str:
    """The identifier for a query that a named symbol in ``path`` stands for.

    Used where the source gives the query a name of its own — a Spring Data
    derived method is the query, so ``CustomerRepository.java:findByEmail`` says
    more than a position would and, unlike a position, does not change when an
    unrelated method is added above it.

    Cannot collide with :func:`query_id`, whose suffix is always a number.
    """
    return f"{path}:{symbol}"
