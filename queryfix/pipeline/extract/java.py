"""Lexically-safe Java ``@Query`` and Spring Data method extraction."""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass

from queryfix.models.query import ExtractedQuery, Provenance, QueryKind, SourceFile
from queryfix.pipeline.extract.base import query_id, symbol_query_id
from queryfix.pipeline.extract.derived import (
    entity_table,
    parse_derived_query,
    render_derived_query,
)
from queryfix.pipeline.extract.java_source import JavaSource

__all__ = ["JavaExtractor", "extract_java"]


class JavaExtractor:
    """Registered adapter for Java source files."""

    def extract(self, source: SourceFile) -> list[ExtractedQuery]:
        return extract_java(source.path, source.content)


QUERY_START = re.compile(r"@Query\b")
REPOSITORY_INTERFACE = re.compile(
    r"\binterface\s+(?P<repository>[A-Za-z][A-Za-z0-9]*Repository)\b[^{}]*\{"
)
DERIVED_METHOD_DECLARATION = re.compile(
    r"(?m)^[^\n;{}]*\b(?P<method>(?:find|read|get|query|search|stream|count|exists|delete|remove)"
    r"(?:First|Top)?\d*By[A-Za-z0-9_]+)\s*\([^;{}]*\)\s*;"
)


@dataclass(frozen=True)
class _Candidate:
    offset: int
    line: int
    kind: QueryKind
    text: str
    symbol: str | None = None


def extract_java(path: str, content: str) -> list[ExtractedQuery]:
    """Extract all supported annotation and derived queries in source order."""
    source = JavaSource.of(content)
    annotations = _annotations(source)
    candidates = [
        *[
            _Candidate(
                offset,
                source.line_at(value_start),
                QueryKind.SQL if native else QueryKind.JPQL,
                text,
            )
            for offset, text, value_start, native, _ in annotations
        ],
        *_derived_candidates(source, _annotation_extents(source)),
    ]
    candidates.sort(key=lambda candidate: candidate.offset)
    return [
        ExtractedQuery(
            id=symbol_query_id(path, candidate.symbol)
            if candidate.symbol
            else query_id(path, ordinal),
            kind=candidate.kind,
            text=candidate.text,
            provenance=Provenance(file=path, line=candidate.line, symbol=candidate.symbol),
        )
        for ordinal, candidate in enumerate(candidates, start=1)
    ]


def _annotations(source: JavaSource) -> list[tuple[int, str, int, bool, int]]:
    """Parse annotations via scanner-provided structure, never raw regex nesting."""
    result: list[tuple[int, str, int, bool, int]] = []
    for match in QUERY_START.finditer(source.code):
        if not source.is_code(match.start()):
            continue
        parsed = _parse_annotation(source, match.end())
        if parsed is not None:
            text, value_start, native, end = parsed
            result.append((match.start(), text, value_start, native, end))
    return result


def _annotation_extents(source: JavaSource) -> list[tuple[int, int]]:
    """All syntactically bounded annotations suppress a decorated derived name."""
    result: list[tuple[int, int]] = []
    for match in QUERY_START.finditer(source.code):
        if not source.is_code(match.start()):
            continue
        index = match.end()
        while index < len(source.text) and source.text[index].isspace():
            index += 1
        if source.text[index : index + 1] == "(":
            close = source.matching_bracket(index)
            if close is not None:
                result.append((match.start(), close + 1))
    return result


