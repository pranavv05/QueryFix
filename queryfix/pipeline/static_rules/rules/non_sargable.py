"""Predicates an index cannot serve, even when the index exists.

"Sargable" — Search ARGument able — is the property of a predicate that a B-tree
can seek on. All three shapes here break it the same way: the index is built over
column values, and the predicate asks about something else (a suffix, a function's
output, a coerced type), so there is nothing to seek to.
"""

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

__all__ = ["NonSargableRule"]

_COMPARISONS = (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.In, exp.Between)

#: The subset an implicit cast can hide in: a range comparison against a text column
#: is a different smell, handled as non-sargable rather than as a cast.
#:
#: Annotated so ``find_all`` yields ``Expression``. Given several types it resolves to
#: their nearest common base — for these two, ``Binary``, which sits beside
#: ``Expression`` rather than under it and declares no ``args``, so the node could not
#: be handed to a helper that reads them.
_EQUALITY_COMPARISONS: tuple[type[exp.Expression], ...] = (exp.EQ, exp.NEQ)

#: Types that a numeric literal will not silently coerce into usefully.
_TEXT_TYPES = ("char", "text", "varchar", "citext", "uuid")


class NonSargableRule:
    """Flags leading-wildcard LIKE, a function wrapping a filtered column, and casts.

    Each check is written to require a *column* on the affected side, because the
    mirror-image shape is harmless and common:

    * ``name LIKE '%smith'`` cannot seek; ``name LIKE 'smith%'`` can, and is not
      flagged — a prefix is exactly what a B-tree is ordered by.
    * ``LOWER(email) = ?`` cannot seek; ``email = LOWER(?)`` can, because the
      function is applied to the argument, not the column.
    * ``CAST(id AS TEXT) = '5'`` cannot seek. A cast on the literal side is fine.
    """

    rule_id = "non-sargable"

    def check(self, context: RuleContext) -> list[Finding]:
        where = clause(context.ast, "where")
        if where is None:
            return []

        findings: list[Finding] = []
        findings.extend(self._leading_wildcards(context, where))
        findings.extend(self._wrapped_columns(context, where))
        findings.extend(self._casts_on_columns(context, where))
        return findings

    def _leading_wildcards(self, context: RuleContext, where: exp.Expression) -> list[Finding]:
        findings: list[Finding] = []
        for like in where.find_all(exp.Like, exp.ILike):
            if not isinstance(like.this, exp.Column):
                continue
            pattern = like.expression
            if not isinstance(pattern, exp.Literal) or not pattern.is_string:
                # A bound parameter's value is unknown at analysis time. It may well
                # start with `%`, but that is a runtime fact, not a static one.
                continue
            if not str(pattern.this).startswith("%"):
                continue

            findings.append(
                context.finding(
                    rule_id=self.rule_id,
                    severity=Severity.MEDIUM,
                    title=f"LIKE pattern starts with a wildcard on `{like.this.sql()}`",
                    explanation=(
                        f"The pattern `{pattern.this}` begins with `%`, so the predicate "
                        f"asks which values *end* with the literal part."
                    ),
                    impact=(
                        "A B-tree index is ordered by the start of the value, so a leading "
                        "wildcard gives it no prefix to seek to and the index cannot be "
                        "used for this predicate — even when one exists on the column. "
                        "Every row is read and pattern-matched instead, so the cost tracks "
                        "the table's size rather than the number of matches."
                    ),
                    suggestions=[
                        Suggestion(
                            description=(
                                "If this is substring search, use a purpose-built index "
                                "rather than a B-tree: a trigram index (`pg_trgm`) supports "
                                "`LIKE '%term%'`, and full-text search supports word "
                                "matching. If the search is really a suffix match, store a "
                                "reversed copy of the column and index that."
                            )
                        )
                    ],
                )
            )
        return findings

    def _wrapped_columns(self, context: RuleContext, where: exp.Expression) -> list[Finding]:
        findings: list[Finding] = []
        seen: set[str] = set()

        for comparison in where.find_all(*_COMPARISONS):
            for side in (comparison.this, comparison.args.get("expression")):
                if not isinstance(side, exp.Func) or isinstance(side, exp.Cast):
                    # Casts are reported separately, with their own explanation.
                    continue
                columns = list(side.find_all(exp.Column))
                if not columns:
                    # `email = LOWER('x')` — the function wraps a literal, which the
                    # planner folds. Nothing to report.
                    continue

                key = side.sql()
                if key in seen:
                    continue
                seen.add(key)

                column = columns[0]
                findings.append(
                    context.finding(
                        rule_id=self.rule_id,
                        severity=Severity.MEDIUM,
                        title=f"Filtered column is wrapped in `{side.key.upper()}(...)`",
                        explanation=(
                            f"The predicate compares `{key}` rather than `{column.sql()}` "
                            f"itself, so the filter is on the function's output."
                        ),
                        impact=(
                            "A plain index stores the column's values, not the function's "
                            "results, so the planner has nothing matching this predicate to "
                            "seek on and falls back to reading every row and calling the "
                            "function on each. The index on the column stays unused no "
                            "matter how selective the comparison is."
                        ),
                        suggestions=[
                            Suggestion(
                                description=(
                                    "Rewrite the predicate so the column stands alone, "
                                    "moving the transformation to the parameter side where "
                                    "possible. Where the expression is genuinely needed, "
                                    "index it directly with an expression index."
                                ),
                                sql=f"CREATE INDEX ... ON <table> (({key}));",
                            )
                        ],
                    )
                )
        return findings

    def _casts_on_columns(self, context: RuleContext, where: exp.Expression) -> list[Finding]:
        findings: list[Finding] = []

        for comparison in where.find_all(*_COMPARISONS):
            for side in (comparison.this, comparison.args.get("expression")):
                if isinstance(side, exp.Cast) and isinstance(side.this, exp.Column):
                    findings.append(
                        self._cast_finding(context, side.this, side.to.sql(), explicit=True)
                    )

        findings.extend(self._implicit_casts(context, where))
        return findings

    def _implicit_casts(self, context: RuleContext, where: exp.Expression) -> list[Finding]:
        """Text column compared to a numeric literal — a cast the author did not write.

        Needs the schema to know the column's type, so this is silent under the stub
        provider. Unlike an explicit ``CAST(...)``, there is nothing in the query
        text to see here; the coercion is inserted by the database.
        """
        findings: list[Finding] = []
        aliases = table_aliases(context.ast)

        for comparison in where.find_all(*_EQUALITY_COMPARISONS):
            column, literal = _column_and_literal(comparison)
            if column is None or literal is None or literal.is_string:
                continue

            table = resolve_table(column, aliases)
            if table is None:
                continue

            declared = context.schema.column_type(table, column.name)
            if declared is None or not declared.startswith(_TEXT_TYPES):
                continue

            findings.append(self._cast_finding(context, column, declared, explicit=False))
        return findings

    def _cast_finding(
        self,
        context: RuleContext,
        column: exp.Column,
        target_type: str,
        *,
        explicit: bool,
    ) -> Finding:
        if explicit:
            explanation = (
                f"`{column.sql()}` is wrapped in a cast to {target_type} before being "
                f"compared, so the comparison is against the converted value."
            )
        else:
            explanation = (
                f"`{column.sql()}` is declared {target_type} but is compared to a numeric "
                f"literal, so the database inserts a conversion to make the types match."
            )

        return context.finding(
            rule_id=self.rule_id,
            severity=Severity.MEDIUM,
            title=f"Comparison on `{column.sql()}` requires a type conversion",
            explanation=explanation,
            impact=(
                "An index is ordered by the column's stored type. Once a conversion sits "
                "between the column and the comparison, that ordering no longer matches "
                "what is being compared, so the index cannot be used and every row is read "
                "and converted. Because the conversion is inserted by the database rather "
                "than written in the query, this is invisible in review — the query looks "
                "like a simple indexed equality."
            ),
            suggestions=[
                Suggestion(
                    description=(
                        "Compare like with like: bind the parameter as the column's own "
                        "type, or fix the column's type if it is modelling a number as "
                        "text. Casting the parameter instead of the column keeps the index "
                        "usable."
                    )
                )
            ],
        )


def _column_and_literal(
    comparison: exp.Expression,
) -> tuple[exp.Column | None, exp.Literal | None]:
    left, right = comparison.this, comparison.args.get("expression")
    if isinstance(left, exp.Column) and isinstance(right, exp.Literal):
        return left, right
    if isinstance(right, exp.Column) and isinstance(left, exp.Literal):
        return right, left
    return None, None


register(NonSargableRule())
