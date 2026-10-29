"""``WHERE`` predicate on a column with no index behind it."""

from __future__ import annotations

from sqlglot import exp

from queryfix.models.finding import Finding, Severity, Suggestion
from queryfix.pipeline.static_rules.base import (
    RuleContext,
    clause,
    register,
    resolve_table,
    table_aliases,
)

__all__ = ["UnindexedFilterRule"]

#: Predicate types an index can serve. A column appearing anywhere else in the
#: WHERE clause (inside a function call, say) is not an indexable predicate and is
#: the business of NonSargableRule instead.
#:
#: Annotated rather than inferred so that ``find_all(*_INDEXABLE_PREDICATES)`` yields
#: ``Expression``. sqlglot's ``find_all`` is generic in the types passed to it, and
#: with several it resolves to their nearest common base — ``Condition``, which sits
#: beside ``Expression`` rather than under it and declares no ``args``. Every node
#: matched here is an ``Expression`` at runtime; this is what says so statically.
_INDEXABLE_PREDICATES: tuple[type[exp.Expression], ...] = (
    exp.EQ,
    exp.NEQ,
    exp.GT,
    exp.GTE,
    exp.LT,
    exp.LTE,
    exp.In,
    exp.Between,
)


class UnindexedFilterRule:
    """Flags a filter on a column the schema has no index for.

    Schema-dependent, and therefore silent by default: with the stub provider
    (:data:`~queryfix.pipeline.static_rules.schema.UNKNOWN_SCHEMA`) every lookup
    returns None and this rule reports nothing. "No index on that column" cannot be
    established from the query text alone, and a rule that guessed would fire on
    every predicate in the diff.

    Only columns in *indexable* positions are considered. ``WHERE LOWER(country) =
    ?`` is not an unindexed-column problem — adding an index on ``country`` would
    not help it — so it is left to
    :class:`~queryfix.pipeline.static_rules.rules.non_sargable.NonSargableRule`.
    """

    rule_id = "unindexed-filter"

    def check(self, context: RuleContext) -> list[Finding]:
        where = clause(context.ast, "where")
        if where is None:
            return []

        aliases = table_aliases(context.ast)
        findings: list[Finding] = []
        seen: set[tuple[str, str]] = set()

        for predicate in where.find_all(*_INDEXABLE_PREDICATES):
            for column in self._indexable_columns(predicate):
                table = resolve_table(column, aliases)
                if table is None:
                    continue

                indexed = context.schema.indexed_columns(table)
                if indexed is None:
                    # Schema does not know this table — say nothing.
                    continue

                name = column.name.lower()
                if name in indexed:
                    continue

                key = (table.lower(), name)
                if key in seen:
                    continue
                seen.add(key)

                findings.append(self._finding(context, table, column.name, indexed))

        return findings

    @staticmethod
    def _indexable_columns(predicate: exp.Expression) -> list[exp.Column]:
        """Columns sitting directly on one side of a predicate.

        ``predicate.this`` only, not a recursive walk: in ``LOWER(country) = ?`` the
        column is wrapped in a function, so it is not directly indexable and must
        not be reported here.
        """
        columns: list[exp.Column] = []
        for side in (predicate.this, predicate.args.get("expression")):
            if isinstance(side, exp.Column):
                columns.append(side)
        return columns

    def _finding(
        self,
        context: RuleContext,
        table: str,
        column: str,
        indexed: frozenset[str],
    ) -> Finding:
        known = ", ".join(sorted(indexed)) if indexed else "none"
        return context.finding(
            rule_id=self.rule_id,
            severity=Severity.HIGH,
            title=f"Filter on `{table}.{column}`, which has no index",
            explanation=(
                f"The WHERE clause filters on `{column}`, but the schema has no index "
                f"leading with that column. Indexed columns on `{table}`: {known}."
            ),
            impact=(
                "Without an index the database has to read every row of the table and "
                "test the predicate on each one, so the cost is set by the table's size "
                "rather than by how many rows match. A filter that returns three rows out "
                "of a million costs the same as one that returns all million. Selectivity "
                "does not rescue it — being more specific makes the query no faster, only "
                "the result smaller."
            ),
            suggestions=[
                Suggestion(
                    description=(
                        f"Add an index on `{table}.{column}` if this filter is on a hot "
                        f"path. The index-simulation stage can measure the actual cost "
                        f"change with HypoPG before anyone commits to it."
                    ),
                    sql=f"CREATE INDEX idx_{table.lower()}_{column.lower()} ON {table} ({column});",
                )
            ],
        )


register(UnindexedFilterRule())
