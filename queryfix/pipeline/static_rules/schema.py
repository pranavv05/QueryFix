"""Schema context for rules that need to know what the database looks like.

Most static rules are self-contained: ``SELECT *`` is a ``SELECT *`` whatever the
schema. Two are not — a predicate is only "unindexed" relative to a real set of
indexes, and a cast is only implicit relative to a real column type.

The lookup is deliberately stubbed for now. :data:`UNKNOWN_SCHEMA` answers "I do
not know" to everything, and rules that depend on it must then stay silent. That
asymmetry is the point: a rule with no schema must produce no findings rather than
guess, because "no index on that column" is unfalsifiable without the schema and
would fire on every query in the diff.

:class:`StaticSchemaProvider` is the real implementation for tests and for the
eventual caller in ``db/snapshot.py``, which will build one from the schema dump.
"""

from __future__ import annotations

from typing import Protocol

__all__ = [
    "UNKNOWN_SCHEMA",
    "SchemaProvider",
    "StaticSchemaProvider",
    "TableSchema",
    "UnknownSchema",
]

from pydantic import BaseModel, Field


class TableSchema(BaseModel):
    """One table's indexed columns and column types.

    ``indexed_columns`` holds every column that leads an index, since only a
    leading column can serve an equality or range predicate on its own. A composite
    index on ``(a, b)`` contributes ``a`` here, not ``b``.
    """

    indexed_columns: frozenset[str] = Field(default_factory=frozenset)
    column_types: dict[str, str] = Field(default_factory=dict)


class SchemaProvider(Protocol):
    """Answers schema questions, or admits it cannot."""

    def indexed_columns(self, table: str) -> frozenset[str] | None:
        """Leading indexed columns of ``table``, or None if the table is unknown."""
        ...

    def column_type(self, table: str, column: str) -> str | None:
        """Declared type of a column, or None if unknown."""
        ...


class UnknownSchema:
    """The stub: knows nothing, so schema-dependent rules stay silent.

    This is the default, which means :class:`UnindexedFilterRule` produces nothing
    until a caller supplies a real schema. That is the intended behaviour, not a
    gap to work around — see the module docstring.
    """

    def indexed_columns(self, table: str) -> frozenset[str] | None:
        return None

    def column_type(self, table: str, column: str) -> str | None:
        return None


class StaticSchemaProvider:
    """A schema held in memory, keyed by lower-cased table name.

    Identifiers are compared case-insensitively because unquoted identifiers fold
    to lower case in Postgres, so ``Customer`` in JPQL and ``customers`` in DDL
    should not miss each other on case alone.
    """

    def __init__(self, tables: dict[str, TableSchema]) -> None:
        self._tables = {name.lower(): schema for name, schema in tables.items()}

    def indexed_columns(self, table: str) -> frozenset[str] | None:
        schema = self._tables.get(table.lower())
        if schema is None:
            return None
        return frozenset(column.lower() for column in schema.indexed_columns)

    def column_type(self, table: str, column: str) -> str | None:
        schema = self._tables.get(table.lower())
        if schema is None:
            return None
        for name, declared in schema.column_types.items():
            if name.lower() == column.lower():
                return declared.lower()
        return None


#: Default provider. See the module docstring for why silence is correct here.
UNKNOWN_SCHEMA = UnknownSchema()
