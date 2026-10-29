"""Stage 3 — the rule protocol and registry.

Deterministic checks over parsed query ASTs. These run before any database is
provisioned, so their findings survive a provisioning failure.

Individual rules live one-per-file under :mod:`queryfix.pipeline.static_rules.rules`
and are named after the smell they detect (``SelectStarRule``,
``LeadingWildcardLikeRule``). Each ships with fixtures: one query it must flag,
and one similar query it must not.

Rules receive a :class:`RuleContext` — a parsed sqlglot AST plus provenance and
schema — never a raw string. Parsing happens once in the engine so that a rule
cannot re-parse with a different dialect, and so an unparseable query is recorded
as unanalyzable in exactly one place.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from sqlglot import exp

from queryfix.models.finding import Finding, Severity, Suggestion
from queryfix.models.query import ExtractedQuery

# Re-exported rather than moved-and-forgotten: the AST readings live in
# `ast_helpers` (see that module for why), but every rule already imports them
# from here and there is no reason to make a rule author care which of the two
# files a helper is defined in.
from queryfix.pipeline.static_rules.ast_helpers import (
    clause,
    has_clause,
    resolve_table,
    table_aliases,
)
from queryfix.pipeline.static_rules.schema import (
    UNKNOWN_SCHEMA,
    SchemaProvider,
)

__all__ = [
    "RULES",
    "Rule",
    "RuleContext",
    "clause",
    "has_clause",
    "register",
    "resolve_table",
    "run_static_rules",
    "table_aliases",
]


@dataclass(frozen=True)
class RuleContext:
    """Everything a rule may look at.

    Not a Pydantic model: it carries a live sqlglot ``Expression``, which is a
    mutable tree with no serialization contract. Stage *outputs* are Pydantic
    models (:class:`Finding`); this is an intra-stage argument holder.
    """

    query: ExtractedQuery
    ast: exp.Expression
    schema: SchemaProvider = UNKNOWN_SCHEMA

    def finding(
        self,
        *,
        rule_id: str,
        severity: Severity,
        title: str,
        explanation: str,
        impact: str,
        suggestions: list[Suggestion] | None = None,
    ) -> Finding:
        """Build a Finding already anchored to this query's provenance."""
        return Finding(
            rule_id=rule_id,
            severity=severity,
            title=title,
            explanation=explanation,
            impact=impact,
            provenance=self.query.provenance,
            query_id=self.query.id,
            suggestions=suggestions or [],
        )


class Rule(Protocol):
    """A single static check over one parsed query."""

    rule_id: str

    def check(self, context: RuleContext) -> list[Finding]:
        """Return findings for this query, or an empty list if it is clean."""
        ...


RULES: list[Rule] = []


def register(rule: Rule) -> Rule:
    """Register a rule instance in the global registry.

    Takes an instance, not a class: the :class:`Rule` protocol requires a bound
    ``check``, and a class object would satisfy the attribute check while failing
    at call time.

    A duplicate ``rule_id`` is rejected, matching
    :class:`~queryfix.pipeline.extract.registry.ExtractorRegistry` — the two
    extension points of this pipeline should behave the same way when misused.
    Registration is an import side effect, so the failure mode being closed here
    is a rule registered twice by two import paths, whose only symptom would be
    every one of its findings appearing twice in the PR comment.
    """
    duplicate = next((existing for existing in RULES if existing.rule_id == rule.rule_id), None)
    if duplicate is not None:
        msg = f"a rule is already registered with rule_id {rule.rule_id!r}"
        raise ValueError(msg)

    RULES.append(rule)
    return rule


def run_static_rules(
    queries: list[ExtractedQuery],
    schema: SchemaProvider = UNKNOWN_SCHEMA,
) -> list[Finding]:
    """Run every registered rule over every parseable query.

    A one-call convenience over
    :class:`~queryfix.pipeline.static_rules.engine.RuleEngine`, for a caller
    with a query list and no reason to hold an engine — a script, a notebook, a
    test. The pipeline itself constructs the engine directly, because it reuses
    one across runs and injects the rule set; routing it through here would mean
    building an engine per run and losing that seam.
    """
    # Imported here: engine imports this module for RuleContext, so a module-level
    # import would be circular.
    from queryfix.pipeline.static_rules.engine import RuleEngine

    return RuleEngine(schema=schema).analyze(queries)
