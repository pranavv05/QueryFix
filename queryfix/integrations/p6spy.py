"""Parse p6spy statement logs captured from a running application.

Source-level analysis proves a query sits inside a loop. Only a statement log
proves how many times it actually ran — which is why this evidence carries so
much weight in the N+1 stage.

The log format is a contract with ``queryfix-sandbox``'s ``spy.properties``::

    <epoch millis>|<elapsed ms>|<category>|<sql>

Field order and separator must stay in step with that file. SQL may itself contain
``|`` (string concatenation), so the split is bounded to three separators and the
remainder is taken verbatim as the statement.
"""

from __future__ import annotations

from collections import defaultdict

import sqlglot
from pydantic import BaseModel, Field
from sqlglot import exp

__all__ = [
    "Statement",
    "StatementGroup",
    "find_repeated_statements",
    "normalize_sql",
    "parse_statement_log",
]

#: Categories worth analysing. `result`/`resultset` are one line per returned row
#: and say nothing about statement count; `info`/`debug` are not statements.
ANALYSABLE_CATEGORIES = frozenset({"statement", "batch"})

_FIELD_COUNT = 4


class Statement(BaseModel):
    """One statement as recorded by p6spy."""

    timestamp_ms: int
    elapsed_ms: int
    category: str
    sql: str


class StatementGroup(BaseModel):
    """A set of executions sharing one normalized statement shape.

    ``count`` is how often the shape ran; ``distinct_variants`` is how many
    *different* literal bindings were seen. When the two are equal and large, the
    caller is looking at the same query re-issued once per row — the signature of
    an N+1 rather than a genuinely repeated identical lookup, which would be a
    caching problem instead.
    """

    normalized_sql: str
    count: int
    distinct_variants: int
    total_elapsed_ms: int
    example_sql: str = Field(description="One raw statement, binds intact.")

    @property
    def mean_elapsed_ms(self) -> float:
        """Average time per execution; 0.0 for an empty group, which cannot occur."""
        return self.total_elapsed_ms / self.count if self.count else 0.0


def normalize_sql(sql: str, dialect: str = "postgres") -> str:
    """Replace literal values with placeholders so repeated shapes group together.

    Parsed with sqlglot and rewritten from the AST rather than pattern-matched —
    a textual substitution cannot tell a literal from the same characters inside
    a quoted identifier or a comment.

    Unparseable input (``SHOW search_path``, vendor-specific commands, a truncated
    line) is returned stripped and unchanged. Such a statement then groups only
    with byte-identical siblings, which is the correct conservative answer.

    The placeholder token is whatever the dialect renders — ``%s`` for Postgres.
    Only the grouping matters, so callers should compare normalized forms rather
    than look for a particular marker.
    """
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except sqlglot.ParseError:
        return sql.strip()

    if tree is None:
        return sql.strip()

    # sqlglot does not raise on everything it cannot model — it falls back to a
    # `Command` node holding the tail as an opaque Literal. Rewriting literals
    # there would turn `SHOW search_path` into `SHOW %s`, discarding the only part
    # that identifies the statement. Treat it as unparsed, as documented.
    if isinstance(tree, exp.Command):
        return sql.strip()

    # Collect before mutating: replacing nodes during the walk that produced them
    # invalidates the iterator.
    for literal in list(tree.find_all(exp.Literal)):
        literal.replace(exp.Placeholder())

    return tree.sql(dialect=dialect)


def parse_statement_log(text: str) -> list[Statement]:
    """Parse a p6spy log, skipping lines that do not match the expected format.

    Malformed lines are dropped rather than raised on: a log truncated mid-write
    (the app was killed, the disk filled) should still yield the statements that
    did land. This stage is evidence-gathering, not validation.
    """
    statements: list[Statement] = []

    # A log that has been through an editor or a Windows shell can arrive with a
    # BOM, which would make the first timestamp unparseable and silently drop the
    # earliest statement — the one most likely to matter.
    for line in text.lstrip("﻿").splitlines():
        if not line.strip():
            continue

        fields = line.split("|", _FIELD_COUNT - 1)
        if len(fields) != _FIELD_COUNT:
            continue

        raw_timestamp, raw_elapsed, category, sql = fields
        try:
            timestamp_ms = int(raw_timestamp)
            elapsed_ms = int(raw_elapsed)
        except ValueError:
            continue

        statements.append(
            Statement(
                timestamp_ms=timestamp_ms,
                elapsed_ms=elapsed_ms,
                category=category.strip(),
                sql=sql.strip(),
            )
        )

    return statements


def find_repeated_statements(
    statements: list[Statement],
    min_count: int = 2,
    categories: frozenset[str] = ANALYSABLE_CATEGORIES,
) -> list[StatementGroup]:
    """Group statements by normalized shape and return the repeated ones.

    Ordered by ``count`` descending, so the worst offender reads first.
    """
    by_shape: dict[str, list[Statement]] = defaultdict(list)
    for statement in statements:
        if statement.category in categories:
            by_shape[normalize_sql(statement.sql)].append(statement)

    groups = [
        StatementGroup(
            normalized_sql=shape,
            count=len(group),
            distinct_variants=len({s.sql for s in group}),
            total_elapsed_ms=sum(s.elapsed_ms for s in group),
            example_sql=group[0].sql,
        )
        for shape, group in by_shape.items()
        if len(group) >= min_count
    ]

    groups.sort(key=lambda g: g.count, reverse=True)
    return groups
