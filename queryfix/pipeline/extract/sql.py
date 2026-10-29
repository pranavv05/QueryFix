"""Stage 2 (SQL) — sqlglot-based extraction and normalization.

Handles ``.sql`` files, migrations, and SQL string literals. Queries that cannot
be parsed come back with ``parse_error`` set rather than guessed at — never regex
SQL.
"""

from __future__ import annotations

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from queryfix.models.query import ExtractedQuery, Provenance, QueryKind, SourceFile, SqlSource
from queryfix.pipeline.extract.base import DEFAULT_DIALECT, query_id

__all__ = ["SqlExtractor", "extract_from_sql"]


class SqlExtractor:
    """The registered :class:`~queryfix.pipeline.extract.base.Extractor` for SQL.

    Its whole job is to read the one option SQL extraction takes — the dialect —
    off the source model before delegating. The ``isinstance`` is deliberate and
    belongs here rather than on the base model: a dialect is meaningless to a
    Java file, and putting it on
    :class:`~queryfix.models.query.SourceFile` would make every language's
    input model carry SQL's concerns. One narrowing check, in the one component
    that understands what a dialect is.
    """

    def extract(self, source: SourceFile) -> list[ExtractedQuery]:
        dialect = source.dialect if isinstance(source, SqlSource) else DEFAULT_DIALECT
        return extract_from_sql(source.path, source.content, dialect=dialect)


def extract_from_sql(
    path: str,
    content: str,
    dialect: str = DEFAULT_DIALECT,
    kind: QueryKind = QueryKind.RAW_SQL,
) -> list[ExtractedQuery]:
    """Extract statements from a ``.sql`` file or migration using sqlglot.

    Statement splitting is done by :func:`sqlglot.parse`, not by splitting on
    semicolons: a semicolon inside a string literal or a dollar-quoted function body
    is not a statement boundary, and only a parser knows the difference.

    Each statement's ``line`` is resolved from the sqlglot token position where
    available, so findings anchor to the statement rather than to the top of the
    file. A statement that fails to parse is still returned, with ``parse_error``
    set — the extract stage reports candidates, and the rule engine decides what is
    analyzable.
    """
    # A UTF-8 BOM is not SQL, but it is not whitespace either, so it reaches the
    # tokenizer and makes the *entire* file unparseable — one stray byte turning a
    # perfectly good migration into "could not analyze". Editors on Windows write it
    # by default, so this is ordinary input rather than a corrupt file. Stripped
    # before the empty check so a BOM-only file is empty, not unanalyzable.
    content = content.removeprefix("﻿")

    if not content.strip():
        return []

    try:
        statements = sqlglot.parse(content, read=dialect)
    except SqlglotError as error:
        # The whole file is unparseable, so there are no statement boundaries to
        # report against. Surface it as one unanalyzable candidate rather than
        # dropping the file silently.
        #
        # `SqlglotError` rather than `ParseError` because the two ways this fails are
        # siblings, not parent and child: an unterminated string literal never reaches
        # the parser and raises `TokenError` instead. Both mean the same thing here —
        # this text is not analyzable — so both produce the same candidate.
        return [
            ExtractedQuery(
                id=query_id(path, 1),
                kind=kind,
                text=content.strip(),
                dialect=dialect,
                provenance=Provenance(file=path, line=1),
                parse_error=str(error),
            )
        ]

    spans = _statement_spans(content, dialect)

    queries: list[ExtractedQuery] = []
    for statement in statements:
        if statement is None or isinstance(statement, exp.Semicolon):
            # Two spellings of "this segment holds no query": None for an empty one
            # (a trailing semicolon, a stray `;;`), and a Semicolon node for one that
            # holds only comments — `-- migration notes` on its own line parses to a
            # Semicolon carrying the comment and nothing else.
            #
            # Neither produces a token, so `_statement_spans` skips both, and neither
            # may advance the span cursor. That is why the cursor counts the queries
            # actually built rather than enumerating `statements`: pairing by the
            # enumerate() position hands every statement after a stray semicolon or a
            # comment the *previous* statement's text and line, and lets a comment
            # through as a query of its own.
            continue

        # Position among the statements that actually exist, which is what `spans` is
        # indexed by and what "the Nth statement in this file" means to a reviewer.
        ordinal = len(queries)

        # `text` is contractually the query *as written* (see models/query.py), so it
        # is sliced from the source rather than re-rendered from the AST. Rendering
        # would silently rewrite what the reviewer sees — `Customer c` becomes
        # `Customer AS c`, `:tier` becomes `%(tier)s` — and a report that quotes a
        # query the author never wrote is hard to trust.
        original, line = spans[ordinal] if ordinal < len(spans) else (None, None)

        queries.append(
            ExtractedQuery(
                id=query_id(path, ordinal + 1),
                kind=kind,
                text=original if original is not None else statement.sql(dialect=dialect),
                normalized=statement.sql(dialect=dialect, normalize=True),
                dialect=dialect,
                provenance=Provenance(file=path, line=line),
            )
        )

    return queries


def _statement_spans(content: str, dialect: str) -> list[tuple[str, int]]:
    """Original text and starting line of each statement, split on semicolon tokens.

    Uses the tokenizer rather than ``content.split(";")`` for the same reason the
    parser splits statements: a semicolon inside a string literal or a dollar-quoted
    body is not a boundary.
    """
    try:
        tokens = sqlglot.tokenize(content, read=dialect)
    except sqlglot.TokenError:
        return []

    spans: list[tuple[str, int]] = []
    start_index: int | None = None
    start_line = 1

    for token in tokens:
        if token.token_type is sqlglot.TokenType.SEMICOLON:
            if start_index is not None:
                spans.append((content[start_index : token.start].strip(), start_line))
                start_index = None
            continue
        if start_index is None:
            start_index = token.start
            start_line = token.line

    if start_index is not None:
        spans.append((content[start_index:].strip(), start_line))

    return spans