def _parse_annotation(source: JavaSource, after_name: int) -> tuple[str, int, bool, int] | None:
    open_paren = after_name
    while open_paren < len(source.text) and source.text[open_paren].isspace():
        open_paren += 1
    if source.text[open_paren : open_paren + 1] != "(":
        return None
    close_paren = source.matching_bracket(open_paren)
    if close_paren is None:
        return None
    value: tuple[str, int] | None = None
    native = False
    for argument_start, argument_end in _arguments(source, open_paren + 1, close_paren):
        code = source.code[argument_start:argument_end]
        stripped = code.strip()
        start = argument_start + len(code) - len(code.lstrip())
        if re.fullmatch(r"nativeQuery\s*=\s*(true|false)", stripped, re.DOTALL):
            native = stripped.rsplit("=", 1)[1].strip() == "true"
            continue
        if re.match(r"value\s*=", stripped):
            equals = source.code.find("=", start, argument_end)
            start = equals + 1
            while start < argument_end and source.text[start].isspace():
                start += 1
        literal = _literal(source, start, argument_end)
        if literal is not None and value is None:
            value = literal
    if value is None:
        return None
    return value[0], value[1], native, close_paren + 1


def _arguments(source: JavaSource, start: int, end: int) -> list[tuple[int, int]]:
    """Split on commas only at the annotation's top lexical level."""
    result: list[tuple[int, int]] = []
    argument_start, depth = start, 0
    for index in range(start, end):
        character = source.structure[index]
        if character in "([{":
            depth += 1
        elif character in ")]}":
            depth -= 1
        elif character == "," and depth == 0:
            result.append((argument_start, index))
            argument_start = index + 1
    result.append((argument_start, end))
    return result


def _literal(source: JavaSource, start: int, end: int) -> tuple[str, int] | None:
    """Decode one standalone Java string or text block; expressions stay unsupported."""
    if source.text.startswith('"""', start):
        close = source.text.find('"""', start + 3, end)
        if close < 0 or source.text[close + 3 : end].strip():
            return None
        text_start = start + 3
        if source.text.startswith("\r\n", text_start):
            text_start += 2
        elif source.text.startswith("\n", text_start):
            text_start += 1
        return source.text[text_start:close], text_start
    if source.text[start : start + 1] != '"':
        return None
    pieces: list[str] = []
    index = start + 1
    escapes = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f"}
    while index < end:
        character = source.text[index]
        if character == '"':
            return (
                ("".join(pieces), start + 1) if not source.text[index + 1 : end].strip() else None
            )
        if character in "\r\n":
            return None
        if character == "\\" and index + 1 < end:
            escaped = source.text[index + 1]
            pieces.append(escapes.get(escaped, escaped))
            index += 2
            continue
        pieces.append(character)
        index += 1
    return None


def _derived_candidates(source: JavaSource, annotated: list[tuple[int, int]]) -> list[_Candidate]:
    candidates: list[_Candidate] = []
    for interface in REPOSITORY_INTERFACE.finditer(source.code):
        close = source.matching_bracket(interface.end() - 1)
        if close is None:
            continue
        body_start, body_end = interface.end(), close
        table = entity_table(interface.group("repository"))
        for method in DERIVED_METHOD_DECLARATION.finditer(source.code[body_start:body_end]):
            start = body_start + method.start()
            if _is_annotated(start, annotated, source):
                continue
            if ")." in method.group(0):
                # Not a declaration: a `default` method's body calling a derived
                # method — `findByCustomerId(id).stream()...;` — satisfies the
                # declaration pattern too, since its own parens close before the
                # line's final `;` just as a parameter list's would. A real
                # parameter list never has `)` immediately followed by `.`: Java
                # allows no method chaining inside a signature, only annotation
                # arguments, generics, and parameter names. That is the cheapest
                # textual fact that tells the two apart without tracking brace
                # depth through the whole interface body.
                continue
            derived = parse_derived_query(method.group("method"))
            if derived is not None:
                candidates.append(
                    _Candidate(
                        start,
                        source.line_at(start),
                        QueryKind.SPRING_DATA_DERIVED,
                        render_derived_query(derived, table),
                        method.group("method"),
                    )
                )
    return candidates


def _is_annotated(method_start: int, annotated: list[tuple[int, int]], source: JavaSource) -> bool:
    index = bisect.bisect_right(annotated, (method_start, method_start)) - 1
    return (
        index >= 0
        and annotated[index][1] <= method_start
        and source.is_blank_between(annotated[index][1], method_start)
    )
