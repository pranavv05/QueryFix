"""Stage 7 (structure) — read Java control flow, types, and call sites.

What this is, and what it deliberately is not
---------------------------------------------

N+1 detection needs facts a query extractor never has to establish: which
identifier holds a repository, which construct repeats, and whether a call sits
inside it. That is program structure, so this module reads program structure and
nothing else. It knows what a loop is; it does not know what a query is, and it
never decides that anything is a problem. Judgement lives in
:mod:`queryfix.pipeline.nplusone`.

It is not a Java parser, and the distinction matters enough to state plainly.
It tokenizes :attr:`~queryfix.pipeline.extract.java_source.JavaSource.structure`
— the view with comments *and* string-literal bodies blanked — and walks brace
structure with real bracket matching. Two consequences follow directly, and both
are load-bearing for a review bot:

* Java written inside a comment or a string literal cannot produce a call site, a
  loop, or a repository. It is not there to be matched.
* A brace inside a literal does not open or close a block, so a method body's
  extent is its real extent.

What it cannot do is resolve types it was never shown. A receiver whose declaring
interface is outside the pull request is resolved by import path when the caller
supplies a way to read it, and otherwise falls back to
:attr:`~queryfix.models.java_structure.RepositoryResolution.NAME_CONVENTION`,
which the detector treats as weaker evidence rather than as proof.

Why lexical rather than a parser
--------------------------------

``javalang`` was evaluated and rejected: its grammar is Java 8 and its last
release was 2020, so records, text blocks, ``var``, sealed types, and switch
expressions — ordinary in the JDK 17/21 services this must read — would fail to
parse, turning a real N+1 into silence. ``tree-sitter-java`` is the technically
correct answer and remains the migration target, as does the JavaParser sidecar
CLAUDE.md's tech-stack table names. Neither is a dependency today.

The boundary that makes either swap cheap is
:mod:`queryfix.models.java_structure`: this module's whole output is those
models, and the detector reads only those models. Replacing the analyzer means
producing the same contracts from a parse tree.

Known limits, all of which the detector is written to tolerate:

* Overload resolution is by name only — Java's argument-type dispatch is not
  modelled, so two overloads of one repository method are one method here.
* Alias tracking is deliberately shallow (see :func:`_local_handles`). Anything
  beyond a direct assignment from a known handle is not followed, because an
  unsound alias analysis produces confident nonsense.
* A method reference (``repo::findById``) is not read as a call. Whether it runs
  per element depends on what consumes it, which is not visible here.
"""

from __future__ import annotations

import bisect
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum

from queryfix.models.java_structure import (
    ArgumentDependency,
    EntityRelationship,
    InjectionKind,
    IterationContext,
    IterationKind,
    JavaFileStructure,
    JavaProgram,
    LazyAssociationAccess,
    MethodDeclaration,
    RepositoryCallSite,
    RepositoryDeclaration,
    RepositoryHandle,
    RepositoryMethodDeclaration,
    RepositoryResolution,
    SourceSpan,
    TypeDeclaration,
)
from queryfix.models.query import ExtractedQuery, SourceFile
from queryfix.pipeline.extract.java_source import JavaSource

__all__ = [
    "JAVA_EXTENSION",
    "ResolveJavaSource",
    "analyze_java_file",
    "analyze_java_program",
    "derive_import_path",
]

#: Files this analyzer claims. Kept beside the analyzer rather than imported from
#: the extractor registry: this stage picks its own inputs out of a mixed source
#: list, and does not participate in extraction's dispatch.
JAVA_EXTENSION = ".java"

#: Reads a repository-relative path, returning None when it does not exist.
#: Injected so cross-file resolution can reach files the pull request did not
#: change without this module knowing GitHub exists.
ResolveJavaSource = Callable[[str], str | None]

#: Spring Data's own base interfaces. A type extending any of these transitively
#: is a repository as a *fact*, which is what separates
#: `RepositoryResolution.DECLARED` from a guess about a name.
_SPRING_DATA_BASE_TYPES = frozenset(
    {
        "Repository",
        "CrudRepository",
        "ListCrudRepository",
        "PagingAndSortingRepository",
        "ListPagingAndSortingRepository",
        "JpaRepository",
        "JpaSpecificationExecutor",
        "MongoRepository",
        "ReactiveCrudRepository",
        "ReactiveMongoRepository",
        "R2dbcRepository",
        "ElasticsearchRepository",
        "CassandraRepository",
        "KeyValueRepository",
        "QuerydslPredicateExecutor",
        "RevisionRepository",
    }
)

#: Suffix used only as a *fallback* when a declaration was never seen. CLAUDE.md's
#: rule against guessing applies: this yields NAME_CONVENTION, never DECLARED.
_REPOSITORY_NAME_SUFFIX = "Repository"

#: JPA association annotations. The two collection-valued ones default to LAZY;
#: the two single-valued ones default to EAGER and must say `FetchType.LAZY`.
_LAZY_BY_DEFAULT = frozenset({"OneToMany", "ManyToMany"})
_EAGER_BY_DEFAULT = frozenset({"ManyToOne", "OneToOne"})
_ASSOCIATIONS = _LAZY_BY_DEFAULT | _EAGER_BY_DEFAULT

#: Operations that iterate whatever they are called on, whatever its type. None of
#: these exist on `Optional`, which is what makes them safe without type
#: resolution — see `_PER_ELEMENT_WITH_STREAM` for the ones that are not.
_PER_ELEMENT_ALWAYS = frozenset({"forEach", "forEachOrdered", "forEachRemaining", "removeIf"})

#: Operations that iterate *only* in a stream pipeline. `Optional.map` runs its
#: lambda at most once, and reading it as per-element is a false positive with a
#: plausible-looking loop attached, which is the worst kind. These therefore
#: require visible evidence of a stream in the same statement.
_PER_ELEMENT_WITH_STREAM = frozenset(
    {
        "map",
        "flatMap",
        "filter",
        "peek",
        "mapToObj",
        "mapToInt",
        "mapToLong",
        "mapToDouble",
        "mapToTop",
        "anyMatch",
        "allMatch",
        "noneMatch",
        "takeWhile",
        "dropWhile",
        "sorted",
        "collect",
        "reduce",
    }
)

#: What opens a stream pipeline. Sought as `name` `(` `)` in the same statement.
_STREAM_OPENERS = frozenset({"stream", "parallelStream"})

#: Keywords that can be followed by `(` without being a method. Without this,
#: `if (…) {` reads as a declaration of a method named `if`.
_CONTROL_KEYWORDS = frozenset(
    {
        "if",
        "for",
        "while",
        "switch",
        "catch",
        "synchronized",
        "return",
        "new",
        "throw",
        "else",
        "do",
        "try",
        "assert",
        "case",
        "instanceof",
        "super",
        "this",
        "break",
        "continue",
        "finally",
        "yield",
    }
)

#: Declaration modifiers, skipped when reading `<type> <name>` out of a statement.
_MODIFIERS = frozenset(
    {
        "public",
        "private",
        "protected",
        "static",
        "final",
        "abstract",
        "native",
        "transient",
        "volatile",
        "strictfp",
        "synchronized",
        "default",
        "sealed",
        "non",
    }
)

