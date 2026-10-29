"""``SELECT *`` — projects every column, including ones nobody asked for."""

from __future__ import annotations

from sqlglot import exp

from queryfix.models.finding import Finding, Severity, Suggestion
from queryfix.models.query import QueryKind
from queryfix.pipeline.static_rules.base import RuleContext, register

__all__ = ["SelectStarRule"]


class SelectStarRule:
    """Flags ``SELECT *`` in any query type, at any nesting depth.

    Matches a bare ``*`` and a qualified ``t.*`` alike. ``COUNT(*)`` is not a
    projection of every column — the star there is an argument to an aggregate and
    reads no column data — so it is excluded.

    A Spring Data derived method (``findByFileNumber``) is excluded too. Its ``*`` is
    something QueryFix rendered, not something the author wrote: the framework
    loads the whole entity by contract, so there is no projection for the author to
    narrow and nothing in the diff to act on. Flagging it was the single largest
    source of noise in a pilot on a real codebase (79 of 88 findings), and a review
    bot that cries wolf gets muted.
    """

    rule_id = "select-star"

    def check(self, context: RuleContext) -> list[Finding]:
        if context.query.kind is QueryKind.SPRING_DATA_DERIVED:
            return []
        stars = [star for star in context.ast.find_all(exp.Star) if not self._is_count_star(star)]
        # A qualified `t.*` parses as a Column whose `this` is a Star, which the
        # Star walk above already reaches, so no separate pass is needed.
        if not stars:
            return []

        return [
            context.finding(
                rule_id=self.rule_id,
                severity=Severity.MEDIUM,
                title="Query selects every column with `SELECT *`",
                explanation=(
                    "The projection is `*`, so the query returns every column of every "
                    "row it matches rather than the columns the caller uses."
                ),
                impact=(
                    "Three costs, none of them visible in the query text. Wide rows move "
                    "bytes the caller discards, over the wire and through the buffer "
                    "cache. An index that covers the predicate can no longer satisfy the "
                    "query on its own, so an index-only scan degrades to a heap fetch per "
                    "row. And the result shape is defined by the table, not the query — "
                    "adding or reordering a column silently changes what callers receive, "
                    "which for positional access (`List<Object[]>`) is a live bug rather "
                    "than a compile error."
                ),
                suggestions=[
                    Suggestion(
                        description=(
                            "Project the columns the caller actually reads. That pins the "
                            "result shape against future schema changes and lets a covering "
                            "index serve the query without touching the heap."
                        )
                    )
                ],
            )
        ]

    @staticmethod
    def _is_count_star(star: exp.Star) -> bool:
        """True for the ``*`` in ``COUNT(*)`` and friends."""
        return isinstance(star.parent, exp.AggFunc)


register(SelectStarRule())
