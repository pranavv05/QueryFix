"""Stage 2 (SQL) — statement splitting, provenance, and malformed input.

The extract stage had no tests of its own, which is the worst place in this pipeline
for a gap: every finding's ``file:line`` anchor and every quoted snippet in the PR
comment comes from here. A rule that fires correctly on the wrong line, or quotes SQL
the author never wrote, is a bug a reviewer blames the whole tool for.

Three defects these tests were written against, all of the same shape — ``spans`` and
``statements`` are computed by two different sqlglot passes, and pairing them by
position desynchronizes as soon as a segment appears in one but not the other:

* a stray ``;`` gave every following statement the previous statement's text and line
* a comment-only segment did the same *and* surfaced the comment as a query
* a UTF-8 BOM made the entire file unanalyzable
"""

from __future__ import annotations

import pytest

from queryfix.models.query import QueryKind
from queryfix.pipeline.extract import extract_from_sql

BOM = "﻿"


# --------------------------------------------------------------------------------
# Statement splitting: only a parser knows where a statement ends.
# --------------------------------------------------------------------------------


def test_a_semicolon_inside_a_string_literal_is_not_a_boundary() -> None:
    queries = extract_from_sql("m.sql", "INSERT INTO a VALUES ('x;y'); SELECT * FROM b;")

    assert [query.text for query in queries] == [
        "INSERT INTO a VALUES ('x;y')",
        "SELECT * FROM b",
    ]


def test_a_dollar_quoted_body_is_one_statement_however_many_semicolons_it_holds() -> None:
    content = (
        "CREATE FUNCTION f() RETURNS int AS $$ BEGIN SELECT 1; SELECT 2; END; $$ "
        "LANGUAGE plpgsql;\n"
        "SELECT * FROM orders;"
    )

    queries = extract_from_sql("m.sql", content)

    assert len(queries) == 2
    assert queries[0].text.startswith("CREATE FUNCTION")
    assert queries[1].text == "SELECT * FROM orders"
    assert queries[1].provenance.line == 2


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        pytest.param("SELECT * FROM a", ["SELECT * FROM a"], id="no-trailing-semicolon"),
        pytest.param("SELECT * FROM a;", ["SELECT * FROM a"], id="trailing-semicolon"),
        pytest.param("SELECT * FROM a;;;", ["SELECT * FROM a"], id="repeated-semicolons"),
    ],
)
def test_statement_terminators_do_not_invent_statements(content: str, expected: list[str]) -> None:
    assert [query.text for query in extract_from_sql("m.sql", content)] == expected


@pytest.mark.parametrize(
    "content",
    [
        pytest.param("", id="empty"),
        pytest.param("   \n\t  \n", id="whitespace"),
        pytest.param(";;;", id="semicolons-only"),
        pytest.param("-- just a note", id="line-comment-only"),
        pytest.param("/* just a note */", id="block-comment-only"),
        pytest.param(BOM, id="bom-only"),
    ],
)
def test_content_with_no_statements_yields_no_candidates(content: str) -> None:
    # An empty result is not a degradation — there was nothing to analyze. Emitting a
    # phantom candidate here would put a comment in the PR report as if it were SQL.
    assert extract_from_sql("m.sql", content) == []


# --------------------------------------------------------------------------------
# Regression: spans/statements desync. Each of these silently corrupted provenance.
# --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("content", "expected_lines"),
    [
        pytest.param(";SELECT * FROM a;\nSELECT * FROM b;", [1, 2], id="leading-semicolon"),
        pytest.param("SELECT * FROM a;;\nSELECT * FROM b;", [1, 2], id="double-semicolon"),
        pytest.param(
            "SELECT * FROM a;\n-- a note\n;\nSELECT * FROM b;", [1, 4], id="comment-segment"
        ),
    ],
)
def test_an_empty_segment_does_not_shift_the_statements_after_it(
    content: str, expected_lines: list[int]
) -> None:
    """The defect: statements were paired with spans by ``enumerate`` position.

    An empty segment produces a parse entry but no tokens, so every statement after it
    inherited the *previous* statement's text and line — silently, with no error.
    """
    queries = extract_from_sql("m.sql", content)

    assert [query.text for query in queries] == ["SELECT * FROM a", "SELECT * FROM b"]
    assert [query.provenance.line for query in queries] == expected_lines
    assert [query.id for query in queries] == ["m.sql:1", "m.sql:2"]