#: Type-declaration keywords, and the token that ends a statement.
_TYPE_KEYWORDS = frozenset({"class", "interface", "enum", "record"})
_STATEMENT_BOUNDARIES = frozenset({";", "{", "}"})

#: Multi-character operators that must tokenize as one unit. `>>` is deliberately
#: absent: it closes two generic parameters far more often than it shifts, and
#: splitting it keeps angle-bracket depth correct where that matters.
_OPERATORS = (
    "->",
    "::",
    "==",
    "!=",
    "<=",
    ">=",
    "&&",
    "||",
    "++",
    "--",
    "+=",
    "-=",
    "*=",
    "/=",
    "%=",
)

_IDENTIFIER = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
_NUMBER = re.compile(r"[0-9][A-Za-z0-9_.]*")


class _TokenKind(Enum):
    IDENTIFIER = "identifier"
    NUMBER = "number"
    OPERATOR = "operator"


@dataclass(frozen=True)
class _Token:
    """One lexical unit of the scrubbed view, with its offset into the original."""

    kind: _TokenKind
    text: str
    start: int
    end: int

    @property
    def is_name(self) -> bool:
        """Whether this token can stand as an identifier in a declaration."""
        return self.kind is _TokenKind.IDENTIFIER


def _tokenize(text: str) -> list[_Token]:
    """Split the scrubbed view into tokens, preserving original offsets.

    Runs over the masked view, so a blanked comment or literal is whitespace and
    contributes nothing. Offsets index the original file because masking preserves
    length — which is what lets a caller slice readable text out of the source
    after matching against the mask.
    """
    tokens: list[_Token] = []
    index = 0
    length = len(text)

    while index < length:
        character = text[index]
        if character.isspace():
            index += 1
            continue

        identifier = _IDENTIFIER.match(text, index)
        if identifier is not None:
            tokens.append(
                _Token(_TokenKind.IDENTIFIER, identifier.group(), index, identifier.end())
            )
            index = identifier.end()
            continue

        number = _NUMBER.match(text, index)
        if number is not None:
            tokens.append(_Token(_TokenKind.NUMBER, number.group(), index, number.end()))
            index = number.end()
            continue

        operator = next((op for op in _OPERATORS if text.startswith(op, index)), None)
        if operator is not None:
            tokens.append(_Token(_TokenKind.OPERATOR, operator, index, index + len(operator)))
            index += len(operator)
            continue

        tokens.append(_Token(_TokenKind.OPERATOR, character, index, index + 1))
        index += 1

    return tokens


def _brace_depths(tokens: Sequence[_Token]) -> list[int]:
    """Open-brace count before each token, for "is this at class-body level?".

    A member declaration sits exactly one brace deeper than its type's body
    opener; a statement inside a method body sits deeper still. Comparing depths
    is what separates a method declaration from a call that happens to look like
    one.
    """
    depths: list[int] = []
    depth = 0
    for token in tokens:
        if token.text == "}":
            depth -= 1
        depths.append(depth)
        if token.text == "{":
            depth += 1
    return depths


class _Scanner:
    """One Java file, tokenized and indexed. Every reader below works off this."""

    def __init__(self, path: str, content: str) -> None:
        self.path = path
        self.source = JavaSource.of(content)
        self.tokens = _tokenize(self.source.structure)
        self.depths = _brace_depths(self.tokens)
        self._starts = [token.start for token in self.tokens]

    def span(self, start: int, end: int) -> SourceSpan:
        """Build a span, resolving both line numbers once."""
        return SourceSpan(
            start=start,
            end=end,
            start_line=self.source.line_at(start),
            end_line=self.source.line_at(max(start, end - 1)),
        )

    def text_of(self, start: int, end: int) -> str:
        """The original source between two offsets, whitespace-collapsed."""
        return " ".join(self.source.text[start:end].split())

    def index_at(self, offset: int) -> int:
        """Index of the first token at or after ``offset``."""
        return bisect.bisect_left(self._starts, offset)

    def matching(self, open_offset: int) -> int | None:
        """The offset of the bracket closing the one at ``open_offset``."""
        return self.source.matching_bracket(open_offset)

    def next_index(self, index: int) -> int | None:
        """The next token index, or None at end of file."""
        return index + 1 if index + 1 < len(self.tokens) else None


def _skip_generics(tokens: Sequence[_Token], index: int) -> int:
    """Step past a ``<…>`` type-argument list starting at ``index``.

    Counts angle brackets rather than seeking the next ``>``, so a nested
    ``Map<String, List<Order>>`` is skipped whole.
    """
    if index >= len(tokens) or tokens[index].text != "<":
        return index
    depth = 0
    while index < len(tokens):
        text = tokens[index].text
        if text == "<":
            depth += 1
        elif text == ">":
            depth -= 1
            if depth == 0:
                return index + 1
        elif text in {";", "{", "}", ")"}:
            # Not a generic list after all — an unbalanced comparison. Leaving the
            # index where it started is the conservative answer.
            return index
        index += 1
    return index


def _type_arguments(tokens: Sequence[_Token], index: int) -> tuple[str, ...]:
    """Top-level type arguments of the reference starting at ``index``.

    ``JpaRepository<Order, Long>`` yields ``("Order", "Long")``. Only the outer
    level is read, so ``Map<String, List<Order>>`` yields ``("Map"'s two args)``
    rather than flattening the nesting — the caller wants the first argument, and
    a nested one is never it.
    """
    cursor = index
    while cursor + 1 < len(tokens) and tokens[cursor].text == "." and tokens[cursor + 1].is_name:
        cursor += 2
    cursor += 1
    if cursor >= len(tokens) or tokens[cursor].text != "<":
        return ()

    arguments: list[str] = []
    depth = 0
    pending: str | None = None
    for position in range(cursor, len(tokens)):
        text = tokens[position].text
        if text == "<":
            depth += 1
            continue
        if text == ">":
            depth -= 1
            if depth == 0:
                if pending is not None:
                    arguments.append(pending)
                return tuple(arguments)
            continue
        if depth == 1:
            if text == ",":
                if pending is not None:
                    arguments.append(pending)
                pending = None
            elif tokens[position].is_name and pending is None:
                pending = tokens[position].text
    return tuple(arguments)


def _read_type_name(tokens: Sequence[_Token], index: int) -> tuple[str | None, int]:
    """Read one type reference, returning its simple name and the next index.

    Handles qualification (``a.b.C`` yields ``C``), type arguments, and array
    suffixes. The simple name is what everything downstream compares on: an
    import list already tells us which package it came from.
    """
    if index >= len(tokens) or not tokens[index].is_name:
        return None, index

    name = tokens[index].text
    index += 1
    while index + 1 < len(tokens) and tokens[index].text == "." and tokens[index + 1].is_name:
        name = tokens[index + 1].text
        index += 2

    index = _skip_generics(tokens, index)
    while index + 1 < len(tokens) and tokens[index].text == "[" and tokens[index + 1].text == "]":
        index += 2
    return name, index


