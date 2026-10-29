"""NoLimitRule — flags unbounded full-table reads only.

The negative cases carry most of the weight here. This rule has four exclusions,
and each one exists because without it the rule fires on correct code.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from queryfix.models.finding import Severity
from queryfix.pipeline.static_rules import NoLimitRule, RuleContext

FLAGGED = [
    pytest.param("SELECT * FROM orders", id="planted-bug-export-all-orders"),
    pytest.param("SELECT id, order_number FROM orders", id="named-columns-still-unbounded"),
    pytest.param(
        "SELECT id FROM orders ORDER BY placed_at DESC",
        id="order-by-does-not-bound-the-result",
    ),
    pytest.param(
        "SELECT status, COUNT(*) FROM orders GROUP BY status",
        id="group-by-returns-one-row-per-group-not-one-row",
    ),
    pytest.param(
        "SELECT a.id FROM a JOIN b ON a.id = b.a_id",
        id="join-relating-two-tables-selects-nothing",
    ),
    pytest.param(
        "SELECT a.id FROM a LEFT JOIN b ON a.id = b.a_id AND b.kind = 'X'",
        id="outer-join-on-picks-right-rows-every-left-row-survives",
    ),
]

NOT_FLAGGED = [
    pytest.param(
        "SELECT id, order_number, status, total_amount FROM orders WHERE placed_at >= '2024-01-01'",
        id="false-positive-guard-export-recent-orders",
    ),
    pytest.param("SELECT * FROM orders LIMIT 100", id="bounded-by-limit"),
    pytest.param("SELECT * FROM orders FETCH FIRST 10 ROWS ONLY", id="bounded-by-fetch-first"),
    pytest.param("SELECT COUNT(*) FROM orders", id="aggregate-returns-a-single-row"),
    pytest.param("SELECT COUNT(*), SUM(total_amount) FROM orders", id="all-aggregates-single-row"),
    pytest.param("SELECT SUM(total_amount) AS total FROM orders", id="aliased-aggregate"),
    pytest.param("SELECT 1", id="no-from-clause-reads-nothing"),
    pytest.param("SELECT now()", id="function-call-reads-no-table"),
    pytest.param(
        "SELECT id FROM orders WHERE customer_id IN (SELECT id FROM customers)",
        id="inner-select-is-bounded-by-its-use",
    ),
    pytest.param("UPDATE orders SET status = 'X'", id="write-is-missing-wheres-job"),
    pytest.param(
        "SELECT b FROM BulkFileBatch b INNER JOIN b.bulkPaymentFile f ON f.fileNumber = :fileNumber",
        id="false-positive-guard-filter-written-in-an-inner-join-on",
    ),
    pytest.param(
        "SELECT a.* FROM a JOIN b ON a.id = b.id AND b.file_number = ?",
        id="inner-join-on-with-a-bound-parameter-filters",
    ),
]


@pytest.mark.parametrize("sql", FLAGGED)
def test_flags_unbounded_scans(sql: str, make_context: Callable[..., RuleContext]) -> None:
    findings = NoLimitRule().check(make_context(sql))

    assert len(findings) == 1
    assert findings[0].rule_id == "no-limit"
    assert findings[0].severity is Severity.HIGH


@pytest.mark.parametrize("sql", NOT_FLAGGED)
def test_does_not_flag_bounded_queries(sql: str, make_context: Callable[..., RuleContext]) -> None:
    assert NoLimitRule().check(make_context(sql)) == []


def test_subquery_without_from_is_not_attributed_to_the_outer_select(
    make_context: Callable[..., RuleContext],
) -> None:
    # The outer SELECT reads no table; only the inner one does. Using find(exp.From)
    # instead of reading the statement's own args would flag the outer query for a
    # scan it never performs.
    assert NoLimitRule().check(make_context("SELECT (SELECT 1 FROM orders)")) == []


def test_names_the_table_and_explains_the_growth_problem(
    make_context: Callable[..., RuleContext],
) -> None:
    finding = NoLimitRule().check(make_context("SELECT * FROM orders"))[0]

    assert "orders" in finding.explanation
    assert finding.impact != finding.explanation
    assert finding.suggestions
