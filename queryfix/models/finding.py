"""Finding contracts — what every analysis stage produces.

Static rules, plan analysis, index simulation, and the N+1 stage all emit
:class:`Finding`, so the report renderer has one shape to handle regardless of
which stage found the problem.
"""

from __future__ import annotations

from enum import Enum

from pydantic import Field

from queryfix.models.base import Contract
from queryfix.models.query import Provenance


class Severity(str, Enum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Evidence(Contract):
    """Supporting detail for a finding: a plan excerpt, a cost delta, a snippet."""

    label: str
    detail: str


class Suggestion(Contract):
    """A proposed fix, and the measured impact of applying it where available."""

    description: str
    sql: str | None = Field(
        default=None,
        description="DDL or rewritten query, e.g. a CREATE INDEX statement.",
    )
    cost_before: float | None = None
    cost_after: float | None = None


class Finding(Contract):
    """One reportable problem, from any analysis stage."""

    rule_id: str
    severity: Severity
    title: str
    explanation: str = Field(
        description="What was found, in terms a reviewer can check against the query."
    )
    impact: str = Field(
        description="Why it is a problem — the consequence at scale, not the observation. "
        "A finding that only restates what it matched gives a reviewer no reason to act, "
        "so this is required rather than optional."
    )
    provenance: Provenance
    query_id: str | None = None
    query_ids: list[str] = Field(
        default_factory=list,
        description="Populated for cross-query findings such as N+1 patterns.",
    )
    evidence: list[Evidence] = Field(default_factory=list)
    suggestions: list[Suggestion] = Field(default_factory=list)
    confidence: float | None = Field(
        default=None,
        description="Set when a finding could not be verified against real SQL or a "
        "real plan — an LLM-derived claim, or a static finding raised against a Spring "
        "Data derived-method's rendered pseudo-SQL rather than a query the application "
        "will actually issue. An ordinal strength, not a probability: it is a fixed value "
        "per evidence tier and is never rendered as a percentage.",
    )
    confidence_basis: str | None = Field(
        default=None,
        description="The checkable conditions behind `confidence`, as one clause a reviewer "
        "can verify (what feeds the call, what was resolved, what was only inferred). "
        "Shown in place of a number.",
    )