def _annotations_before(scanner: _Scanner, index: int) -> tuple[str, ...]:
    """Annotation names decorating the declaration whose token is ``index``.

    Walks backwards to the enclosing statement boundary. Argument lists are
    stepped over as units, so ``@Query(value = "…", nativeQuery = true)`` does not
    strand the walk inside its own parentheses.
    """
    names: list[str] = []
    cursor = index - 1
    tokens = scanner.tokens

    while cursor >= 0:
        text = tokens[cursor].text
        if text in _STATEMENT_BOUNDARIES:
            break
        if text == ")":
            depth = 0
            while cursor >= 0:
                if tokens[cursor].text == ")":
                    depth += 1
                elif tokens[cursor].text == "(":
                    depth -= 1
                    if depth == 0:
                        break
                cursor -= 1
            cursor -= 1
            continue
        if tokens[cursor].is_name and cursor > 0 and tokens[cursor - 1].text == "@":
            names.append(tokens[cursor].text)
            cursor -= 2
            continue
        cursor -= 1

    return tuple(reversed(names))


def _annotations_within(scanner: _Scanner, start: int, end: int) -> tuple[str, ...]:
    """Annotation names appearing between two token indices.

    The forward counterpart to :func:`_annotations_before`, for a caller that
    already holds the whole statement — a field declaration, whose annotations sit
    at its start rather than above some later token. Argument lists are stepped
    over so an annotation's own contents cannot be read as further annotations.
    """
    names: list[str] = []
    tokens = scanner.tokens
    cursor = start

    while cursor < end and cursor < len(tokens):
        if tokens[cursor].text != "@":
            cursor += 1
            continue
        cursor += 1
        if cursor < end and tokens[cursor].is_name:
            names.append(tokens[cursor].text)
            cursor += 1
        if cursor < end and tokens[cursor].text == "(":
            close = scanner.matching(tokens[cursor].start)
            cursor = end if close is None else scanner.index_at(close + 1)

    return tuple(names)


def _annotation_arguments(scanner: _Scanner, name: str, before: int) -> str:
    """The argument text of the named annotation nearest above ``before``.

    Used only to read ``fetch = FetchType.LAZY``, which decides whether a
    single-valued association is lazy. Returns an empty string when the
    annotation takes no arguments.
    """
    tokens = scanner.tokens
    cursor = before - 1
    while cursor >= 0:
        text = tokens[cursor].text
        if text in _STATEMENT_BOUNDARIES:
            break
        if (
            tokens[cursor].is_name
            and text == name
            and cursor > 0
            and tokens[cursor - 1].text == "@"
        ):
            following = cursor + 1
            if following < len(tokens) and tokens[following].text == "(":
                close = scanner.matching(tokens[following].start)
                if close is not None:
                    return scanner.text_of(tokens[following].start + 1, close)
            return ""
        cursor -= 1
    return ""


def _type_declarations(scanner: _Scanner) -> tuple[list[TypeDeclaration], list[int]]:
    """Every class/interface/enum/record declaration, with its body-opening index.

    The parallel index list is what member detection needs: a declaration belongs
    to a type body if it sits exactly one brace deeper than that body's opener.
    """
    declarations: list[TypeDeclaration] = []
    open_indices: list[int] = []
    tokens = scanner.tokens

    for index, token in enumerate(tokens):
        if not token.is_name or token.text not in _TYPE_KEYWORDS:
            continue
        # `Foo.class` is a class literal, and `record` is a contextual keyword that
        # is a perfectly legal identifier elsewhere.
        if index > 0 and tokens[index - 1].text == ".":
            continue
        if index + 1 >= len(tokens) or not tokens[index + 1].is_name:
            continue

        name = tokens[index + 1].text
        cursor = index + 2
        supertypes: list[str] = []
        supertype_arguments: tuple[str, ...] = ()
        body_index: int | None = None

        while cursor < len(tokens):
            text = tokens[cursor].text
            if text == "{":
                body_index = cursor
                break
            if text in {";", "}"}:
                break
            if text == "<":
                cursor = _skip_generics(tokens, cursor)
                continue
            if text == "(":
                close = scanner.matching(tokens[cursor].start)
                cursor = len(tokens) if close is None else scanner.index_at(close + 1)
                continue
            if tokens[cursor].is_name and text in {"extends", "implements", "permits"}:
                cursor += 1
                continue
            if tokens[cursor].is_name:
                arguments = _type_arguments(tokens, cursor)
                supertype, cursor = _read_type_name(tokens, cursor)
                if supertype is not None:
                    supertypes.append(supertype)
                    if arguments and not supertype_arguments:
                        supertype_arguments = arguments
                continue
            cursor += 1

        if body_index is None:
            continue
        close_offset = scanner.matching(tokens[body_index].start)
        if close_offset is None:
            continue

        declarations.append(
            TypeDeclaration(
                name=name,
                kind=token.text,
                line=scanner.source.line_at(token.start),
                span=scanner.span(tokens[body_index].start, close_offset + 1),
                supertypes=tuple(supertypes),
                supertype_arguments=supertype_arguments,
                annotations=_annotations_before(scanner, index),
            )
        )
        open_indices.append(body_index)

    return declarations, open_indices


def _enclosing_type(declarations: Sequence[TypeDeclaration], offset: int) -> TypeDeclaration | None:
    """The innermost type whose body contains ``offset``.

    Innermost, so a nested or anonymous class's members are attributed to it
    rather than to the outer class — which is what keeps a nested class's
    unrelated field from resolving an outer call site's receiver.
    """
    containing = [declaration for declaration in declarations if declaration.span.contains(offset)]
    if not containing:
        return None
    return min(containing, key=lambda declaration: declaration.span.length)


def _method_declarations(
    scanner: _Scanner,
    types: Sequence[TypeDeclaration],
    open_indices: Sequence[int],
) -> list[MethodDeclaration]:
    """Every method and constructor declared directly in a type body.

    A declaration is an identifier followed by ``(``, sitting at member depth,
    preceded by something that can end a return type, and followed — after the
    parameter list and any ``throws`` clause — by a body or a semicolon. The last
    condition is what distinguishes ``List<Order> findAll();`` from ``findAll();``.
    """
    methods: list[MethodDeclaration] = []
    tokens = scanner.tokens
    member_depths = {scanner.depths[index] + 1 for index in open_indices}

    for index, token in enumerate(tokens):
        if not token.is_name or token.text in _CONTROL_KEYWORDS or token.text in _TYPE_KEYWORDS:
            continue
        if index + 1 >= len(tokens) or tokens[index + 1].text != "(":
            continue
        if scanner.depths[index] not in member_depths:
            continue

        previous = tokens[index - 1] if index > 0 else None
        if previous is None:
            continue

        enclosing = _enclosing_type(types, token.start)
        # A constructor has no return type, so the token before it is whatever
        # ended the previous member — `;` or `}` for one written without an access
        # modifier. Matching the enclosing type's name is what identifies it.
        is_constructor = enclosing is not None and token.text == enclosing.name
        if not is_constructor:
            # A return type ends in an identifier, a generic close, or an array
            # close. Anything else — `=`, `;`, `.`, `return` — is an invocation.
            if not (previous.is_name or previous.text in {">", "]"}):
                continue
            if previous.is_name and previous.text in _CONTROL_KEYWORDS:
                continue

        close_offset = scanner.matching(tokens[index + 1].start)
        if close_offset is None:
            continue

        cursor = scanner.index_at(close_offset + 1)
        while cursor < len(tokens) and tokens[cursor].text not in {"{", ";", "}"}:
            # `throws IOException, SQLException` and annotation-typed receivers.
            cursor += 1
        if cursor >= len(tokens) or tokens[cursor].text == "}":
            continue

        body: SourceSpan | None = None
        if tokens[cursor].text == "{":
            body_close = scanner.matching(tokens[cursor].start)
            if body_close is None:
                continue
            body = scanner.span(tokens[cursor].start + 1, body_close)

        methods.append(
            MethodDeclaration(
                name=token.text,
                line=scanner.source.line_at(token.start),
                declaring_type=enclosing.name if enclosing is not None else None,
                parameters_span=scanner.span(tokens[index + 1].start, close_offset + 1),
                body=body,
                annotations=_annotations_before(scanner, index),
            )
        )

    return methods