@pytest.mark.parametrize(
    "content",
    [
        pytest.param("SELECT 1; -- trailing note", id="trailing-line-comment"),
        pytest.param("SELECT 1; /* trailing note */", id="trailing-block-comment"),
    ],
)
def test_a_trailing_comment_is_not_extracted_as_a_query(content: str) -> None:
    # sqlglot parses a comment-only segment into a Semicolon node carrying the comment.
    # Treated as a statement, it became a candidate whose text was the rendered comment.
    queries = extract_from_sql("m.sql", content)

    assert [query.text for query in queries] == ["SELECT 1"]


def test_a_byte_order_mark_does_not_make_the_file_unanalyzable() -> None:
    # Editors on Windows write a BOM by default. It is not whitespace, so it reached
    # the tokenizer and failed the parse of the whole file.
    queries = extract_from_sql("m.sql", f"{BOM}SELECT * FROM orders;")

    assert [query.text for query in queries] == ["SELECT * FROM orders"]
    assert queries[0].parse_error is None
    assert queries[0].provenance.line == 1


def test_a_byte_order_mark_inside_a_literal_is_left_alone() -> None:
    # Only a *leading* BOM is an encoding artefact; one inside a string is data.
    (query,) = extract_from_sql("m.sql", f"SELECT '{BOM}' FROM t")

    assert BOM in query.text


# --------------------------------------------------------------------------------
# Provenance: what a finding's file:line anchor is built from.
# --------------------------------------------------------------------------------


def test_line_numbers_survive_headers_blank_lines_and_comments() -> None:
    content = (
        "-- migration 003\n"  # 1
        "-- author: someone\n"  # 2
        "\n"  # 3
        "SELECT * FROM orders;\n"  # 4
        "\n"  # 5
        "-- and the write\n"  # 6
        "UPDATE customers SET tier = 'gold';\n"  # 7
    )

    queries = extract_from_sql("m.sql", content)

    assert [query.provenance.line for query in queries] == [4, 7]


def test_line_numbers_are_correct_with_windows_line_endings() -> None:
    queries = extract_from_sql("m.sql", "SELECT * FROM a;\r\nSELECT * FROM b;\r\n")

    assert [query.provenance.line for query in queries] == [1, 2]


def test_statements_sharing_a_line_both_report_that_line() -> None:
    queries = extract_from_sql("m.sql", "SELECT * FROM a; SELECT * FROM b;")

    assert [query.provenance.line for query in queries] == [1, 1]


def test_the_path_is_recorded_verbatim_as_provenance() -> None:
    (query,) = extract_from_sql("db/migration/V3__add_orders.sql", "SELECT 1")

    assert query.provenance.file == "db/migration/V3__add_orders.sql"
    assert query.id == "db/migration/V3__add_orders.sql:1"


def test_ids_are_unique_and_dense_across_a_file() -> None:
    content = ";SELECT 1;; -- note\nSELECT 2;\nSELECT 3;"

    ids = [query.id for query in extract_from_sql("m.sql", content)]

    assert ids == ["m.sql:1", "m.sql:2", "m.sql:3"]
    assert len(set(ids)) == len(ids)


# --------------------------------------------------------------------------------
# The text contract: what the PR comment will quote back at the author.
# --------------------------------------------------------------------------------


def test_text_is_the_query_as_written_not_as_re_rendered() -> None:
    # Rendering from the AST rewrites what the reviewer sees: `Customer c` becomes
    # `Customer AS c`. A report quoting SQL the author never wrote is hard to trust.
    (query,) = extract_from_sql("Repo.java", "SELECT c.id FROM Customer c WHERE c.tier = :tier")

    assert query.text == "SELECT c.id FROM Customer c WHERE c.tier = :tier"
    assert " AS " not in query.text
    assert ":tier" in query.text


def test_normalization_is_recorded_separately_from_the_original() -> None:
    (query,) = extract_from_sql("m.sql", "select  id  from   Orders")

    assert query.text == "select  id  from   Orders"
    assert query.normalized is not None
    assert query.normalized != query.text


