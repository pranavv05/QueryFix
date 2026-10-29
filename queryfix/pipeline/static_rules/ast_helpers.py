"""Stage 3 — shared readings of a sqlglot AST.

Questions more than one rule has to answer, answered once. Two rules previously
carried their own copies of the alias resolution below, written slightly
differently; a rule asking "which table is this column on?" and getting a
different answer depending on which file it lives in is a bug waiting for the
third copy.

These are *readings*, not checks: each takes an AST and returns a fact about it,
with no notion of severity, findings, or provenance. That is what keeps them
reusable and what keeps :mod:`queryfix.pipeline.static_rules.base` to the
protocol and registry it is named for.
"""

from __future__ import annotations

from sqlglot import exp

__all__ = ["clause", "has_clause", "resolve_table", "table_aliases"]


def has_clause(node: exp.Expression, *keys: str) -> bool:
    """Whether a statement carries any of these clauses.

    Presence is tested by truthiness, not ``is not None``, because sqlglot marks an
    absent clause inconsistently: ``None`` in most slots but ``False`` in others —
    ``Delete.args["using"]`` is ``False`` for a plain ``DELETE``. An ``is not None``
    check reads as correct and silently treats every bare DELETE as scoped.

    Truthiness is safe here: ``exp.Expression`` defines neither ``__bool__`` nor
    ``__len__``, so a real clause node is always truthy.
    """
    return any(bool(node.args.get(key)) for key in keys)


def clause(node: exp.Expression, *keys: str) -> exp.Expression | None:
    """The first of these clauses present on a statement, as a node.

    Accepts several keys because sqlglot renames arg keys across versions — a
    ``SELECT``'s FROM clause is ``from_`` in 30.x and ``from`` before it.
    """
    for key in keys:
        value = node.args.get(key)
        if isinstance(value, exp.Expression):
            return value
    return None


def table_aliases(node: exp.Expression) -> dict[str, str]:
    """Map every alias and table name in a statement to its real table name.

    ``FROM customers c`` yields both ``c -> customers`` and
    ``customers -> customers``, so a predicate can qualify either way. Keys are
    lower-cased because unquoted identifiers fold to lower case in Postgres;
    values keep the case they were written in, so a finding quotes the table the
    way the author spelled it.
    """
    aliases: dict[str, str] = {}
    for table in node.find_all(exp.Table):
        real = table.name
        aliases[real.lower()] = real
        alias = table.alias
        if alias:
            aliases[alias.lower()] = real
    return aliases


def resolve_table(column: exp.Column, aliases: dict[str, str]) -> str | None:
    """Which table a column belongs to, or None when it cannot be known.

    An unqualified column is only resolvable when exactly one table is in play;
    with a join it could belong to either, and attributing it to the wrong one
    would produce a confident, wrong finding. None means "do not report", which
    is the same contract the schema provider uses for the same reason.
    """
    qualifier = column.table
    if qualifier:
        return aliases.get(qualifier.lower())

    distinct = set(aliases.values())
    if len(distinct) == 1:
        return next(iter(distinct))
    return None