def _split_arguments(scanner: _Scanner, start: int, end: int) -> list[tuple[int, int]]:
    """Split a parameter or argument list on top-level commas."""
    tokens = scanner.tokens
    result: list[tuple[int, int]] = []
    depth = 0
    begin = start
    for index in range(scanner.index_at(start), len(tokens)):
        token = tokens[index]
        if token.start >= end:
            break
        if token.text in {"(", "[", "{", "<"}:
            depth += 1
        elif token.text in {")", "]", "}", ">"}:
            depth -= 1
        elif token.text == "," and depth == 0:
            result.append((begin, token.start))
            begin = token.end
    result.append((begin, end))
    return [(a, b) for a, b in result if scanner.text_of(a, b)]


def _declared_variable(scanner: _Scanner, start: int, end: int) -> tuple[str, str] | None:
    """Read ``<Type> <name>`` out of a declaration fragment, or None.

    Shared by fields, parameters, and locals because the three are the same shape
    with different punctuation around them. Modifiers and annotations are skipped;
    anything that does not reduce to exactly a type followed by a name is rejected
    rather than guessed at.
    """
    tokens = scanner.tokens
    index = scanner.index_at(start)
    limit = scanner.index_at(end)

    while index < limit:
        token = tokens[index]
        if token.text == "@":
            index += 1
            if index < limit and tokens[index].is_name:
                index += 1
            if index < limit and tokens[index].text == "(":
                close = scanner.matching(tokens[index].start)
                index = limit if close is None else scanner.index_at(close + 1)
            continue
        if token.is_name and token.text in _MODIFIERS:
            index += 1
            continue
        break

    type_name, index = _read_type_name(tokens, index)
    if type_name is None or index >= limit:
        return None
    # A varargs parameter puts `...` between the type and the name.
    while index < limit and tokens[index].text == ".":
        index += 1
    if index >= limit or not tokens[index].is_name:
        return None

    variable = tokens[index].text
    following = index + 1
    if following < limit and tokens[following].text not in {"=", ",", ")"}:
        return None
    return type_name, variable


def _iteration_contexts(scanner: _Scanner) -> list[IterationContext]:
    """Every construct whose body runs once per element.

    Loops are read from their keyword; lambdas are read from ``->`` and admitted
    only when the call consuming them iterates (see
    :func:`_lambda_iteration_kind`). The span recorded is always the *body*, never
    the header — a repository call in ``for (X x : repo.findAll())`` runs once, and
    a detector that included the header would report it as repeated.
    """
    contexts: list[IterationContext] = []
    tokens = scanner.tokens
    consumed_while: set[int] = set()

    for index, token in enumerate(tokens):
        if token.is_name and token.text == "do":
            body = _body_span(scanner, index + 1)
            if body is None:
                continue
            contexts.append(IterationContext(kind=IterationKind.DO_WHILE, span=body))
            tail = scanner.index_at(body.end)
            while tail < len(tokens) and tokens[tail].text != "while":
                tail += 1
            if tail < len(tokens):
                consumed_while.add(tail)

    for index, token in enumerate(tokens):
        if not token.is_name:
            continue

        if token.text in {"for", "while"} and index not in consumed_while:
            if index + 1 >= len(tokens) or tokens[index + 1].text != "(":
                continue
            close = scanner.matching(tokens[index + 1].start)
            if close is None:
                continue
            body = _body_span(scanner, scanner.index_at(close + 1))
            if body is None:
                continue
            if token.text == "while":
                contexts.append(IterationContext(kind=IterationKind.WHILE, span=body))
                continue
            contexts.append(_for_context(scanner, tokens[index + 1].start + 1, close, body))

    contexts.extend(_lambda_contexts(scanner))
    return contexts


def _body_span(scanner: _Scanner, index: int) -> SourceSpan | None:
    """The repeated body starting at token ``index`` — a block or one statement."""
    tokens = scanner.tokens
    if index >= len(tokens):
        return None
    if tokens[index].text == "{":
        close = scanner.matching(tokens[index].start)
        if close is None:
            return None
        return scanner.span(tokens[index].start + 1, close)

    depth = 0
    for cursor in range(index, len(tokens)):
        text = tokens[cursor].text
        if text in {"(", "[", "{"}:
            depth += 1
        elif text in {")", "]", "}"}:
            depth -= 1
        elif text == ";" and depth == 0:
            return scanner.span(tokens[index].start, tokens[cursor].end)
    return None


def _for_context(
    scanner: _Scanner, header_start: int, header_end: int, body: SourceSpan
) -> IterationContext:
    """Classify a ``for`` header as enhanced or classic, binding its element."""
    tokens = scanner.tokens
    depth = 0
    for index in range(scanner.index_at(header_start), len(tokens)):
        token = tokens[index]
        if token.start >= header_end:
            break
        if token.text in {"(", "[", "<"}:
            depth += 1
        elif token.text in {")", "]", ">"}:
            depth -= 1
        elif token.text == ":" and depth == 0:
            declared = _declared_variable(scanner, header_start, token.start)
            return IterationContext(
                kind=IterationKind.ENHANCED_FOR,
                span=body,
                element_identifier=declared[1] if declared is not None else None,
                element_type=declared[0] if declared is not None else None,
                iterable_text=scanner.text_of(token.end, header_end) or None,
            )

    return IterationContext(kind=IterationKind.FOR, span=body)


def _lambda_contexts(scanner: _Scanner) -> list[IterationContext]:
    """Lambda bodies that a per-element operation will run once per element."""
    contexts: list[IterationContext] = []
    tokens = scanner.tokens

    for index, token in enumerate(tokens):
        if token.text != "->":
            continue

        parameter, lambda_start = _lambda_parameter(scanner, index)
        kind = _lambda_iteration_kind(scanner, lambda_start)
        if kind is None:
            continue
        body = _lambda_body(scanner, index + 1)
        if body is None:
            continue
        contexts.append(IterationContext(kind=kind, span=body, element_identifier=parameter))

    return contexts


def _lambda_parameter(scanner: _Scanner, arrow: int) -> tuple[str | None, int]:
    """The lambda's single parameter name, and the index its parameter list opens at.

    Only a single parameter is bound. A two-parameter lambda is a ``BiConsumer``
    over a map's entries, where naming one of them as "the element" would be
    arbitrary; the context is still recorded, just without an element to trace
    arguments back to.
    """
    tokens = scanner.tokens
    previous = arrow - 1
    if previous < 0:
        return None, arrow

    if tokens[previous].text == ")":
        depth = 0
        cursor = previous
        while cursor >= 0:
            if tokens[cursor].text == ")":
                depth += 1
            elif tokens[cursor].text == "(":
                depth -= 1
                if depth == 0:
                    break
            cursor -= 1
        if cursor < 0:
            return None, arrow
        parts = _split_arguments(scanner, tokens[cursor].start + 1, tokens[previous].start)
        if len(parts) != 1:
            return None, cursor
        declared = _declared_variable(scanner, parts[0][0], parts[0][1])
        if declared is not None:
            return declared[1], cursor
        single = scanner.text_of(parts[0][0], parts[0][1])
        return (single or None), cursor

    if tokens[previous].is_name:
        return tokens[previous].text, previous
    return None, arrow


