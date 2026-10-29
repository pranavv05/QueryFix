"""UnindexedFilterRule — needs a schema, and says nothing without one."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from queryfix.models.finding import Severity
from queryfix.pipeline.static_rules import (
    RuleContext,
    StaticSchemaProvider,
    UnindexedFilterRule,
)


def test_flags_the_planted_unindexed_country_filter(
    make_context: Callable[..., RuleContext], sandbox_schema: StaticSchemaProvider
) -> None:
    # The sandbox's JPQL fixture: `customers.country` is deliberately unindexed.
    findings = UnindexedFilterRule().check(
        make_context(
            "SELECT c FROM customers c WHERE c.country = 'US' ORDER BY c.signed_up_at DESC",
            schema=sandbox_schema,
        )
    )

    assert len(findings) == 1
    assert findings[0].rule_id == "unindexed-filter"
    assert findings[0].severity is Severity.HIGH
    assert "country" in findings[0].title


def test_suggests_a_usable_create_index_statement(
    make_context: Callable[..., RuleContext], sandbox_schema: StaticSchemaProvider
) -> None:
    finding = UnindexedFilterRule().check(
        make_context("SELECT id FROM customers WHERE country = 'US'", schema=sandbox_schema)
    )[0]

    assert finding.suggestions
    assert finding.suggestions[0].sql is not None
    assert "CREATE INDEX" in finding.suggestions[0].sql
    assert "country" in finding.suggestions[0].sql


@pytest.mark.parametrize(
    "sql",
    [
        pytest.param(
            "SELECT id FROM customers WHERE signed_up_at > '2024-01-01'",
            id="false-positive-guard-indexed-signed-up-at",
        ),
        pytest.param("SELECT id FROM customers WHERE email = 'a@b.c'", id="indexed-unique-email"),
        pytest.param("SELECT id FROM orders WHERE customer_id = 1", id="indexed-customer-id"),
        pytest.param(
            "SELECT id FROM orders WHERE status = 'NEW' AND placed_at > '2024-01-01'",
            id="both-predicates-indexed",
        ),
        pytest.param("SELECT id FROM customers", id="no-where-clause-at-all"),
    ],
)
def test_does_not_flag_indexed_filters(
    sql: str,
    make_context: Callable[..., RuleContext],
    sandbox_schema: StaticSchemaProvider,
) -> None:
    assert UnindexedFilterRule().check(make_context(sql, schema=sandbox_schema)) == []


def test_is_silent_without_a_schema(make_context: Callable[..., RuleContext]) -> None:
    # The default stub knows nothing. A rule that guessed here would fire on every
    # predicate in the diff, which is worse than reporting nothing.
    findings = UnindexedFilterRule().check(
        make_context("SELECT id FROM customers WHERE country = 'US'")
    )

    assert findings == []


def test_is_silent_for_a_table_the_schema_does_not_know(
    make_context: Callable[..., RuleContext], sandbox_schema: StaticSchemaProvider
) -> None:
    findings = UnindexedFilterRule().check(
        make_context("SELECT id FROM audit_log WHERE actor = 'x'", schema=sandbox_schema)
    )

    assert findings == []


def test_does_not_flag_a_column_wrapped_in_a_function(
    make_context: Callable[..., RuleContext], sandbox_schema: StaticSchemaProvider
) -> None:
    # `LOWER(country) = ?` is a sargability problem, not an unindexed-column one:
    # adding an index on `country` would not make it faster. Reporting it here too
    # would put two findings with contradictory fixes on one predicate.
    findings = UnindexedFilterRule().check(
        make_context("SELECT id FROM customers WHERE LOWER(country) = 'us'", schema=sandbox_schema)
    )

    assert findings == []


def test_reports_an_unqualified_column_when_only_one_table_is_in_play(
    make_context: Callable[..., RuleContext], sandbox_schema: StaticSchemaProvider
) -> None:
    findings = UnindexedFilterRule().check(
        make_context("SELECT id FROM customers WHERE country = 'US'", schema=sandbox_schema)
    )

    assert len(findings) == 1


def test_does_not_guess_an_unqualified_column_across_a_join(
    make_context: Callable[..., RuleContext], sandbox_schema: StaticSchemaProvider
) -> None:
    # `country` is unqualified and both tables are candidates. Attributing it to one
    # of them would be a confident guess, and the wrong guess produces a finding
    # naming a table the column does not belong to.
    findings = UnindexedFilterRule().check(
        make_context(
            "SELECT o.id FROM orders o JOIN customers c ON c.id = o.customer_id "
            "WHERE country = 'US'",
            schema=sandbox_schema,
        )
    )

    assert findings == []


def test_reports_each_column_once(
    make_context: Callable[..., RuleContext], sandbox_schema: StaticSchemaProvider
) -> None:
    findings = UnindexedFilterRule().check(
        make_context(
            "SELECT id FROM customers WHERE country = 'US' OR country = 'CA'",
            schema=sandbox_schema,
        )
    )

    assert len(findings) == 1


def test_flags_range_and_in_predicates_too(
    make_context: Callable[..., RuleContext], sandbox_schema: StaticSchemaProvider
) -> None:
    for sql in (
        "SELECT id FROM customers WHERE lifetime_value > 100",
        "SELECT id FROM customers WHERE country IN ('US', 'CA')",
        "SELECT id FROM customers WHERE lifetime_value BETWEEN 1 AND 2",
    ):
        findings = UnindexedFilterRule().check(make_context(sql, schema=sandbox_schema))
        assert len(findings) == 1, sql
