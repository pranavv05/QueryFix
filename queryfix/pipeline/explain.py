"""Stage 5 — Plan analysis.

Runs ``EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)`` against the isolated reference
database and inspects the resulting plan tree.

Every statement goes through :func:`queryfix.db.session.rollback_transaction`,
which owns the ``BEGIN`` / ``ROLLBACK`` wrapper — nothing here opens a raw cursor.
"""

from __future__ import annotations

from typing import Any

from queryfix.models.finding import Finding
from queryfix.models.query import ExtractedQuery

__all__ = ["analyze_plan", "explain_analyze"]


def explain_analyze(connection: Any, query: ExtractedQuery) -> dict[str, Any]:
    """Run ``EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)`` inside a rolled-back tx.

    Placeholder parameters are bound before execution. Returns the parsed plan
    JSON.
    """
    raise NotImplementedError("explain.explain_analyze is not implemented yet")


def analyze_plan(query: ExtractedQuery, plan: dict[str, Any]) -> list[Finding]:
    """Inspect a plan tree for performance problems.

    Looks for sequential scans on large tables, bad row estimates, nested-loop
    blowups, external sorts, and spilled hashes.

    Takes the plan as data rather than a connection so this stays unit-testable
    against captured JSON in ``tests/fixtures/plans/`` with no Docker.
    """
    raise NotImplementedError("explain.analyze_plan is not implemented yet")
