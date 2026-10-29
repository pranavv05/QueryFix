"""Tests for p6spy statement-log parsing.

The N+1 stage leans on this log as its strongest evidence, so the parser is held
to the format the sandbox actually produces, not an idealised version of it.
"""

from __future__ import annotations

from queryfix.integrations.p6spy import (
    Statement,
    find_repeated_statements,
    normalize_sql,
    parse_statement_log,
)

N_PLUS_ONE_TABLE = "orders"


def test_parses_every_line_of_a_real_log(nplus1_statement_log: str) -> None:
    statements = parse_statement_log(nplus1_statement_log)

    assert len(statements) == 10
    assert all(s.category == "statement" for s in statements)
    assert all(s.timestamp_ms > 0 for s in statements)


def test_skips_malformed_lines_instead_of_raising() -> None:
    # A log truncated mid-write should still yield the statements that landed.
    lines = [
        "1785923413922|1|statement|SELECT 1",
        "",
        "not-a-log-line",
        "abc|1|statement|SELECT 2",  # non-numeric timestamp
        "1785923413925|x|statement|SELECT 3",  # non-numeric elapsed
        "1785923413926|2|statement",  # truncated mid-line
        "1785923413927|3|statement|SELECT 4",
    ]
    text = "\n".join(lines)

    statements = parse_statement_log(text)

    assert [s.sql for s in statements] == ["SELECT 1", "SELECT 4"]


def test_sql_containing_the_separator_survives() -> None:
    # `||` is Postgres concatenation; a naive split would truncate the statement.
    text = "1785923413922|1|statement|SELECT a || '|' || b FROM t"

    (statement,) = parse_statement_log(text)

    assert statement.sql == "SELECT a || '|' || b FROM t"


def test_normalize_replaces_literals_with_placeholders() -> None:
    first = normalize_sql("SELECT * FROM orders WHERE customer_id = 42")
    second = normalize_sql("SELECT * FROM orders WHERE customer_id = 99")

    # Equality is the contract; the placeholder token itself is dialect-rendered
    # (`%s` for Postgres) and not something callers should depend on.
    assert first == second
    assert "42" not in first
    assert "99" not in second


def test_a_bom_does_not_swallow_the_first_statement() -> None:
    # Round-tripping a log through a Windows shell can prepend a BOM.
    text = "﻿1785923413922|1|statement|SELECT 1\n1785923413923|2|statement|SELECT 2"

    statements = parse_statement_log(text)

    assert [s.sql for s in statements] == ["SELECT 1", "SELECT 2"]


def test_normalize_does_not_confuse_identifiers_for_literals() -> None:
    # A quoted identifier is not a value; rewriting it would merge two genuinely
    # different statements into one shape.
    normalized = normalize_sql("SELECT \"count\" FROM orders WHERE status = 'PAID'")

    assert "count" in normalized
    assert "PAID" not in normalized


def test_normalize_leaves_unparseable_input_intact() -> None:
    # Conservative fallback: group only with byte-identical siblings.
    assert normalize_sql("!!! not sql at all") == "!!! not sql at all"


def test_finds_the_n_plus_one_shape(nplus1_statement_log: str) -> None:
    groups = find_repeated_statements(parse_statement_log(nplus1_statement_log))

    worst = groups[0]
    assert N_PLUS_ONE_TABLE in worst.normalized_sql
    assert worst.count == 6
    # Every execution carried a different bind value — the N+1 signature.
    assert worst.distinct_variants == worst.count


def test_repeated_identical_statement_is_not_mistaken_for_an_n_plus_one(
    nplus1_statement_log: str,
) -> None:
    groups = find_repeated_statements(parse_statement_log(nplus1_statement_log))

    show = next(g for g in groups if "search_path" in g.normalized_sql)

    # Ran twice, but with identical text: a redundant lookup, not a fan-out. The
    # distinction is what stops the report calling connection setup an N+1.
    assert show.count == 2
    assert show.distinct_variants == 1


def test_single_executions_are_not_reported(nplus1_statement_log: str) -> None:
    groups = find_repeated_statements(parse_statement_log(nplus1_statement_log))

    # The unbounded-export and unindexed-country fixtures each ran once here;
    # they are single-query smells for the static and plan stages, not N+1s.
    assert all(group.count >= 2 for group in groups)
    assert not any("country" in group.normalized_sql for group in groups)


def test_row_level_categories_are_excluded() -> None:
    # `resultset` is one line per returned row and would swamp the statements.
    statements = [
        Statement(timestamp_ms=1, elapsed_ms=0, category="resultset", sql="SELECT 1"),
        Statement(timestamp_ms=2, elapsed_ms=0, category="resultset", sql="SELECT 1"),
    ]

    assert find_repeated_statements(statements) == []


def test_groups_are_ordered_worst_first() -> None:
    statements = [
        Statement(timestamp_ms=i, elapsed_ms=1, category="statement", sql=f"SELECT {i}")
        for i in range(5)
    ] + [
        Statement(timestamp_ms=100, elapsed_ms=1, category="statement", sql="SELECT x FROM t"),
        Statement(timestamp_ms=101, elapsed_ms=1, category="statement", sql="SELECT x FROM t"),
    ]

    groups = find_repeated_statements(statements)

    assert [g.count for g in groups] == sorted((g.count for g in groups), reverse=True)
    assert groups[0].count == 5


def test_mean_elapsed_is_reported_per_execution() -> None:
    statements = [
        Statement(timestamp_ms=1, elapsed_ms=4, category="statement", sql="SELECT 1"),
        Statement(timestamp_ms=2, elapsed_ms=6, category="statement", sql="SELECT 2"),
    ]

    (group,) = find_repeated_statements(statements)

    assert group.total_elapsed_ms == 10
    assert group.mean_elapsed_ms == 5.0