def _lambda_iteration_kind(scanner: _Scanner, lambda_start: int) -> IterationKind | None:
    """Whether the call consuming this lambda runs it once per element.

    ``forEach`` and friends iterate whatever they are called on and do not exist
    on ``Optional``, so they need no type resolution. Everything else — ``map``,
    ``filter`` — is admitted only with a visible stream opener in the same
    statement, because ``Optional.map`` runs at most once and reporting it as a
    loop would be a confident false positive.

    Returns None for a lambda that is a callback rather than an iteration:
    ``ifPresent``, ``thenApply``, ``execute``, and anything else unrecognized.
    """
    tokens = scanner.tokens
    depth = 0
    cursor = lambda_start - 1

    while cursor >= 0:
        text = tokens[cursor].text
        if text in {")", "]", "}"}:
            depth += 1
        elif text in {"(", "[", "{"}:
            if depth == 0:
                break
            depth -= 1
        cursor -= 1

    if cursor < 0 or tokens[cursor].text != "(":
        return None
    name_index = cursor - 1
    if name_index < 0 or not tokens[name_index].is_name:
        return None

    method = tokens[name_index].text
    if method in _PER_ELEMENT_ALWAYS:
        return IterationKind.LAMBDA_FOR_EACH
    if method in _PER_ELEMENT_WITH_STREAM and _has_stream_opener(scanner, name_index):
        return IterationKind.LAMBDA_STREAM
    return None


def _has_stream_opener(scanner: _Scanner, before: int) -> bool:
    """Whether a ``stream()``/``parallelStream()`` call precedes ``before``.

    Bounded to the current statement, so an unrelated pipeline earlier in the
    method cannot vouch for this one.
    """
    tokens = scanner.tokens
    cursor = before - 1
    while cursor >= 0:
        text = tokens[cursor].text
        if text in _STATEMENT_BOUNDARIES:
            return False
        if (
            tokens[cursor].is_name
            and text in _STREAM_OPENERS
            and cursor + 2 < len(tokens)
            and tokens[cursor + 1].text == "("
            and tokens[cursor + 2].text == ")"
        ):
            return True
        cursor -= 1
    return False


def _lambda_body(scanner: _Scanner, index: int) -> SourceSpan | None:
    """The lambda's body — a block, or an expression up to its argument's end."""
    tokens = scanner.tokens
    if index >= len(tokens):
        return None
    if tokens[index].text == "{":
        close = scanner.matching(tokens[index].start)
        if close is None:
            return None
        return scanner.span(tokens[index].start + 1, close)

    depth = 0
    for cursor in range(index, len(tokens)):
        text = tokens[cursor].text
        if text in {"(", "[", "{"}:
            depth += 1
        elif text in {")", "]", "}"}:
            if depth == 0:
                return scanner.span(tokens[index].start, tokens[cursor].start)
            depth -= 1
        elif text in {",", ";"} and depth == 0:
            return scanner.span(tokens[index].start, tokens[cursor].start)
    return None


def _package_and_imports(scanner: _Scanner) -> tuple[str | None, tuple[str, ...]]:
    """The file's package declaration and its single-type imports."""
    package: str | None = None
    imports: list[str] = []
    tokens = scanner.tokens

    for index, token in enumerate(tokens):
        if not token.is_name or token.text not in {"package", "import"}:
            continue
        if index > 0 and tokens[index - 1].text == ".":
            continue
        parts: list[str] = []
        cursor = index + 1
        while cursor < len(tokens) and tokens[cursor].text != ";":
            if tokens[cursor].is_name and tokens[cursor].text != "static":
                parts.append(tokens[cursor].text)
            elif tokens[cursor].text == "*":
                parts.append("*")
            cursor += 1
        if not parts:
            continue
        if token.text == "package":
            package = ".".join(parts)
        else:
            imports.append(".".join(parts))

    return package, tuple(imports)


def _is_repository_type(declaration: TypeDeclaration, known: Mapping[str, object]) -> bool:
    """Whether a declared type is a Spring Data repository.

    Established by what it extends, transitively through repository interfaces we
    have already seen, or by an explicit ``@Repository``. Never by its name — that
    is the fallback in :func:`_resolve_repository_type`, and it produces a weaker
    resolution precisely because it is a convention rather than a fact.
    """
    if declaration.kind != "interface":
        return False
    if any(supertype in _SPRING_DATA_BASE_TYPES for supertype in declaration.supertypes):
        return True
    if any(supertype in known for supertype in declaration.supertypes):
        return True
    return "Repository" in declaration.annotations


def _repository_declarations(
    scanner: _Scanner,
    types: Sequence[TypeDeclaration],
    methods: Sequence[MethodDeclaration],
    known: Mapping[str, RepositoryDeclaration],
    queries_by_line: Mapping[int, ExtractedQuery],
) -> list[RepositoryDeclaration]:
    """Repository interfaces in this file, with the queries their methods declare.

    Method-to-query attribution is by position: a repository method's query is the
    extracted query whose line falls between the previous member and this method's
    own line. That is how a ``@Query`` annotation — which extraction anchors to the
    annotation's string literal, several lines above the method it decorates —
    reaches the method a call site names.
    """
    declarations: list[RepositoryDeclaration] = []
    resolved: dict[str, RepositoryDeclaration] = dict(known)

    # Repeated until stable so a custom base interface declared *below* its
    # subinterface in the same file still resolves it.
    pending = [declaration for declaration in types if declaration.kind == "interface"]
    accepted: list[TypeDeclaration] = []
    changed = True
    while changed:
        changed = False
        for declaration in list(pending):
            if _is_repository_type(declaration, resolved):
                accepted.append(declaration)
                pending.remove(declaration)
                resolved[declaration.name] = RepositoryDeclaration(
                    type_name=declaration.name,
                    file=scanner.path,
                    line=declaration.line,
                    resolution=RepositoryResolution.DECLARED,
                )
                changed = True

    for declaration in accepted:
        members = [
            method
            for method in methods
            if declaration.span.contains(method.parameters_span.start)
            and method.declaring_type == declaration.name
        ]
        members.sort(key=lambda method: method.line)

        repository_methods: list[RepositoryMethodDeclaration] = []
        previous_line = declaration.line
        for method in members:
            query = next(
                (
                    candidate
                    for line, candidate in sorted(queries_by_line.items())
                    if previous_line < line <= method.line
                ),
                None,
            )
            repository_methods.append(
                RepositoryMethodDeclaration(
                    name=method.name,
                    line=method.line,
                    repository_type=declaration.name,
                    file=scanner.path,
                    query_id=query.id if query is not None else None,
                    annotations=method.annotations,
                )
            )
            previous_line = method.line

        declarations.append(
            RepositoryDeclaration(
                type_name=declaration.name,
                file=scanner.path,
                line=declaration.line,
                resolution=RepositoryResolution.DECLARED,
                supertypes=declaration.supertypes,
                entity_type=(
                    declaration.supertype_arguments[0] if declaration.supertype_arguments else None
                ),
                methods=tuple(repository_methods),
            )
        )

    return declarations


