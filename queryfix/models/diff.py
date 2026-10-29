"""Diff contracts — what a pull request looks like before it becomes sources.

Stage 1 has two halves that are worth keeping apart. One is *what GitHub said*: a
list of changed paths, each with a status and a patch. The other is *what the
pipeline will read*: a list of :class:`~queryfix.models.query.SourceFile`. This
module holds the first, so that the conversion between them is a pure function
over models instead of a function over whatever shape PyGithub happens to return
this release.

That separation is what lets the whole of stage 1 be tested from a recorded
payload with no network: :class:`ChangedFile` is the seam, and
``tests/fixtures/diffs/`` is a real one filled in.
"""

from __future__ import annotations

import logging
from enum import Enum

from pydantic import Field

from queryfix.models.base import Contract
from queryfix.models.query import SourceFile
from queryfix.models.report import RunContext

__all__ = [
    "ChangeStatus",
    "ChangedFile",
    "DiffIngest",
    "SkipReason",
    "SkippedFile",
]

logger = logging.getLogger(__name__)


class ChangeStatus(str, Enum):
    """What a pull request did to one file, as GitHub reports it.

    These are GitHub's own spellings, not ours, so that a recorded payload can be
    loaded verbatim. Note ``REMOVED`` rather than "deleted" — a mismatch that is
    easy to write and produces a deletion analyzed as a modification, which is why
    the value is pinned by a test.
    """

    ADDED = "added"
    MODIFIED = "modified"
    RENAMED = "renamed"
    REMOVED = "removed"
    COPIED = "copied"
    CHANGED = "changed"
    UNCHANGED = "unchanged"

    @classmethod
    def parse(cls, value: str) -> ChangeStatus:
        """Read a status GitHub sent, tolerating one this version does not know.

        Strict parsing would mean a status added to the API after this release
        takes down the whole run rather than one file. An unknown status is read
        as a modification, which is the conservative choice in the direction that
        matters: the file is analyzed rather than silently dropped from a review.
        """
        try:
            return cls(value)
        except ValueError:
            logger.warning(
                "unrecognized pull-request file status %r; treating it as %r",
                value,
                cls.MODIFIED.value,
            )
            return cls.MODIFIED


class ChangedFile(Contract):
    """One entry of GitHub's ``GET /repos/{repo}/pulls/{n}/files`` response.

    Narrowed to the fields stage 1 actually reads. Everything else in that payload
    — blob URLs, per-file SHAs, the contents link — describes how to fetch things
    the pipeline fetches by other means, and carrying it would only invite a stage
    to reach for the network from somewhere that should not.
    """

    path: str = Field(
        min_length=1,
        description="Path in the head commit. For a rename this is the new path, "
        "which is the one a finding must anchor to.",
    )
    status: ChangeStatus
    previous_path: str | None = Field(
        default=None,
        description="Path in the base commit, set only when the file moved.",
    )
    patch: str | None = Field(
        default=None,
        description="Unified diff for this file. Absent for a pure rename, and "
        "withheld by GitHub for binary or very large files.",
    )
    additions: int = Field(default=0, ge=0)
    deletions: int = Field(default=0, ge=0)


class SkipReason(str, Enum):
    """Why a changed file produced no source, when that is not a failure.

    Skipping and degrading are different claims and must not share a channel. A
    skip says *there was nothing here to analyze*; a degradation says *there was
    something here and we could not read it*. Collapsing the two would make a pull
    request full of Markdown look like a pull request QueryFix choked on.
    """

    DELETED = "deleted"
    UNSUPPORTED_LANGUAGE = "unsupported-language"
    NO_TEXT_CHANGE = "no-text-change"


class SkippedFile(Contract):
    """A changed file that was deliberately not turned into a source."""

    path: str = Field(min_length=1)
    reason: SkipReason


class DiffIngest(Contract):
    """Stage 1's output: the run, its sources, and an account of everything else.

    Every changed file in the pull request appears in exactly one of ``sources``,
    ``skipped``, or ``degraded_stages``. That total is the contract — a file that
    fell out of all three would be a file nobody can tell was never looked at,
    which is the failure mode CLAUDE.md's invariant 5 exists to prevent.
    """

    context: RunContext
    sources: list[SourceFile] = Field(
        default_factory=list,
        description="Head-side files an extractor claims, in the order GitHub "
        "listed them, each carrying the hunks the pull request touched.",
    )
    skipped: list[SkippedFile] = Field(
        default_factory=list,
        description="Changed files with nothing to analyze, and why.",
    )
    degraded_stages: list[str] = Field(
        default_factory=list,
        description='Files that should have been read and could not, as "ingest:<path>". '
        "Shaped to merge straight into Report.degraded_stages.",
    )
