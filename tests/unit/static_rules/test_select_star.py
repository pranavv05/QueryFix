"""SelectStarRule — flags `SELECT *`, stays quiet on named projections."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from queryfix.models.finding import Severity
from queryfix.pipeline.static_rules import RuleContext, SelectStarRule

FLAGGED = [
    pytest.param("SELECT * FROM orders", id="planted-bug-export-all-orders"),
    pytest.param("SELECT * FROM orders WHERE id = 1", id="star-with-where"),
    pytest.param("SELECT o.* FROM orders o", id="qualified-star"),
    pytest.param("SELECT * FROM orders LIMIT 10", id="star-with-limit"),
    pytest.param(
        "SELECT id, (SELECT * FROM order_items WHERE order_id = o.id) FROM orders o",
        id="star-in-subquery",
    ),
]

NOT_FLAGGED = [
    pytest.param(
        "SELECT id, order_number, status, total_amount FROM orders WHERE placed_at >= '2024-01-01'",
        id="false-positive-guard-export-recent-orders",
    ),
    pytest.param("SELECT COUNT(*) FROM orders", id="count-star-is-not-a-projection"),
    pytest.param("SELECT COUNT(*), SUM(total_amount) FROM orders", id="multiple-aggregates"),
    pytest.param("UPDATE orders SET status = 'X' WHERE id = 1", id="update-has-no-projection"),
]


@pytest.mark.parametrize("sql", FLAGGED)
def test_flags_select_star(sql: str, make_context: Callable[..., RuleContext]) -> None:
    findings = SelectStarRule().check(make_context(sql))

    assert len(findings) == 1
    assert findings[0].rule_id == "select-star"
    assert findings[0].severity is Severity.MEDIUM


@pytest.mark.parametrize("sql", NOT_FLAGGED)
def test_does_not_flag_named_projections(
    sql: str, make_context: Callable[..., RuleContext]
) -> None:
    assert SelectStarRule().check(make_context(sql)) == []


def test_finding_explains_why_not_just_what(
    make_context: Callable[..., RuleContext],
) -> None:
    # The impact field is the reason a reviewer acts. Asserting it is non-empty and
    # distinct from the explanation guards against a rule that only restates its
    # own match, which would render as a report nobody can act on.
    finding = SelectStarRule().check(make_context("SELECT * FROM orders"))[0]

    assert finding.impact
    assert finding.impact != finding.explanation
    assert finding.suggestions
    assert finding.suggestions[0].description


def test_finding_is_anchored_to_the_source(
    make_context: Callable[..., RuleContext],
) -> None:
    finding = SelectStarRule().check(make_context("SELECT * FROM orders"))[0]

    assert finding.provenance.file == "Repo.java"
    assert finding.provenance.line == 42
    assert finding.query_id == "q1"
