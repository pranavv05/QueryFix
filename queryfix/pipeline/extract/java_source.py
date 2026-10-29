"""Stage 2 (Java) — the tokenization boundary underneath Java extraction.

Java extraction still recognizes annotations and method declarations by pattern
(the JavaParser sidecar in CLAUDE.md's tech-stack table is the eventual answer),
but a pattern applied to raw source cannot tell code from the things that merely
look like it. That distinction is not a nicety:

* ``// @Query("SELECT c FROM Ghost c")`` in a comment is not a query. Matching it
  reports a finding against a query the pull request does not contain, which is
  the one kind of wrong a review bot cannot be.
* ``content.find("}")`` is not a closing brace. A repository carrying a ``default``
  method closes *that* method's body first, so every derived method after it is
  silently invisible.

This module owns exactly that distinction and nothing else. It scans once,
classifies every byte as code, comment, or literal, and exposes three things the
extractor needs: masked views to match against, the kind at an offset, and real
bracket matching. It answers questions *about* Java text; it knows nothing about
queries, and swapping it for the sidecar means replacing one collaborator rather
than untangling extraction from lexing.

Every view it produces is the same length as the source it was built from, so an
offset found in a mask is an offset into the original. Provenance therefore
survives masking, which is what lets the extractor match against a scrubbed view
and still slice the query text a reviewer will recognize.
"""

from __future__ import annotations

import bisect
from enum import Enum

__all__ = ["JavaRegionKind", "JavaSource"]


class JavaRegionKind(Enum):
    """What a stretch of Java source is."""

    CODE = "code"
    LINE_COMMENT = "line_comment"
    BLOCK_COMMENT = "block_comment"
    STRING = "string"
    TEXT_BLOCK = "text_block"
    CHAR = "char"


_COMMENT_KINDS = frozenset({JavaRegionKind.LINE_COMMENT, JavaRegionKind.BLOCK_COMMENT})
_LITERAL_KINDS = frozenset({JavaRegionKind.STRING, JavaRegionKind.TEXT_BLOCK, JavaRegionKind.CHAR})

#: Bracket pairs :meth:`JavaSource.matching_bracket` understands.
_CLOSERS = {"{": "}", "(": ")", "[": "]"}


class JavaSource:
    """One Java file, scanned into code / comment / literal regions.

    Construct with :meth:`of`. Instances are read-only and safe to share: every
    view is computed once at construction and never mutated afterwards.
    """

    __slots__ = ("_code", "_kinds", "_line_starts", "_starts", "_structure", "_text")

    def __init__(self, text: str) -> None:
        self._text = text

        regions = _scan(text)
        self._starts = [start for start, _, _ in regions]
        self._kinds = [kind for _, _, kind in regions]

        # Two masks rather than one, because the two consumers want opposite
        # things. Annotation matching needs literals intact — the query *is* a
        # string literal — but must not see comments. Bracket matching needs the
        # reverse: a brace inside a string or a comment is not structure.
        self._code = _mask(text, regions, _COMMENT_KINDS)
        self._structure = _mask(text, regions, _COMMENT_KINDS | _LITERAL_KINDS)

        self._line_starts = _line_starts(text)

    @classmethod
    def of(cls, text: str) -> JavaSource:
        """Scan ``text`` once and return the views built from it."""
        return cls(text)

    @property
    def text(self) -> str:
        """The source exactly as supplied. Query text is sliced from here."""
        return self._text

    @property
    def code(self) -> str:
        """The source with comment bodies blanked, literals left readable.

        Match annotations and declarations against this. Blanking preserves both
        length and line structure, so offsets and ``(?m)^`` anchors behave as they
        would on the original.
        """
        return self._code

    @property
    def structure(self) -> str:
        """The source with comments *and* literal bodies blanked.

        Only brackets that actually nest survive here, which is what makes
        :meth:`matching_bracket` trustworthy.
        """
        return self._structure

    def kind_at(self, offset: int) -> JavaRegionKind:
        """What the byte at ``offset`` belongs to.

        Offsets past the end report :attr:`JavaRegionKind.CODE`; there is nothing
        there, and no caller should treat "past the end" as "inside a comment".
        """
        if not self._starts or offset < 0:
            return JavaRegionKind.CODE
        index = bisect.bisect_right(self._starts, offset) - 1
        if index < 0:
            return JavaRegionKind.CODE
        return self._kinds[index]

    def is_code(self, offset: int) -> bool:
        """Whether ``offset`` is real code rather than a comment or a literal."""
        return self.kind_at(offset) is JavaRegionKind.CODE

    def line_at(self, offset: int) -> int:
        """The 1-based line ``offset`` falls on.

        Resolved by binary search over precomputed line starts rather than
        ``text.count("\\n", 0, offset)``, which rescans the file from the top for
        every query and turns a large repository interface into quadratic work.
        """
        return bisect.bisect_right(self._line_starts, offset)

    def matching_bracket(self, open_index: int) -> int | None:
        """Index of the bracket closing the one at ``open_index``, or None.

        Counts over :attr:`structure`, so braces inside comments and string
        literals do not open or close anything. Returns None for an unbalanced
        source, which the caller must treat as "this region has no known extent"
        rather than guessing one.
        """
        opener = self._structure[open_index : open_index + 1]
        closer = _CLOSERS.get(opener)
        if closer is None:
            return None

        depth = 0
        for index in range(open_index, len(self._structure)):
            character = self._structure[index]
            if character == opener:
                depth += 1
            elif character == closer:
                depth -= 1
                if depth == 0:
                    return index
        return None

    def is_blank_between(self, start: int, end: int) -> bool:
        """Whether only whitespace separates two offsets, ignoring comments.

        Reads :attr:`code`, so a comment between an annotation and the method it
        decorates counts as whitespace — which is what a Java compiler does too.
        """
        return not self._code[start:end].strip()