def _mapped_tables(scanner: _Scanner, types: Sequence[TypeDeclaration]) -> dict[str, str]:
    """Entity-to-table names, from ``@Table(name = "…")``.

    The only place a real table name enters the pipeline. Everywhere else a table
    is *derived* from a class name, which is a convention that fails exactly where
    it matters most — an entity called ``Order`` almost always maps to ``orders``,
    because ``order`` is a reserved word. Read from the original source rather
    than the scrubbed view, since the name lives inside a string literal.
    """
    tables: dict[str, str] = {}
    for declaration in types:
        if "Entity" not in declaration.annotations:
            continue
        index = scanner.index_at(declaration.span.start)
        arguments = _annotation_arguments(scanner, "Table", index)
        match = re.search(r"""name\s*=\s*["']([^"']+)["']""", arguments)
        if match is not None:
            tables[declaration.name] = match.group(1)
    return tables


def _entity_relationships(
    scanner: _Scanner,
    types: Sequence[TypeDeclaration],
    open_indices: Sequence[int],
) -> list[EntityRelationship]:
    """Lazily-fetched associations on ``@Entity`` types in this file."""
    relationships: list[EntityRelationship] = []
    tokens = scanner.tokens

    for declaration, open_index in zip(types, open_indices, strict=True):
        if "Entity" not in declaration.annotations:
            continue
        member_depth = scanner.depths[open_index] + 1

        start = open_index + 1
        end = scanner.index_at(declaration.span.end)
        statement_start = start
        for index in range(start, min(end, len(tokens))):
            token = tokens[index]
            if token.text == "{":
                close = scanner.matching(token.start)
                if close is not None:
                    statement_start = scanner.index_at(close + 1)
                continue
            if token.text != ";" or scanner.depths[index] != member_depth:
                continue
            annotations = _annotations_within(scanner, statement_start, index)
            association = next((name for name in annotations if name in _ASSOCIATIONS), None)
            if association is not None:
                declared = _declared_variable(scanner, tokens[statement_start].start, token.start)
                if declared is not None and _is_lazy(scanner, association, index):
                    field_name = declared[1]
                    relationships.append(
                        EntityRelationship(
                            entity_type=declaration.name,
                            field_name=field_name,
                            accessor_name=f"get{field_name[:1].upper()}{field_name[1:]}",
                            association=association,
                            file=scanner.path,
                            line=scanner.source.line_at(tokens[statement_start].start),
                        )
                    )
            statement_start = index + 1

    return relationships


def _is_lazy(scanner: _Scanner, association: str, index: int) -> bool:
    """Whether an association is lazily fetched, by JPA's defaults and overrides."""
    arguments = _annotation_arguments(scanner, association, index)
    if "EAGER" in arguments:
        return False
    if association in _LAZY_BY_DEFAULT:
        return True
    return "LAZY" in arguments


def _field_handles(
    scanner: _Scanner,
    types: Sequence[TypeDeclaration],
    open_indices: Sequence[int],
    is_repository: Callable[[str], bool],
) -> list[RepositoryHandle]:
    """Fields whose declared type is a repository, however they were injected.

    Constructor injection needs no separate case: the constructor assigns to a
    field, and it is the *field* every call site reads. Parameters are handled by
    :func:`_parameter_handles` so a repository used directly inside the
    constructor still resolves.
    """
    handles: list[RepositoryHandle] = []
    tokens = scanner.tokens

    for declaration, open_index in zip(types, open_indices, strict=True):
        member_depth = scanner.depths[open_index] + 1
        # Past the body's own opening brace: starting *on* it would match the
        # "member with a body" branch below and skip the whole type.
        start = open_index + 1
        end = scanner.index_at(declaration.span.end)
        statement_start = start

        for index in range(start, min(end, len(tokens))):
            token = tokens[index]
            if token.text == "{":
                # Skip a member with a body — its interior is not field territory.
                close = scanner.matching(token.start)
                if close is not None:
                    statement_start = scanner.index_at(close + 1)
                continue
            if token.text != ";" or scanner.depths[index] != member_depth:
                continue
            if statement_start < index:
                declared = _declared_variable(scanner, tokens[statement_start].start, token.start)
                if declared is not None and is_repository(declared[0]):
                    handles.append(
                        RepositoryHandle(
                            identifier=declared[1],
                            repository_type=declared[0],
                            injection=InjectionKind.FIELD,
                            line=scanner.source.line_at(tokens[statement_start].start),
                            scope=declaration.span,
                        )
                    )
            statement_start = index + 1

    return handles


def _parameter_handles(
    scanner: _Scanner,
    methods: Sequence[MethodDeclaration],
    is_repository: Callable[[str], bool],
) -> list[RepositoryHandle]:
    """Method and constructor parameters that carry a repository."""
    handles: list[RepositoryHandle] = []

    for method in methods:
        if method.body is None:
            continue
        parts = _split_arguments(
            scanner, method.parameters_span.start + 1, method.parameters_span.end - 1
        )
        for begin, finish in parts:
            declared = _declared_variable(scanner, begin, finish)
            if declared is None or not is_repository(declared[0]):
                continue
            handles.append(
                RepositoryHandle(
                    identifier=declared[1],
                    repository_type=declared[0],
                    injection=(
                        InjectionKind.CONSTRUCTOR_PARAMETER
                        if method.name == method.declaring_type
                        else InjectionKind.METHOD_PARAMETER
                    ),
                    line=method.line,
                    scope=method.body,
                )
            )

    return handles


def _local_handles(
    scanner: _Scanner,
    methods: Sequence[MethodDeclaration],
    is_repository: Callable[[str], bool],
    named: Mapping[str, str],
) -> list[RepositoryHandle]:
    """Locals holding a repository, by declared type or by direct assignment.

    Two shapes only, and the narrowness is the point. ``OrderRepository local =
    anything;`` is trusted because the *declared type* says what it holds. ``var
    local = orderRepository;`` is trusted because the right-hand side is exactly
    one identifier that is already a known handle. Anything else — a factory call,
    a ternary, a field of another object — is not followed, because guessing there
    produces call sites attributed to a repository that was never involved.
    """
    handles: list[RepositoryHandle] = []
    tokens = scanner.tokens

    for method in methods:
        if method.body is None:
            continue
        start = scanner.index_at(method.body.start)
        end = scanner.index_at(method.body.end)
        statement_start = start

        for index in range(start, min(end, len(tokens))):
            token = tokens[index]
            if token.text != ";":
                continue
            assignment = next(
                (cursor for cursor in range(statement_start, index) if tokens[cursor].text == "="),
                None,
            )
            if assignment is not None and statement_start < assignment:
                declared = _declared_variable(
                    scanner, tokens[statement_start].start, tokens[assignment].start
                )
                if declared is not None:
                    type_name, variable = declared
                    resolved: str | None = None
                    if is_repository(type_name):
                        resolved = type_name
                    elif type_name == "var":
                        right = scanner.text_of(tokens[assignment].end, token.start)
                        resolved = named.get(right.removeprefix("this."))
                    if resolved is not None:
                        handles.append(
                            RepositoryHandle(
                                identifier=variable,
                                repository_type=resolved,
                                injection=InjectionKind.LOCAL_VARIABLE,
                                line=scanner.source.line_at(tokens[statement_start].start),
                                scope=method.body,
                            )
                        )
            statement_start = index + 1

    return handles


