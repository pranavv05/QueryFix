"""Stage 1 (diff) — turn a pull request's changed files into sources to analyze.

Pure, and deliberately so: nothing here opens a socket. GitHub access lives in
:mod:`queryfix.integrations.github`, which hands this module
:class:`~queryfix.models.diff.ChangedFile` values and a way to read head-side
text. That is what makes the whole of stage 1 testable from the recorded payload
in ``tests/fixtures/diffs/`` with no network and no credentials.

Why a source carries the *whole head file* rather than the patch
----------------------------------------------------------------

The obvious implementation reconstructs each file from its patch — take the
context and added lines, drop the removed ones — and hands the extractor that.
It is tempting because it needs no second request, and it is wrong, because a
patch is not a program. QueryFix's fixture pull request demonstrates the
failure exactly: a hunk adds the derived method ``findByStatusAndCustomerId`` to
``OrderRepository``, but the line declaring ``interface OrderRepository`` is
thirty lines above the hunk and so is not in the patch. The Java extractor finds
derived methods *inside a repository interface*, so a reconstructed source would
have quietly reported nothing about a query the pull request had just added. The
same holds for SQL that references a table created earlier in the migration, and
for anything else whose meaning depends on text the author did not touch.

So the content is the file as it stands at the head commit, in full, and the
hunks ride alongside it in
:attr:`~queryfix.models.query.SourceFile.hunks`. Two things follow, and both
are the point:

- **Line numbers need no translation.** Line *n* of the content is line *n* of
  the head file, so a finding anchored where the extractor found it already
  points at the pull request's own file rather than at the base. There is no
  offset arithmetic to get wrong, because there is no offset.
- **"Was this line touched?" is still answerable**, through
  :meth:`~queryfix.models.query.SourceFile.is_changed`. Deciding what to *do*
  with that answer — report only on changed lines, or report everything and rank
  changed lines higher — is the report stage's call, so this stage records the
  hunks and drops nothing.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Sequence

from queryfix.models.diff import ChangedFile, ChangeStatus, DiffIngest, SkippedFile, SkipReason
from queryfix.models.query import Hunk, SourceFile
from queryfix.models.report import RunContext
from queryfix.pipeline.extract import ExtractorRegistry, default_registry

__all__ = ["INGEST_STAGE", "MalformedPatch", "ReadHeadFile", "build_sources", "parse_hunks"]

logger = logging.getLogger(__name__)

#: Ingest degrades per file, as ``ingest:<path>``, matching the ``extract:<path>``
#: convention the runner already uses. One file out of ten unreadable is a
#: materially different report from all ten, and a bare stage name cannot say which.
INGEST_STAGE = "ingest"

#: Reads a path's text at the head commit. Injected rather than imported so this
#: module stays pure and the tests can back it with a recorded fixture.
ReadHeadFile = Callable[[str], str]

#: ``@@ -<base_start>[,<base_lines>] +<head_start>[,<head_lines>] @@`` — the counts
#: are omitted when they are 1, which is the unified-diff convention and the source
#: of the classic off-by-one in hand-rolled parsers.
_HUNK_HEADER = re.compile(
    r"^@@ -(?P<base_start>\d+)(?:,(?P<base_lines>\d+))? "
    r"\+(?P<head_start>\d+)(?:,(?P<head_lines>\d+))? @@"
)


class MalformedPatch(ValueError):
    """A patch could not be read as a unified diff.

    Raised rather than shrugged off. A patch we half-understand yields hunks that
    are subtly wrong, and a wrong hunk means a finding anchored to a line the
    author never wrote — which is worse than saying we could not read the file.
    """


def parse_hunks(patch: str) -> list[Hunk]:
    """Read the hunk headers of one file's unified diff, in order.

    Only the headers carry line coordinates, but the bodies are walked anyway, to
    check that the head-side line count each header *declares* is the count its
    body actually contains. Skipping that check is how a parser silently survives
    a patch it is mis-reading; here a mismatch is the signal that every hunk after
    it is suspect, and it fails the file rather than the run.
    """
    hunks: list[Hunk] = []
    # None until the first header: everything before it is git's file preamble
    # (``diff --git``, ``index``, ``---``, ``+++``), which GitHub's per-file patch
    # omits but a patch read from a ``.diff`` file does not.
    consumed: int | None = None
    # Head line the next body line sits on, and the runs of added lines seen so far in
    # the hunk being read. A hunk's span includes GitHub's context padding, so the
    # exact added lines are tracked separately (see ``Hunk.added_ranges``).
    head_line = 0
    runs: list[tuple[int, int]] = []

    def finish() -> None:
        if hunks and runs:
            hunks[-1] = hunks[-1].model_copy(update={"added_ranges": list(runs)})
        runs.clear()

    for raw in patch.splitlines():
        header = _HUNK_HEADER.match(raw)
        if header is not None:
            _check_declared_length(hunks, consumed)
            finish()
            hunks.append(
                Hunk(
                    base_start=int(header["base_start"]),
                    base_lines=int(header["base_lines"] or 1),
                    head_start=int(header["head_start"]),
                    head_lines=int(header["head_lines"] or 1),
                )
            )
            head_line = int(header["head_start"])
            consumed = 0
            continue

        if consumed is None:
            continue
        if raw.startswith("\\"):
            # "\ No newline at end of file" annotates the line above; it is not one.
            continue
        if raw.startswith("-"):
            continue
        if raw.startswith("+"):
            if runs and runs[-1][1] == head_line - 1:
                runs[-1] = (runs[-1][0], head_line)
            else:
                runs.append((head_line, head_line))
            head_line += 1
            consumed += 1
            continue
        if raw.startswith(" "):
            head_line += 1
            consumed += 1
            continue
        if raw == "":
            # A blank context line is " " in a well-formed diff, but trailing
            # whitespace does not survive every editor, mail gateway, or copy out
            # of a browser. Reading it as the blank context line it plainly is
            # costs nothing; rejecting it would fail files that are fine.
            head_line += 1
            consumed += 1
            continue

        msg = f"unrecognized patch line: {raw[:60]!r}"
        raise MalformedPatch(msg)

    finish()
    _check_declared_length(hunks, consumed)

    if not hunks and patch.strip():
        msg = "patch has content but no hunk header"
        raise MalformedPatch(msg)

    return hunks


def _check_declared_length(hunks: list[Hunk], consumed: int | None) -> None:
    """Verify the hunk just finished held the number of head lines it declared."""
    if consumed is None or not hunks:
        return
    declared = hunks[-1].head_lines
    if consumed != declared:
        msg = (
            f"hunk at head line {hunks[-1].head_start} declares {declared} line(s) "
            f"but contains {consumed}"
        )
        raise MalformedPatch(msg)


def build_sources(
    context: RunContext,
    files: Sequence[ChangedFile],
    read_head: ReadHeadFile,
    registry: ExtractorRegistry | None = None,
) -> DiffIngest:
    """Convert a pull request's changed files into the sources stage 2 will read.

    Every file lands in exactly one of three places, and which one is the whole
    of this function's judgement:

    - **A source**, if some extractor claims the path and the head text can be read.
    - **Skipped**, if there is nothing to analyze — the file was deleted, no
      extractor claims its language, or it only moved.
    - **Degraded**, if it should have been readable and was not. That is per file:
      one unreadable file costs that file and nothing else (invariant 5).

    ``registry`` is injectable for the same reason it is in the extract
    dispatcher: which languages exist is not this function's business, and a test
    should be able to narrow the set without patching module state.
    """
    resolved = registry if registry is not None else default_registry()

    sources: list[SourceFile] = []
    skipped: list[SkippedFile] = []
    degraded: list[str] = []

    for changed in files:
        reason = _skip_reason(changed, resolved)
        if reason is not None:
            skipped.append(SkippedFile(path=changed.path, reason=reason))
            continue

        marker = f"{INGEST_STAGE}:{changed.path}"

        if changed.patch is None:
            # GitHub withholds the patch for binary files and for diffs past its
            # size ceiling. Either way the changed region is unknowable, and
            # analyzing the whole file would report on lines nobody touched.
            logger.warning(
                "no patch for %s; it cannot be located in the diff",
                changed.path,
                extra={"run_id": context.run_id, "source_path": changed.path},
            )
            degraded.append(marker)
            continue

        try:
            hunks = parse_hunks(changed.patch)
            content = read_head(changed.path)
        except Exception:
            # Stage boundary. Both halves are here on purpose: an unreadable patch
            # and an unreadable file are the same caveat to whoever reads the
            # report — *this file was not analyzed* — and splitting them would
            # only invite the two to drift into two spellings of one marker.
            logger.exception(
                "ingest failed for %s; continuing with the remaining files",
                changed.path,
                extra={"run_id": context.run_id, "source_path": changed.path},
            )
            degraded.append(marker)
            continue

        sources.append(SourceFile(path=changed.path, content=content, hunks=hunks))

    logger.info(
        "ingest complete: run_id=%s number_of_sources=%d number_of_skipped=%d degraded=%d",
        context.run_id,
        len(sources),
        len(skipped),
        len(degraded),
        extra={
            "run_id": context.run_id,
            "repo": context.repo,
            "pr_number": context.pr_number,
            "number_of_sources": len(sources),
            "number_of_skipped": len(skipped),
            "degraded_stages": degraded,
        },
    )

    return DiffIngest(context=context, sources=sources, skipped=skipped, degraded_stages=degraded)


def _skip_reason(changed: ChangedFile, registry: ExtractorRegistry) -> SkipReason | None:
    """Why this file yields no source, or None if it should yield one.

    Order matters. Deletion is checked first because it is the most informative
    answer for a file that is both deleted and unanalyzable — and because a
    deleted file's patch is entirely removals, so anything downstream of here
    would be reasoning about text that no longer exists.
    """
    if changed.status is ChangeStatus.REMOVED:
        return SkipReason.DELETED

    if registry.for_path(changed.path) is None:
        # Not a degradation. A pull request contains READMEs, lock files, and
        # images; "nothing to analyze here" is the correct, quiet answer for them,
        # and reporting it as a failure would bury the real ones.
        return SkipReason.UNSUPPORTED_LANGUAGE

    if changed.patch is None and changed.additions == 0 and changed.deletions == 0:
        # A pure rename. The bytes are unchanged, so there is no new query to
        # find; re-reporting the file's existing ones under its new name would be
        # noise on a pull request that only moved it.
        return SkipReason.NO_TEXT_CHANGE

    return None
