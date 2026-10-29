"""MissingWhereRule — flags unqualified writes, not scoped ones."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from queryfix.models.finding import Severity
from queryfix.pipeline.static_rules import MissingWhereRule, RuleContext

FLAGGED = [
    pytest.param(
        "UPDATE Customer c SET c.loyaltyTier = :tier",
        id="planted-bug-promote-all-to-tier-jpql",
    ),
    pytest.param(
        "UPDATE customers SET loyalty_tier = 'GOLD'",
        id="update-no-where-sql",
    ),
    pytest.param("DELETE FROM orders", id="delete-no-where"),
    pytest.param(
        "UPDATE customers SET loyalty_tier = 'GOLD', lifetime_value = 0",
        id="multi-column-update-no-where",
    ),
]

NOT_FLAGGED = [
    pytest.param(
        "UPDATE Customer c SET c.loyaltyTier = :tier WHERE c.lifetimeValue >= :threshold",
        id="false-positive-guard-promote-high-value-customers",
    ),
    pytest.param(
        "UPDATE customers SET loyalty_tier = 'GOLD' WHERE id = 1",
        id="update-with-where",
    ),
    pytest.param("DELETE FROM orders WHERE id = 1", id="delete-with-where"),
    pytest.param(
        "DELETE FROM orders USING customers WHERE orders.customer_id = customers.id",
        id="delete-scoped-by-using-join",
    ),
    pytest.param("DELETE FROM orders LIMIT 10", id="delete-bounded-by-limit"),
    pytest.param("SELECT * FROM orders", id="select-is-not-this-rules-job"),
    pytest.param("TRUNCATE TABLE orders", id="truncate-is-explicitly-whole-table"),
]


@pytest.mark.parametrize("sql", FLAGGED)
def test_flags_unqualified_writes(sql: str, make_context: Callable[..., RuleContext]) -> None:
    findings = MissingWhereRule().check(make_context(sql))

    assert len(findings) == 1
    assert findings[0].rule_id == "missing-where"
    # Data loss, not slowness — this is the one static rule that should outrank a
    # plan finding in the report.
    assert findings[0].severity is Severity.CRITICAL


@pytest.mark.parametrize("sql", NOT_FLAGGED)
def test_does_not_flag_scoped_writes(sql: str, make_context: Callable[..., RuleContext]) -> None:
    assert MissingWhereRule().check(make_context(sql)) == []


def test_names_the_affected_table(make_context: Callable[..., RuleContext]) -> None:
    finding = MissingWhereRule().check(make_context("UPDATE customers SET loyalty_tier = 'GOLD'"))[
        0
    ]

    assert "customers" in finding.explanation


def test_update_and_delete_get_different_impact_text(
    make_context: Callable[..., RuleContext],
) -> None:
    # A rewrite and a removal are different consequences; a shared blurb that said
    # "affects every row" for both would tell the reviewer less than either.
    update = MissingWhereRule().check(make_context("UPDATE customers SET loyalty_tier = 'X'"))[0]
    delete = MissingWhereRule().check(make_context("DELETE FROM customers"))[0]

    assert "rewritten" in update.impact
    assert "deleted" in delete.impact
    assert update.impact != delete.impact


def test_a_mysql_joined_update_is_scoped_by_its_join(
    make_context: Callable[..., RuleContext],
) -> None:
    # Found in a real migration: `UPDATE p JOIN s ON ... SET ...` rewrites only the rows
    # the join matches, so "no WHERE" does not mean "every row".
    sql = "UPDATE payment AS p JOIN bulk_file AS s ON p.file_no = s.file_number SET p.t = s.t"

    assert MissingWhereRule().check(make_context(sql, dialect="mysql")) == []


def test_a_plain_mysql_update_with_no_where_still_fires(
    make_context: Callable[..., RuleContext],
) -> None:
    findings = MissingWhereRule().check(make_context("UPDATE payment SET t = 1", dialect="mysql"))

    assert [finding.rule_id for finding in findings] == ["missing-where"]