def _enclosing_method(
    methods: Sequence[MethodDeclaration], offset: int
) -> MethodDeclaration | None:
    """The innermost method whose body contains ``offset``."""
    containing = [
        method for method in methods if method.body is not None and method.body.contains(offset)
    ]
    if not containing:
        return None
    return min(containing, key=lambda method: method.body.length if method.body else 0)


def _iterations_around(
    contexts: Sequence[IterationContext], offset: int
) -> tuple[IterationContext, ...]:
    """Every repeating construct containing ``offset``, outermost first."""
    containing = [context for context in contexts if context.span.contains(offset)]
    containing.sort(key=lambda context: context.span.length, reverse=True)
    return tuple(containing)


def _resolve_handle(
    handles: Sequence[RepositoryHandle], identifier: str, offset: int
) -> RepositoryHandle | None:
    """The narrowest in-scope handle for ``identifier`` at ``offset``.

    Narrowest wins so a local or parameter shadows a field of the same name, which
    is what Java itself does.
    """
    candidates = [
        handle
        for handle in handles
        if handle.identifier == identifier
        and (handle.scope is None or handle.scope.contains(offset))
    ]
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda handle: handle.scope.length if handle.scope is not None else 1 << 62,
    )


def _call_sites(
    scanner: _Scanner,
    types: Sequence[TypeDeclaration],
    methods: Sequence[MethodDeclaration],
    handles: Sequence[RepositoryHandle],
    contexts: Sequence[IterationContext],
    resolution: Callable[[str], RepositoryResolution],
) -> list[RepositoryCallSite]:
    """Repository invocations, each with the constructs that repeat it.

    Only ``receiver.method(...)`` is read, where ``receiver`` resolves to a handle.
    ``someObject.findByCustomerId()`` on a type that is not a repository produces
    nothing, which is the false positive this shape exists to avoid. A method
    reference produces nothing either — it has no argument list here, and whether
    it runs per element depends on what consumes it.
    """
    sites: list[RepositoryCallSite] = []
    tokens = scanner.tokens

    for index, token in enumerate(tokens):
        if not token.is_name or token.text in _CONTROL_KEYWORDS:
            continue
        if index + 1 >= len(tokens) or tokens[index + 1].text != "(":
            continue
        if index < 2 or tokens[index - 1].text != ".":
            continue

        receiver_index = index - 2
        receiver = tokens[receiver_index]
        if not receiver.is_name:
            continue

        receiver_text = receiver.text
        if (
            receiver_index >= 2
            and tokens[receiver_index - 1].text == "."
            and tokens[receiver_index - 2].text == "this"
        ):
            receiver_text = f"this.{receiver.text}"
        elif receiver.text == "this":
            continue

        handle = _resolve_handle(handles, receiver.text, token.start)
        if handle is None:
            continue

        close = scanner.matching(tokens[index + 1].start)
        if close is None:
            continue

        arguments = scanner.text_of(tokens[index + 1].start + 1, close)
        iteration = _iterations_around(contexts, token.start)
        dependency, detail = _argument_dependency(
            scanner, tokens[index + 1].start + 1, close, iteration, arguments
        )
        enclosing_method = _enclosing_method(methods, token.start)
        enclosing_type = _enclosing_type(types, token.start)

        sites.append(
            RepositoryCallSite(
                file=scanner.path,
                line=scanner.source.line_at(token.start),
                span=scanner.span(receiver.start, close + 1),
                receiver_text=receiver_text,
                receiver_identifier=receiver.text,
                repository_type=handle.repository_type,
                resolution=resolution(handle.repository_type),
                injection=handle.injection,
                method_name=token.text,
                arguments_text=arguments,
                enclosing_method=(enclosing_method.name if enclosing_method is not None else None),
                enclosing_type=enclosing_type.name if enclosing_type is not None else None,
                iteration=iteration,
                dependency=dependency,
                dependency_detail=detail,
            )
        )

    return sites


def _argument_dependency(
    scanner: _Scanner,
    start: int,
    end: int,
    iteration: Sequence[IterationContext],
    arguments: str,
) -> tuple[ArgumentDependency, str | None]:
    """Whether the call's arguments are derived from an enclosing element.

    Matching is on identifier tokens inside the argument list, so a substring of a
    longer name cannot pass for the element. This is what separates
    ``findByCustomerId(customer.getId())`` — the classic parent-key N+1 — from
    ``findAll()`` in a loop, which is repeated work of a different kind.
    """
    if not arguments:
        return ArgumentDependency.NO_ARGUMENTS, None

    tokens = scanner.tokens
    present = {
        tokens[index].text
        for index in range(scanner.index_at(start), len(tokens))
        if tokens[index].start < end and tokens[index].is_name
    }

    for context in reversed(iteration):
        element = context.element_identifier
        if element is not None and element in present:
            return (
                ArgumentDependency.LOOP_ELEMENT_ARGUMENT,
                f"`{element}` flows into the argument list `({arguments})`",
            )

    return ArgumentDependency.INDEPENDENT, None


def _lazy_accesses(
    scanner: _Scanner,
    types: Sequence[TypeDeclaration],
    methods: Sequence[MethodDeclaration],
    contexts: Sequence[IterationContext],
    entities: Mapping[str, tuple[EntityRelationship, ...]],
) -> list[LazyAssociationAccess]:
    """Lazy associations dereferenced inside a repeating construct.

    Requires the receiver to be an element whose *declared* type is a known entity
    — which in practice means an enhanced ``for``, the only construct that states
    its element's type. A lambda parameter's type is inferred, and guessing it
    would mean reporting a getter on an unknown type as a database access.
    """
    accesses: list[LazyAssociationAccess] = []
    tokens = scanner.tokens

    element_types = {
        context.element_identifier: context.element_type
        for context in contexts
        if context.element_identifier is not None and context.element_type is not None
    }
    if not element_types:
        return accesses

    for index, token in enumerate(tokens):
        if not token.is_name or index + 1 >= len(tokens) or tokens[index + 1].text != "(":
            continue
        if index < 2 or tokens[index - 1].text != ".":
            continue
        receiver = tokens[index - 2]
        if not receiver.is_name:
            continue

        entity_type = element_types.get(receiver.text)
        if entity_type is None:
            continue
        relationship = next(
            (
                candidate
                for candidate in entities.get(entity_type, ())
                if candidate.accessor_name == token.text
            ),
            None,
        )
        if relationship is None:
            continue

        iteration = _iterations_around(contexts, token.start)
        if not iteration:
            continue
        close = scanner.matching(tokens[index + 1].start)
        if close is None:
            continue

        enclosing_method = _enclosing_method(methods, token.start)
        enclosing_type = _enclosing_type(types, token.start)
        accesses.append(
            LazyAssociationAccess(
                file=scanner.path,
                line=scanner.source.line_at(token.start),
                span=scanner.span(receiver.start, close + 1),
                receiver_identifier=receiver.text,
                entity_type=entity_type,
                accessor_name=token.text,
                relationship=relationship,
                enclosing_method=(enclosing_method.name if enclosing_method is not None else None),
                enclosing_type=enclosing_type.name if enclosing_type is not None else None,
                iteration=iteration,
            )
        )

    return accesses


