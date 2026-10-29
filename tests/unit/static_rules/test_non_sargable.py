"""NonSargableRule — flags predicates an index cannot seek on.

Every negative case here is the mirror image of a positive one: a prefix instead of
a suffix, a function on the parameter instead of the column. Getting those wrong is
how a sargability rule becomes noise a team switches off.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from queryfix.models.finding import Severity
from queryfix.pipeline.static_rules import (
    NonSargableRule,
    RuleContext,
    StaticSchemaProvider,
)


@pytest.mark.parametrize(
    "sql",
    [
        pytest.param(
            "SELECT id FROM customers WHERE full_name LIKE '%smith'", id="leading-wildcard"
        ),
        pytest.param(
            "SELECT id FROM customers WHERE full_name LIKE '%smith%'", id="both-ends-wildcard"
        ),
        pytest.param(
            "SELECT id FROM customers WHERE full_name ILIKE '%smith'", id="leading-wildcard-ilike"
        ),
    ],
)
def test_flags_leading_wildcard_like(sql: str, make_context: Callable[..., RuleContext]) -> None:
    findings = NonSargableRule().check(make_context(sql))

    assert len(findings) == 1
    assert findings[0].severity is Severity.MEDIUM
    assert "wildcard" in findings[0].title


@pytest.mark.parametrize(
    "sql",
    [
        pytest.param(
            "SELECT id FROM customers WHERE full_name LIKE 'smith%'",
            id="false-positive-guard-trailing-wildcard-is-seekable",
        ),
        pytest.param(
            "SELECT id FROM customers WHERE full_name LIKE 'smith'", id="no-wildcard-at-all"
        ),
        pytest.param(
            "SELECT id FROM customers WHERE full_name LIKE :pattern",
            id="bound-parameter-value-is-unknown-statically",
        ),
    ],
)
def test_does_not_flag_seekable_like_patterns(
    sql: str, make_context: Callable[..., RuleContext]
) -> None:
    assert NonSargableRule().check(make_context(sql)) == []


@pytest.mark.parametrize(
    "sql",
    [
        pytest.param("SELECT id FROM customers WHERE LOWER(email) = 'a@b.c'", id="lower-on-column"),
        pytest.param(
            "SELECT id FROM orders WHERE DATE(placed_at) = '2024-01-01'", id="date-on-column"
        ),
        pytest.param(
            "SELECT id FROM customers WHERE COALESCE(country, 'US') = 'US'", id="coalesce-on-column"
        ),
    ],
)
def test_flags_functions_wrapping_a_filtered_column(
    sql: str, make_context: Callable[..., RuleContext]
) -> None:
    findings = NonSargableRule().check(make_context(sql))

    assert len(findings) == 1
    assert findings[0].rule_id == "non-sargable"


@pytest.mark.parametrize(
    "sql",
    [
        pytest.param(
            "SELECT id FROM customers WHERE email = LOWER('A@B.C')",
            id="false-positive-guard-function-on-the-parameter-side",
        ),
        pytest.param(
            "SELECT id FROM orders WHERE placed_at > now()", id="function-with-no-column-argument"
        ),
        pytest.param("SELECT id FROM customers WHERE email = 'a@b.c'", id="plain-indexed-equality"),
    ],
)
def test_does_not_flag_functions_applied_to_the_argument(
    sql: str, make_context: Callable[..., RuleContext]
) -> None:
    assert NonSargableRule().check(make_context(sql)) == []


def test_flags_an_explicit_cast_on_a_column(
    make_context: Callable[..., RuleContext],
) -> None:
    findings = NonSargableRule().check(
        make_context("SELECT id FROM orders WHERE CAST(id AS TEXT) = '5'")
    )

    assert len(findings) == 1
    assert "conversion" in findings[0].title


def test_flags_an_implicit_cast_when_the_schema_knows_the_column_type(
    make_context: Callable[..., RuleContext], sandbox_schema: StaticSchemaProvider
) -> None:
    # `order_number` is varchar; comparing it to a number makes the database coerce
    # the column, and nothing in the query text shows that happening.
    findings = NonSargableRule().check(
        make_context("SELECT id FROM orders WHERE order_number = 12345", schema=sandbox_schema)
    )

    assert len(findings) == 1
    assert "conversion" in findings[0].title


def test_does_not_flag_a_matched_type_comparison(
    make_context: Callable[..., RuleContext], sandbox_schema: StaticSchemaProvider
) -> None:
    for sql in (
        "SELECT id FROM orders WHERE order_number = 'A-1'",
        "SELECT id FROM orders WHERE customer_id = 12345",
    ):
        assert NonSargableRule().check(make_context(sql, schema=sandbox_schema)) == [], sql


def test_implicit_cast_detection_is_silent_without_a_schema(
    make_context: Callable[..., RuleContext],
) -> None:
    # Without the column's declared type there is no way to know a coercion happens.
    assert (
        NonSargableRule().check(make_context("SELECT id FROM orders WHERE order_number = 12345"))
        == []
    )


def test_no_where_clause_means_nothing_to_check(
    make_context: Callable[..., RuleContext],
) -> None:
    assert NonSargableRule().check(make_context("SELECT * FROM orders")) == []


def test_impact_explains_why_the_index_goes_unused(
    make_context: Callable[..., RuleContext],
) -> None:
    finding = NonSargableRule().check(
        make_context("SELECT id FROM customers WHERE full_name LIKE '%smith'")
    )[0]

    assert "index" in finding.impact.lower()
    assert finding.impact != finding.explanation
