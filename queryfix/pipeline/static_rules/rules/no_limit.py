"""``SELECT`` over a whole table with no row limit — an unbounded result set."""

from __future__ import annotations

from sqlglot import exp

from queryfix.models.finding import Finding, Severity, Suggestion
from queryfix.pipeline.static_rules.base import (
    RuleContext,
    clause,
    has_clause,
    register,
)

__all__ = ["NoLimitRule"]


class NoLimitRule:
    """Flags a top-level ``SELECT`` that scans a table with nothing bounding it.

    The exclusions matter more than the check, because each one is a false positive
    this rule would otherwise produce on healthy code:

    * **Not a subquery.** Only the root statement is considered. An inner
      ``SELECT`` in ``IN (...)`` or ``EXISTS (...)`` is bounded by its use, not by
      a ``LIMIT`` of its own, and demanding one there would be wrong.
    * **Not an aggregate that returns one row.** ``SELECT COUNT(*) FROM t`` reads
      the table but returns a single row, so a ``LIMIT`` would add nothing. This
      requires *every* projection to be an aggregate and no ``GROUP BY`` — with a
      ``GROUP BY`` the row count is the number of groups, which is unbounded again.
    * **Not a query with a WHERE clause.** This is the judgement call in the rule.
      A filtered query is not what "table scan" means, and flagging every
      ``SELECT`` without a ``LIMIT`` would fire on the majority of correct queries
      in any codebase — including the sandbox's deliberately healthy
      ``exportRecentOrders``. The cost of a selective-but-unlimited query is a
      question for the plan stage, which can see cardinality; a static rule cannot
      tell ``WHERE id = ?`` from ``WHERE status = 'active'``.
    * **Not a query with no FROM.** ``SELECT 1`` and ``SELECT now()`` read nothing.

    What survives all four is the genuine case: read this entire table, return all
    of it, however large it grows.
    """

    rule_id = "no-limit"

    def check(self, context: RuleContext) -> list[Finding]:
        node = context.ast
        if not isinstance(node, exp.Select):
            return []

        if _from_clause(node) is None:
            return []
        if has_clause(node, "limit", "fetch", "where"):
            return []
        if self._filtered_by_join_condition(node):
            return []
        if self._returns_single_row(node):
            return []

        table = self._scanned_table(node) or "the source table"

        return [
            context.finding(
                rule_id=self.rule_id,
                severity=Severity.HIGH,
                title="SELECT reads an entire table with no row limit",
                explanation=(
                    f"This SELECT reads `{table}` with no WHERE clause and no "
                    f"LIMIT/FETCH, so the result set is the whole table."
                ),
                impact=(
                    "Cost is proportional to table size with no ceiling, so the query "
                    "passes review at today's row count and degrades continuously as the "
                    "table grows — there is no threshold at which it starts failing "
                    "loudly. Every matched row is also materialised on the client: in a "
                    "JVM caller the whole result lands in the heap at once, which turns a "
                    "slow query into an out-of-memory failure of the whole process rather "
                    "than of one request."
                ),
                suggestions=[
                    Suggestion(
                        description=(
                            "Decide what bounds this query and say it in SQL: a WHERE "
                            "clause if the caller wants a subset, keyset pagination "
                            "(`WHERE id > :last ORDER BY id LIMIT :n`) if it wants pages, "
                            "or a server-side cursor / streaming fetch if it genuinely "
                            "must process every row without holding them all at once."
                        )
                    )
                ],
            )
        ]

    @staticmethod
    def _filtered_by_join_condition(node: exp.Select) -> bool:
        """True when an INNER JOIN's ``ON`` constrains a column to a value.

        ``... INNER JOIN b.file f ON f.fileNumber = :fileNumber`` is a filter written
        in the join: the result is restricted to one file exactly as if the predicate
        sat in a WHERE. Only an inner join counts — an outer join's ``ON`` decides
        which right-hand rows attach, not which left-hand rows survive — and only a
        comparison of a column against a parameter or literal does, since
        ``ON a.id = b.id`` relates two tables without selecting anything.
        """
        for join in node.args.get("joins") or []:
            if join.args.get("side"):
                continue
            on = join.args.get("on")
            if on is None:
                continue
            for comparison in on.find_all(exp.EQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.In):
                sides = [comparison.this, comparison.args.get("expression")]
                has_column = [
                    isinstance(side, exp.Expression) and side.find(exp.Column) is not None
                    for side in sides
                ]
                if sorted(has_column) == [False, True] or (
                    isinstance(comparison, exp.In) and has_column[0]
                ):
                    return True
        return False

    @staticmethod
    def _returns_single_row(node: exp.Select) -> bool:
        """True for a bare aggregate like ``SELECT COUNT(*), SUM(x) FROM t``."""
        if has_clause(node, "group"):
            return False

        projections = node.expressions
        if not projections:
            return False

        return all(isinstance(projection.unalias(), exp.AggFunc) for projection in projections)

    @staticmethod
    def _scanned_table(node: exp.Select) -> str | None:
        table = node.find(exp.Table)
        return table.name if table is not None else None


def _from_clause(select: exp.Select) -> exp.Expression | None:
    """The statement's own FROM clause.

    Reading the args rather than ``find(exp.From)`` is deliberate: ``find`` descends
    into subqueries, so ``SELECT (SELECT 1 FROM t)`` would look like it had a FROM of
    its own and get flagged for scanning a table it never reads.
    """
    return clause(select, "from_", "from")


register(NoLimitRule())
