"""Tests for the Java tokenization boundary.

These cover the scanner on its own, without any notion of a query, because the
extractor's correctness now rests on it: if ``code`` still contains a comment or
``matching_bracket`` returns the wrong brace, every guarantee above it is void.
"""

from __future__ import annotations

import pytest

from queryfix.pipeline.extract.java_source import JavaRegionKind, JavaSource


def test_every_view_preserves_the_length_of_the_source() -> None:
    # The property the whole design rests on: an offset found in a mask is an
    # offset into the original, so provenance survives masking.
    content = '''class A {
    // a comment with "quotes" and { braces }
    String s = "a string with // and /* inside";
    /* block
       comment */
    String t = """
        text block
        """;
    char c = '}';
}
'''
    source = JavaSource.of(content)

    assert len(source.code) == len(content)
    assert len(source.structure) == len(content)
    assert source.text == content


@pytest.mark.parametrize(
    ("content", "needle"),
    [
        ("// @Query(x)\n", "@Query"),
        ("/* @Query(x) */\n", "@Query"),
        ("/** javadoc @Query(x) */\n", "@Query"),
        ("int a; // trailing\n", "trailing"),
    ],
)
def test_comment_text_is_absent_from_the_code_view(content: str, needle: str) -> None:
    assert needle not in JavaSource.of(content).code


def test_string_literals_survive_in_the_code_view_but_not_the_structure_view() -> None:
    # Annotation matching needs the literal — the query *is* the literal. Bracket
    # matching must not see the braces inside it.
    source = JavaSource.of('String s = "SELECT { } FROM t";')

    assert "SELECT { } FROM t" in source.code
    assert "SELECT" not in source.structure


def test_newlines_are_preserved_by_masking() -> None:
    # `(?m)^` anchors in the extractor's patterns depend on this.
    content = "/* one\ntwo\nthree */\nint a;\n"
    source = JavaSource.of(content)

    assert source.code.count("\n") == content.count("\n")


@pytest.mark.parametrize(
    ("content", "offset_of", "expected"),
    [
        ("int a;", "int", JavaRegionKind.CODE),
        ("// hi", "hi", JavaRegionKind.LINE_COMMENT),
        ("/* hi */", "hi", JavaRegionKind.BLOCK_COMMENT),
        ('String s = "hi";', "hi", JavaRegionKind.STRING),
        ('String s = """\nhi\n""";', "hi", JavaRegionKind.TEXT_BLOCK),
        ("char c = 'x';", "x'", JavaRegionKind.CHAR),
    ],
)
def test_kind_at_classifies_each_construct(
    content: str, offset_of: str, expected: JavaRegionKind
) -> None:
    source = JavaSource.of(content)

    assert source.kind_at(content.index(offset_of)) is expected


def test_a_text_block_is_not_read_as_an_empty_string_and_a_stray_quote() -> None:
    content = 'String s = """\nSELECT 1\n""";'
    source = JavaSource.of(content)

    assert source.kind_at(content.index("SELECT")) is JavaRegionKind.TEXT_BLOCK


def test_an_escaped_quote_does_not_end_a_string() -> None:
    content = 'String s = "a \\" b"; int after;'
    source = JavaSource.of(content)

    assert source.kind_at(content.index("b")) is JavaRegionKind.STRING
    assert source.kind_at(content.index("after")) is JavaRegionKind.CODE


def test_a_double_slash_inside_a_string_does_not_start_a_comment() -> None:
    content = 'String url = "http://example.com"; int after;'
    source = JavaSource.of(content)

    assert source.kind_at(content.index("after")) is JavaRegionKind.CODE
    assert "after" in source.code


def test_a_quote_inside_a_comment_does_not_start_a_string() -> None:
    content = "// it's fine\nint after;"
    source = JavaSource.of(content)

    assert source.kind_at(content.index("after")) is JavaRegionKind.CODE


def test_an_unterminated_string_ends_at_the_newline() -> None:
    # Java strings cannot span lines, so the rest of the file is still code —
    # swallowing it into a literal that never closes would blind the extractor
    # to everything after a typo.
    content = 'String s = "oops\nint after;'
    source = JavaSource.of(content)

    assert source.kind_at(content.index("after")) is JavaRegionKind.CODE


def test_an_unterminated_block_comment_runs_to_the_end_of_file() -> None:
    content = "/* oops\nint after;"
    source = JavaSource.of(content)

    assert source.kind_at(content.index("after")) is JavaRegionKind.BLOCK_COMMENT


def test_matching_bracket_skips_nested_pairs() -> None:
    content = "interface R { default void a() { int x; } void b(); }"
    source = JavaSource.of(content)

    close = source.matching_bracket(content.index("{"))

    assert close == len(content) - 1


def test_matching_bracket_ignores_braces_in_comments_and_literals() -> None:
    content = 'interface R { String s = "}"; /* } */ void b(); }'
    source = JavaSource.of(content)

    close = source.matching_bracket(content.index("{"))

    assert close == len(content) - 1


def test_matching_bracket_handles_parentheses() -> None:
    content = "@Query(value = f(1), nativeQuery = true) void a();"
    source = JavaSource.of(content)

    close = source.matching_bracket(content.index("("))

    assert close is not None
    assert content[close] == ")"
    assert close == content.index(")", content.index("nativeQuery"))


def test_matching_bracket_returns_none_when_unbalanced() -> None:
    source = JavaSource.of("interface R { void a();")

    assert source.matching_bracket(12) is None


def test_matching_bracket_returns_none_when_the_offset_is_not_a_bracket() -> None:
    source = JavaSource.of("int a;")

    assert source.matching_bracket(0) is None


@pytest.mark.parametrize(
    ("content", "needle", "expected"),
    [
        ("a\nb\nc", "a", 1),
        ("a\nb\nc", "b", 2),
        ("a\nb\nc", "c", 3),
        ("a\r\nb", "b", 2),
        ("\n\n\nx", "x", 4),
    ],
)
def test_line_at_reports_one_based_lines(content: str, needle: str, expected: int) -> None:
    assert JavaSource.of(content).line_at(content.index(needle)) == expected


def test_is_blank_between_treats_a_comment_as_whitespace() -> None:
    # A Java compiler does, so an annotation separated from its method by a
    # comment still decorates it.
    content = "@Query(x)\n  // why\n  void a();"
    source = JavaSource.of(content)

    assert source.is_blank_between(content.index(")") + 1, content.index("void"))


def test_is_blank_between_is_false_when_real_code_intervenes() -> None:
    content = "@Query(x)\nint field;\nvoid a();"
    source = JavaSource.of(content)

    assert not source.is_blank_between(content.index(")") + 1, content.index("void"))


def test_an_empty_source_is_handled() -> None:
    source = JavaSource.of("")

    assert source.code == ""
    assert source.structure == ""
    assert source.kind_at(0) is JavaRegionKind.CODE
    assert source.line_at(0) == 1


def test_a_source_with_no_comments_or_literals_is_returned_unchanged() -> None:
    content = "interface R { void a(); }"
    source = JavaSource.of(content)

    assert source.code == content
    assert source.structure == content
