"""Stage 6 — Index simulation via HypoPG.

Proposes candidate indexes from the plan and its predicates, creates them
hypothetically, and records the before/after cost comparison. Only candidates
clearing the configured improvement threshold are surfaced.

Two constraints that are easy to get wrong:

- Hypothetical indexes are visible to ``EXPLAIN`` but **not** to
  ``EXPLAIN ANALYZE`` — an ``ANALYZE`` run actually executes the query, which a
  non-existent index cannot serve. The after-cost measurement therefore uses
  plain ``EXPLAIN``.
- ``hypopg_reset()`` runs after every candidate, or simulations compound and the
  second candidate's "improvement" includes the first's.
"""

from __future__ import annotations

from typing import Any

from queryfix.models.finding import Suggestion
from queryfix.models.query import ExtractedQuery

__all__ = ["simulate_indexes"]


def simulate_indexes(
    connection: Any,
    query: ExtractedQuery,
    plan: dict[str, Any],
) -> list[Suggestion]:
    """Propose candidate indexes and measure their simulated impact.

    For each candidate: ``hypopg_create_index`` -> plain ``EXPLAIN`` -> record the
    cost delta -> ``hypopg_reset``.
    """
    raise NotImplementedError("hypopg.simulate_indexes is not implemented yet")
