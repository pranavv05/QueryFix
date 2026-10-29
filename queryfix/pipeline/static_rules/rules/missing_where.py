"""``UPDATE`` / ``DELETE`` with no ``WHERE`` — rewrites or removes every row."""

from __future__ import annotations

from sqlglot import exp

from queryfix.models.finding import Finding, Severity, Suggestion
from queryfix.pipeline.static_rules.base import RuleContext, has_clause, register

__all__ = ["MissingWhereRule"]


class MissingWhereRule:
    """Flags an unqualified write.

    Only ``UPDATE`` and ``DELETE`` are in scope. A ``SELECT`` with no ``WHERE`` is
    also worth reporting, but it is a different severity and a different fix, so it
    belongs to :class:`~queryfix.pipeline.static_rules.rules.no_limit.NoLimitRule`
    rather than being folded in here.

    ``TRUNCATE`` is deliberately not flagged: it is unambiguously whole-table by
    definition, so its author cannot have meant otherwise. This rule is about a
    write whose scope looks narrower than it is.
    """

    rule_id = "missing-where"

    #: Severity is CRITICAL rather than HIGH because the damage is not a slow query
    #: — it is incorrect data, and it is already committed by the time anyone reads
    #: the plan.
    _SEVERITY = Severity.CRITICAL

    def check(self, context: RuleContext) -> list[Finding]:
        node = context.ast
        if not isinstance(node, (exp.Update, exp.Delete)):
            return []

        if has_clause(node, "where"):
            return []

        # A write can be scoped by something other than a WHERE clause. Treating
        # these as unqualified would be a false positive: `DELETE FROM a USING b`
        # is constrained by the join, and a LIMIT bounds the row count directly.
        if has_clause(node, "using", "limit"):
            return []
        # MySQL's multi-table form, `UPDATE a JOIN b ON ... SET ...`, is likewise
        # confined to the rows the join matches; sqlglot hangs the joins on the
        # target table rather than on the statement.
        if isinstance(node.this, exp.Table) and node.this.args.get("joins"):
            return []

        verb = "UPDATE" if isinstance(node, exp.Update) else "DELETE"
        target = self._target_name(node) or "the target table"

        return [
            context.finding(
                rule_id=self.rule_id,
                severity=self._SEVERITY,
                title=f"{verb} has no WHERE clause and affects every row",
                explanation=(
                    f"This {verb} on `{target}` carries no WHERE clause and no other "
                    f"row-limiting construct, so its scope is the entire table."
                ),
                impact=(
                    "Every row is rewritten, not just the intended cohort, and the "
                    "previous values are gone once the transaction commits — there is no "
                    "cheap undo. Before it commits, the statement holds a row lock on "
                    "every row it has touched, so concurrent writers block behind it and "
                    "the lock set grows with the table. A statement of this shape is "
                    "almost always a predicate that was meant to be there and is not."
                    if verb == "UPDATE"
                    else "Every row is deleted, not just the intended cohort, and the rows "
                    "are gone once the transaction commits. Before it commits, the "
                    "statement holds a lock on every row it has touched, so concurrent "
                    "writers block behind it."
                ),
                suggestions=[
                    Suggestion(
                        description=(
                            "Add the predicate that identifies the intended rows. If the "
                            "whole table really is the target, say so explicitly in review "
                            "— and for a delete, consider TRUNCATE, which states that "
                            "intent in the statement itself."
                        )
                    )
                ],
            )
        ]

    @staticmethod
    def _target_name(node: exp.Expression) -> str | None:
        """Best-effort table name for the message; None when the shape is unusual."""
        target = node.this
        if isinstance(target, exp.Table):
            return target.name
        if isinstance(target, exp.Expression):
            found = target.find(exp.Table)
            if found is not None:
                return found.name
        return None


register(MissingWhereRule())