def analyze_java_file(
    path: str,
    content: str,
    *,
    repositories: Mapping[str, RepositoryDeclaration] | None = None,
    entities: Mapping[str, tuple[EntityRelationship, ...]] | None = None,
    queries: Sequence[ExtractedQuery] = (),
) -> JavaFileStructure:
    """Read one Java file into structural facts.

    ``repositories`` and ``entities`` carry what other files established, which is
    what makes a call site in a service resolvable against an interface declared
    somewhere else. Passing neither is valid and simply narrows what can be
    resolved — a same-file repository still works.
    """
    known_repositories = dict(repositories or {})
    known_entities = dict(entities or {})

    scanner = _Scanner(path, content)
    package, imports = _package_and_imports(scanner)
    types, open_indices = _type_declarations(scanner)
    methods = _method_declarations(scanner, types, open_indices)

    queries_by_line = {
        query.provenance.line: query
        for query in queries
        if query.provenance.file == path and query.provenance.line is not None
    }
    declared = _repository_declarations(
        scanner, types, methods, known_repositories, queries_by_line
    )
    for declaration in declared:
        known_repositories[declaration.type_name] = declaration

    tables = _mapped_tables(scanner, types)
    relationships = _entity_relationships(scanner, types, open_indices)
    for relationship in relationships:
        known_entities[relationship.entity_type] = (
            *known_entities.get(relationship.entity_type, ()),
            relationship,
        )

    def is_repository(type_name: str) -> bool:
        return type_name in known_repositories or type_name.endswith(_REPOSITORY_NAME_SUFFIX)

    def resolution(type_name: str) -> RepositoryResolution:
        declaration = known_repositories.get(type_name)
        if declaration is not None:
            return declaration.resolution
        return RepositoryResolution.NAME_CONVENTION

    handles = [
        *_field_handles(scanner, types, open_indices, is_repository),
        *_parameter_handles(scanner, methods, is_repository),
    ]
    named = {handle.identifier: handle.repository_type for handle in handles}
    handles.extend(_local_handles(scanner, methods, is_repository, named))

    contexts = _iteration_contexts(scanner)
    call_sites = _call_sites(scanner, types, methods, handles, contexts, resolution)
    lazy = _lazy_accesses(scanner, types, methods, contexts, known_entities)

    return JavaFileStructure(
        path=path,
        package=package,
        imports=imports,
        types=tuple(types),
        methods=tuple(methods),
        repositories=tuple(declared),
        handles=tuple(handles),
        call_sites=tuple(call_sites),
        iterations=tuple(contexts),
        relationships=tuple(relationships),
        lazy_accesses=tuple(lazy),
        tables=tables,
    )


def derive_import_path(structure: JavaFileStructure, type_name: str) -> str | None:
    """The repository-relative path of ``type_name``, from this file's imports.

    Works by anchoring on the file's own coordinates: a file at
    ``<root>/com/acme/OrderService.java`` declaring ``package com.acme`` fixes
    ``<root>``, and an import of ``com.acme.data.OrderRepository`` then resolves
    to ``<root>/com/acme/data/OrderRepository.java``. No layout is assumed —
    ``src/main/java`` is derived, not hardcoded — so a Gradle module, a Maven
    module, or a flat repository all work.

    Returns None when the type is not imported by name (a wildcard import, or a
    same-package type), because fetching a guessed path is a request that will
    404 and a latency cost with no upside.
    """
    if structure.package is None:
        return None
    suffix = structure.package.replace(".", "/")
    directory, separator, _name = structure.path.rpartition("/")
    if not separator or not directory.endswith(suffix):
        return None
    root = directory[: len(directory) - len(suffix)]

    for imported in structure.imports:
        if imported.rsplit(".", 1)[-1] != type_name:
            continue
        return f"{root}{imported.replace('.', '/')}.java"

    same_package = f"{root}{suffix}/{type_name}.java"
    return same_package if same_package != structure.path else None


def _java_sources(sources: Iterable[SourceFile]) -> list[SourceFile]:
    """The Java members of a mixed source list, in the order they arrived."""
    return [source for source in sources if source.path.endswith(JAVA_EXTENSION)]


def analyze_java_program(
    sources: Sequence[SourceFile],
    *,
    queries: Sequence[ExtractedQuery] = (),
    resolve_source: ResolveJavaSource | None = None,
) -> JavaProgram:
    """Analyze every Java source in a run, resolving across files.

    Three passes, and each exists because the one before it cannot know enough:

    1. **Declarations.** Repository interfaces and entities, from every file. A
       service's call site cannot be resolved before the interface it calls has
       been read, and a pull request lists its files in no useful order.
    2. **Targeted retrieval.** For a receiver whose type is still unknown, the
       declaring file is fetched by import path when ``resolve_source`` allows it.
       Only types actually used as receivers are fetched, never the repository —
       a pull request touching two services must not pull a thousand files.
    3. **Resolution.** Call sites, iteration contexts, and lazy accesses, now
       against everything the first two passes established.

    Failing to resolve is never fatal: an unresolved type keeps its call sites,
    marked ``NAME_CONVENTION``, and is named in ``unresolved_types``.
    """
    java = _java_sources(sources)
    contents = {source.path: source.content for source in java}

    repositories: dict[str, RepositoryDeclaration] = {}
    entities: dict[str, tuple[EntityRelationship, ...]] = {}
    tables: dict[str, str] = {}

    def absorb(structure: JavaFileStructure) -> None:
        for declaration in structure.repositories:
            repositories[declaration.type_name] = declaration
        for relationship in structure.relationships:
            entities[relationship.entity_type] = (
                *entities.get(relationship.entity_type, ()),
                relationship,
            )
        tables.update(structure.tables)

    first_pass: dict[str, JavaFileStructure] = {}
    for source in java:
        structure = analyze_java_file(source.path, source.content, queries=queries)
        first_pass[source.path] = structure
        absorb(structure)

    unresolved: set[str] = set()
    if resolve_source is not None:
        wanted: dict[str, str] = {}
        for structure in first_pass.values():
            for site in structure.call_sites:
                if site.repository_type in repositories:
                    continue
                path = derive_import_path(structure, site.repository_type)
                if path is not None and path not in contents:
                    wanted.setdefault(site.repository_type, path)

        for type_name, path in wanted.items():
            fetched = resolve_source(path)
            if fetched is None:
                unresolved.add(type_name)
                continue
            contents[path] = fetched
            absorb(analyze_java_file(path, fetched, queries=queries))

    files: dict[str, JavaFileStructure] = {}
    for path, content in contents.items():
        files[path] = analyze_java_file(
            path,
            content,
            repositories=repositories,
            entities=entities,
            queries=queries,
        )

    for structure in files.values():
        for site in structure.call_sites:
            if site.repository_type not in repositories:
                unresolved.add(site.repository_type)

    return JavaProgram(
        files=files,
        repositories=repositories,
        entities=entities,
        tables=tables,
        unresolved_types=tuple(sorted(unresolved)),
    )
