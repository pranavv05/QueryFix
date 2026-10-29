"""Helpers for static-rule tests.

Rules take a parsed AST, so every test needs the same three lines of setup: parse
the SQL, wrap it with provenance, attach a schema. These fixtures own that so a
rule test reads as query-in / findings-out.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
import sqlglot

from queryfix.models.query import ExtractedQuery, Provenance, QueryKind
from queryfix.pipeline.static_rules import (
    UNKNOWN_SCHEMA,
    RuleContext,
    SchemaProvider,
    StaticSchemaProvider,
    TableSchema,
)


@pytest.fixture
def make_context() -> Callable[..., RuleContext]:
    """Build a RuleContext from SQL, as the engine would."""

    def _make(
        sql: str,
        schema: SchemaProvider = UNKNOWN_SCHEMA,
        dialect: str = "postgres",
    ) -> RuleContext:
        query = ExtractedQuery(
            id="q1",
            kind=QueryKind.RAW_SQL,
            text=sql,
            dialect=dialect,
            provenance=Provenance(file="Repo.java", line=42),
        )
        # `parse_one` is declared to return the wider `Expr`; `RuleContext` wants an
        # `Expression`. Narrowed here rather than ignored, which is the same split
        # the engine resolves in `RuleEngine._parse`.
        ast = sqlglot.parse_one(sql, read=dialect)
        assert isinstance(ast, sqlglot.exp.Expression)

        return RuleContext(query=query, ast=ast, schema=schema)

    return _make


@pytest.fixture
def sandbox_schema() -> StaticSchemaProvider:
    """The sandbox's real index layout, from V1__create_tables.sql.

    `country` is absent from `customers.indexed_columns` on purpose — that omission
    is the planted bug the unindexed-filter rule has to catch, and the healthy
    columns beside it are what it has to stay quiet about.
    """
    return StaticSchemaProvider(
        {
            "customers": TableSchema(
                indexed_columns=frozenset({"id", "email", "signed_up_at"}),
                column_types={
                    "id": "bigint",
                    "email": "varchar",
                    "full_name": "varchar",
                    "country": "varchar",
                    "loyalty_tier": "varchar",
                    "lifetime_value": "numeric",
                    "signed_up_at": "timestamptz",
                },
            ),
            "orders": TableSchema(
                indexed_columns=frozenset(
                    {"id", "order_number", "customer_id", "placed_at", "status"}
                ),
                column_types={
                    "id": "bigint",
                    "customer_id": "bigint",
                    "order_number": "varchar",
                    "status": "varchar",
                    "total_amount": "numeric",
                    "placed_at": "timestamptz",
                },
            ),
        }
    )