def _scan(text: str) -> list[tuple[int, int, JavaRegionKind]]:
    """Classify ``text`` into non-overlapping ``(start, end, kind)`` regions.

    A single left-to-right pass, because the constructs are mutually exclusive
    once entered: a ``"`` inside a comment starts nothing, and a ``//`` inside a
    string is two characters. Unterminated constructs run to a defensible
    boundary (end of line for a quote, end of file for a block comment) rather
    than raising — this scans whatever a diff contains, including a file that is
    mid-edit, and refusing to scan is worse than scanning conservatively.
    """
    regions: list[tuple[int, int, JavaRegionKind]] = []
    length = len(text)
    code_start = 0
    index = 0

    while index < length:
        kind, end = _region_at(text, index, length)
        if kind is None:
            index += 1
            continue

        if code_start < index:
            regions.append((code_start, index, JavaRegionKind.CODE))
        regions.append((index, end, kind))
        index = end
        code_start = end

    if code_start < length:
        regions.append((code_start, length, JavaRegionKind.CODE))
    return regions


def _region_at(text: str, index: int, length: int) -> tuple[JavaRegionKind | None, int]:
    """The non-code region starting at ``index``, or ``(None, index)``."""
    character = text[index]

    if character == "/" and index + 1 < length:
        following = text[index + 1]
        if following == "/":
            end = text.find("\n", index)
            return JavaRegionKind.LINE_COMMENT, length if end == -1 else end
        if following == "*":
            end = text.find("*/", index + 2)
            return JavaRegionKind.BLOCK_COMMENT, length if end == -1 else end + 2

    if text.startswith('"""', index):
        # Checked before the single quote so a text block's opening delimiter is
        # not read as an empty string followed by a stray quote.
        end = text.find('"""', index + 3)
        return JavaRegionKind.TEXT_BLOCK, length if end == -1 else end + 3

    if character == '"':
        return JavaRegionKind.STRING, _end_of_quoted(text, index, '"', length)

    if character == "'":
        return JavaRegionKind.CHAR, _end_of_quoted(text, index, "'", length)

    return None, index


def _end_of_quoted(text: str, start: int, quote: str, length: int) -> int:
    """Exclusive end of a single-line quoted literal opened at ``start``.

    A Java string or char literal cannot span a line, so an unterminated one ends
    at the newline — which keeps the rest of the file classified as code instead
    of swallowing it into a literal that never closes.
    """
    index = start + 1
    while index < length:
        character = text[index]
        if character == "\\":
            index += 2
            continue
        if character == quote:
            return index + 1
        if character == "\n":
            return index
        index += 1
    return length


def _mask(
    text: str,
    regions: list[tuple[int, int, JavaRegionKind]],
    blanked: frozenset[JavaRegionKind],
) -> str:
    """``text`` with every region of a blanked kind replaced by spaces.

    Newlines are kept so line numbers, and the ``(?m)`` anchors the extractor
    relies on, survive. Everything else becomes a space, which preserves length —
    the property that lets a caller match here and slice from the original.
    """
    if not any(kind in blanked for _, _, kind in regions):
        return text

    pieces: list[str] = []
    for start, end, kind in regions:
        segment = text[start:end]
        pieces.append(_blank(segment) if kind in blanked else segment)
    return "".join(pieces)


def _blank(segment: str) -> str:
    """``segment`` reduced to spaces, keeping its newlines."""
    return "".join("\n" if character == "\n" else " " for character in segment)


def _line_starts(text: str) -> list[int]:
    """Offset of the first character of each line, for binary-search lookups."""
    starts = [0]
    index = text.find("\n")
    while index != -1:
        starts.append(index + 1)
        index = text.find("\n", index + 1)
    return starts
