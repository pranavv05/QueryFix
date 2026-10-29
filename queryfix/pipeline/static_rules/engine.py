"""Stage 3 — the rule engine.

Parses each candidate query once, hands the AST to every registered rule, and
aggregates the findings. Owns two concerns the rules should not each re-solve:
parsing (one dialect, one place, one way of recording failure) and failure
isolation (a rule that raises must not take the stage down).
"""

from __future__ import annotations

import logging

import sqlglot
from sqlglot import exp

from queryfix.models.finding import Finding, Severity
from queryfix.models.query import ExtractedQuery, QueryKind
from queryfix.pipeline.static_rules.base import RULES, Rule, RuleContext
from queryfix.pipeline.static_rules.schema import UNKNOWN_SCHEMA, SchemaProvider

__all__ = ["RuleEngine", "silence_sqlglot_fallback_warnings"]

logger = logging.getLogger(__name__)

#: Applied to every finding raised against a Spring Data derived-method query.
#: That query's text is not SQL the application will run — it is
#: :func:`~queryfix.pipeline.extract.derived.render_derived_query`'s rendering of
#: what the method name declares, over a guessed table name, with no knowledge of
#: entity mappings. A `@ManyToOne` association compiles to a JOIN Spring Data
#: issues and this renderer cannot see (CLAUDE.md's caution about `findByCustomerId`).
#: The rule that fired is still evaluating a real shape — the predicate, the
#: projection, the operation — so the finding is not discarded; it is labelled
#: unverified, the same treatment CLAUDE.md prescribes for an unverifiable Claude
#: claim, so a reviewer does not read it as a claim about executed SQL.
_INFERRED_CONFIDENCE = 0.5

#: sqlglot logs a WARNING for every statement it cannot fully model and parses as a
#: `Command` instead — `SHOW search_path`, `SET ROLE ...`. That is expected input,
#: not a problem: `normalize_sql` and the rules both handle Command nodes by design.
#: On a real statement log it is one warning per line, which buries anything real.
_FALLBACK_WARNING_FRAGMENT = "contains unsupported syntax"


class _CommandFallbackFilter(logging.Filter):
    """Drops sqlglot's parse-fallback warnings, leaving its other logging intact."""

    def filter(self, record: logging.LogRecord) -> bool:
        return _FALLBACK_WARNING_FRAGMENT not in record.getMessage()


def silence_sqlglot_fallback_warnings() -> None:
    """Install the fallback-warning filter on sqlglot's logger, at most once.

    Idempotent: constructing several engines, or importing this module repeatedly,
    must not stack duplicate filters on a logger that outlives them.
    """
    sqlglot_logger = logging.getLogger("sqlglot")
    if any(isinstance(existing, _CommandFallbackFilter) for existing in sqlglot_logger.filters):
        return
    sqlglot_logger.addFilter(_CommandFallbackFilter())


class RuleEngine:
    """Runs a set of static rules over extracted queries."""

    def __init__(
        self,
        rules: list[Rule] | None = None,
        schema: SchemaProvider = UNKNOWN_SCHEMA,
    ) -> None:
        # Default to the live registry rather than a snapshot taken at import time,
        # so a rule module imported after construction still participates.
        self._rules = rules
        self._schema = schema
        silence_sqlglot_fallback_warnings()

    @property
    def rules(self) -> list[Rule]:
        return self._rules if self._rules is not None else RULES

    def analyze(self, queries: list[ExtractedQuery]) -> list[Finding]:
        """Run every rule over every parseable query, worst findings first."""
        findings: list[Finding] = []

        for query in queries:
            if query.parse_error is not None:
                # The extract stage already recorded why; re-reporting it as a rule
                # finding would double-count one problem.
                continue

            ast = self._parse(query)
            if ast is None:
                continue

            context = RuleContext(query=query, ast=ast, schema=self._schema)
            for rule in self.rules:
                raised = self._run_one(rule, context)
                if query.kind is QueryKind.SPRING_DATA_DERIVED:
                    raised = [self._mark_inferred(finding) for finding in raised]
                findings.extend(raised)

        findings.sort(key=lambda finding: _SEVERITY_ORDER[finding.severity])
        return findings

    def _mark_inferred(self, finding: Finding) -> Finding:
        """Label a finding as unverified, unless a rule already set its own confidence."""
        if finding.confidence is not None:
            return finding
        return finding.model_copy(
            update={
                "confidence": _INFERRED_CONFIDENCE,
                "confidence_basis": (
                    "the SQL shown is rendered from the repository method's name, and "
                    "Spring Data may issue a different statement (a join, for example)"
                ),
            }
        )

    def _parse(self, query: ExtractedQuery) -> exp.Expression | None:
        """Parse one query, or return None having logged why it could not be."""
        try:
            ast = sqlglot.parse_one(query.text, read=query.dialect)
        except sqlglot.ParseError as error:
            logger.warning(
                "static_rules: query %s at %s is unparseable, skipping: %s",
                query.id,
                query.provenance.file,
                error,
            )
            return None

        if ast is None:
            return None

        if not isinstance(ast, exp.Expression) or isinstance(ast, exp.Command):
            # Two shapes with nothing for a rule to inspect. A Command is sqlglot's
            # opaque fallback for syntax it cannot model — guessing from its raw text
            # would mean regexing SQL. A non-Expression is the wider `Expr` base that
            # `parse_one` is declared to return; it carries no `args`, so there are no
            # clauses to read even in principle. Every statement-level node sqlglot
            # produces is an Expression, so in practice this only narrows the type.
            logger.debug("static_rules: query %s has no inspectable structure, skipping", query.id)
            return None

        return ast

    def _run_one(self, rule: Rule, context: RuleContext) -> list[Finding]:
        """Run one rule, containing any failure to that rule.

        Stage-level fail-soft per CLAUDE.md: a rule with a bug degrades its own
        check and nothing else. Catching Exception rather than a specific type is
        deliberate here and scoped to a single rule call — the alternative is that
        one malformed AST silently loses the findings of every rule after it.
        """
        try:
            return rule.check(context)
        except Exception:
            logger.exception(
                "static_rules: rule %s failed on query %s; continuing",
                getattr(rule, "rule_id", type(rule).__name__),
                context.query.id,
            )
            return []


_SEVERITY_ORDER: dict[Severity, int] = {
    Severity.CRITICAL: 0,
    Severity.HIGH: 1,
    Severity.MEDIUM: 2,
    Severity.LOW: 3,
    Severity.INFO: 4,
}