def test_indentation_is_stripped_but_internal_layout_is_kept() -> None:
    (query,) = extract_from_sql("m.sql", "\n  SELECT id,\n         name\n  FROM t;\n")

    assert query.text.startswith("SELECT id,")
    assert "\n" in query.text


# --------------------------------------------------------------------------------
# Malformed input: unanalyzable, never raised.
# --------------------------------------------------------------------------------


def test_a_parse_error_yields_one_unanalyzable_candidate() -> None:
    (query,) = extract_from_sql("m.sql", "SELECT FROM WHERE")

    assert query.parse_error is not None
    assert query.normalized is None
    assert query.provenance.line == 1
    assert query.text == "SELECT FROM WHERE"


def test_a_tokenizer_error_is_reported_the_same_way_as_a_parse_error() -> None:
    # An unterminated string never reaches the parser: sqlglot raises TokenError, a
    # sibling of ParseError rather than a subclass, which escaped a `except ParseError`.
    (query,) = extract_from_sql("m.sql", "SELECT * FROM t WHERE name = 'unterminated")

    assert query.parse_error is not None
    assert query.provenance.line == 1


def test_one_bad_statement_costs_the_whole_file_and_says_so() -> None:
    # sqlglot cannot resynchronize after a parse error, so there are no statement
    # boundaries left to report against. The contract is that the file is surfaced as
    # one unanalyzable candidate rather than silently dropped — the runner is what
    # keeps the *other* files alive.
    queries = extract_from_sql("m.sql", "SELECT * FROM a;\nSELECT FROM WHERE;\nSELECT * FROM b;")

    assert len(queries) == 1
    assert queries[0].parse_error is not None


# --------------------------------------------------------------------------------
# Dialect, encoding, and size.
# --------------------------------------------------------------------------------


def test_the_dialect_is_honoured_and_recorded() -> None:
    (query,) = extract_from_sql("m.sql", "SELECT `col` FROM `orders`", dialect="mysql")

    assert query.dialect == "mysql"
    assert query.parse_error is None
    assert query.text == "SELECT `col` FROM `orders`"


def test_the_same_sql_can_be_valid_in_one_dialect_and_not_another() -> None:
    backticks = "SELECT `col` FROM `orders`"

    assert extract_from_sql("m.sql", backticks, dialect="mysql")[0].parse_error is None
    assert extract_from_sql("m.sql", backticks, dialect="postgres")[0].parse_error is not None


def test_the_kind_defaults_to_raw_sql_and_can_be_overridden() -> None:
    (default,) = extract_from_sql("m.sql", "SELECT 1")
    (native,) = extract_from_sql("R.java", "SELECT 1", kind=QueryKind.JPA_NATIVE)

    assert default.kind is QueryKind.RAW_SQL
    assert native.kind is QueryKind.JPA_NATIVE


def test_non_ascii_identifiers_and_literals_survive_extraction() -> None:
    content = "SELECT * FROM bestellungen WHERE stadt = 'Zürich';"

    (query,) = extract_from_sql("migraciónes/003_pedidos.sql", content)

    assert query.text == "SELECT * FROM bestellungen WHERE stadt = 'Zürich'"
    assert query.provenance.file == "migraciónes/003_pedidos.sql"
    assert query.parse_error is None


@pytest.mark.parametrize(
    ("label", "content"),
    [
        pytest.param(
            "wide-in-list",
            "SELECT id FROM t WHERE id IN (" + ",".join(str(n) for n in range(5000)) + ")",
            id="wide-in-list",
        ),
        pytest.param(
            "deep-nesting",
            "SELECT * FROM (" * 50 + "SELECT 1" + ") x" * 50,
            id="deep-nesting",
        ),
        pytest.param(
            "many-predicates",
            "SELECT id FROM t WHERE " + " AND ".join(f"c{n} = {n}" for n in range(500)),
            id="many-predicates",
        ),
    ],
)
def test_pathological_but_legal_sql_is_extracted_rather_than_exploding(
    label: str, content: str
) -> None:
    # Machine-generated SQL reaches this parser. A stack overflow here would be caught
    # by the runner's stage boundary and reported as a degraded run — which is a
    # silent loss of coverage, so it is worth knowing these shapes are handled.
    queries = extract_from_sql("generated.sql", content)

    assert len(queries) == 1, label
    assert queries[0].parse_error is None
