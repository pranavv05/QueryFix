"""Shared pytest fixtures."""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from github import Github

from queryfix.api.main import app
from queryfix.models.diff import ChangedFile, ChangeStatus

FIXTURES = Path(__file__).resolve().parent / "fixtures"
DIFFS = FIXTURES / "diffs"


@pytest.fixture
def client() -> Iterator[TestClient]:
    """HTTP client bound to the FastAPI app, with no network calls."""
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def nplus1_statement_log() -> str:
    """A real p6spy excerpt captured from the sandbox's N+1 fixture.

    Taken from an actual run rather than hand-written, so the parser is tested
    against the format p6spy really emits — including the setup chatter a real log
    carries.
    """
    return (FIXTURES / "p6spy" / "nplus1-excerpt.log").read_text(encoding="utf-8")


# --- The recorded pull request -------------------------------------------------


@dataclass(frozen=True)
class RecordedPullRequest:
    """``tests/fixtures/diffs/``, loaded — a real PR payload, diff, and head files.

    See that directory's README for how it was recorded and what each case in it
    is for. Nothing here touches the network; the fixture *is* the network.
    """

    repo: str
    number: int
    base_sha: str
    head_sha: str
    entries: tuple[Mapping[str, Any], ...]
    head_text: Mapping[str, str]

    def entry(self, suffix: str) -> Mapping[str, Any]:
        """The one recorded file entry whose path ends with ``suffix``."""
        matches = [e for e in self.entries if str(e["filename"]).endswith(suffix)]
        if len(matches) != 1:
            msg = f"{len(matches)} recorded files end with {suffix!r}; expected exactly 1"
            raise LookupError(msg)
        return matches[0]

    def path(self, suffix: str) -> str:
        """The full recorded path ending with ``suffix``."""
        return str(self.entry(suffix)["filename"])

    def changed_files(self) -> list[ChangedFile]:
        """The recorded payload as the models stage 1 consumes.

        The same narrowing :func:`queryfix.integrations.github.fetch_changed_files`
        performs, so a test can exercise the pure half of ingest without a client.
        """
        return [
            ChangedFile(
                path=str(entry["filename"]),
                status=ChangeStatus.parse(str(entry["status"])),
                previous_path=cast(str | None, entry.get("previous_filename")),
                patch=cast(str | None, entry.get("patch")),
                additions=int(entry["additions"]),
                deletions=int(entry["deletions"]),
            )
            for entry in self.entries
        ]


def _load_recorded() -> RecordedPullRequest:
    pull = json.loads((DIFFS / "sandbox-pull-request.json").read_text(encoding="utf-8"))
    entries = json.loads((DIFFS / "sandbox-pull-request-files.json").read_text(encoding="utf-8"))

    head_root = DIFFS / "sandbox-head"
    head_text = {
        path.relative_to(head_root).as_posix(): path.read_text(encoding="utf-8")
        for path in head_root.rglob("*")
        if path.is_file()
    }

    return RecordedPullRequest(
        repo=str(pull["base"]["repo"]["full_name"]),
        number=int(pull["number"]),
        base_sha=str(pull["base"]["sha"]),
        head_sha=str(pull["head"]["sha"]),
        entries=tuple(entries),
        head_text=head_text,
    )


@pytest.fixture(scope="session")
def recorded_pr() -> RecordedPullRequest:
    """The recorded pull request, read once for the session."""
    return _load_recorded()


# --- A PyGithub stand-in serving that recording --------------------------------


class RecordedFile:
    """One entry of ``GET /pulls/{n}/files``, shaped like ``github.File.File``."""

    def __init__(self, entry: Mapping[str, Any]) -> None:
        self.filename: str = str(entry["filename"])
        self.status: str = str(entry["status"])
        self.previous_filename: str | None = cast(str | None, entry.get("previous_filename"))
        self.patch: str | None = cast(str | None, entry.get("patch"))
        self.additions: int = int(entry["additions"])
        self.deletions: int = int(entry["deletions"])


class RecordedRef:
    """``pull.base`` / ``pull.head``, narrowed to the SHA stage 1 reads."""

    def __init__(self, sha: str) -> None:
        self.sha = sha


class RecordedContents:
    """``repo.get_contents(...)``, narrowed to the bytes stage 1 decodes."""

    def __init__(self, text: str) -> None:
        self.decoded_content = text.encode("utf-8")


class RecordedIssueComment:
    """One PR comment, shaped like ``github.IssueComment.IssueComment``.

    Mutable: ``edit`` replaces the body in place, which is how
    ``upsert_report_comment`` proves that a second run edits rather than creates.
    """

    _next_id: int = 1

    def __init__(self, body: str) -> None:
        self.id: int = RecordedIssueComment._next_id
        RecordedIssueComment._next_id += 1
        self.body: str = body

    def edit(self, body: str) -> None:
        self.body = body


#: The ``review.state`` GitHub reports back for a given submitted event — its
#: own mapping, not QueryFix's: ``github.integrations.github`` defines
#: ``_STATE_FOR_EVENT`` with the same values because it observes this real
#: behavior, not because this fake copies QueryFix's internals.
_REVIEW_STATE_FOR_EVENT = {
    "REQUEST_CHANGES": "CHANGES_REQUESTED",
    "COMMENT": "COMMENTED",
    "APPROVE": "APPROVED",
}


class RecordedPullRequestReview:
    """One Pull Request Review, shaped like ``github.PullRequestReview.PullRequestReview``.

    Mutable the same way :class:`RecordedIssueComment` is: ``edit`` replaces
    the body in place, and ``dismiss`` moves the review to the terminal
    ``DISMISSED`` state and records the message GitHub would show, which is
    how ``upsert_review``'s idempotency strategy is provable end to end.
    """

    _next_id: int = 1

    def __init__(self, body: str, event: str) -> None:
        self.id: int = RecordedPullRequestReview._next_id
        RecordedPullRequestReview._next_id += 1
        self.body: str = body
        self.state: str = _REVIEW_STATE_FOR_EVENT[event]
        self.submitted_at: datetime = datetime.now(UTC)
        self.dismissal_message: str | None = None

    def edit(self, body: str) -> None:
        self.body = body

    def dismiss(self, message: str) -> None:
        self.state = "DISMISSED"
        self.dismissal_message = message


@dataclass
class RecordedGitHub:
    """A PyGithub stand-in that answers from the recording and never uses a socket.

    Deliberately not a ``MagicMock``. The point of these tests is that stage 1
    reads the shape GitHub really returns, and a mock agrees with whatever it is
    asked — including attributes PyGithub does not have. This raises instead.

    The failure hooks exist because per-file fail-soft is a contract, not a
    nicety: ``fail_head_for`` makes one file unreadable so a test can prove the
    others still land.
    """

    recorded: RecordedPullRequest

    #: Paths whose head read raises, to exercise per-file degradation.
    fail_head_for: set[str] = field(default_factory=set)
    #: Raised by ``get_pull`` / ``get_files`` / ``get_contents`` / comments.
    raise_on_pull: BaseException | None = None
    raise_on_files: BaseException | None = None
    raise_on_contents: BaseException | None = None
    raise_on_comment: BaseException | None = None
    #: Raised by ``get_reviews`` / ``create_review``.
    raise_on_review: BaseException | None = None
    #: Overrides the recorded entries, for cases the real PR does not contain.
    entries: tuple[Mapping[str, Any], ...] | None = None

    #: Every ``(path, ref)`` asked for, so a test can prove reads are SHA-pinned.
    head_reads: list[tuple[str, str]] = field(default_factory=list)
    #: How many times a repository was resolved, to prove one run authenticates once.
    repo_lookups: list[str] = field(default_factory=list)
    #: Mutable list of PR comments, for testing upsert_report_comment.
    comments: list[RecordedIssueComment] = field(default_factory=list)
    #: Mutable list of PR reviews, for testing upsert_review.
    reviews: list[RecordedPullRequestReview] = field(default_factory=list)
    #: Paths ``get_git_tree`` reports. Empty means "derive it from
    #: ``recorded.head_text``'s keys" — the common case for a candidate-
    #: discovery test, which already has to populate ``head_text`` with every
    #: file's content and should not also have to repeat its path list.
    repository_tree: list[str] = field(default_factory=list)
    #: Raised by ``get_git_tree``.
    raise_on_tree: BaseException | None = None
    #: Set to report a truncated tree, matching GitHub's own API behavior for
    #: a very large repository.
    tree_truncated: bool = False

    @property
    def client(self) -> Github:
        """This fake, typed as the client the production signatures declare.

        The cast is confined here so no test carries one. It is honest: the
        production code narrows PyGithub to a handful of attributes, and those
        are exactly what this class implements.
        """
        return cast(Github, self)

    # PyGithub surface ----------------------------------------------------------

    def get_repo(self, full_name: str) -> RecordedRepo:
        self.repo_lookups.append(full_name)
        if full_name != self.recorded.repo:
            msg = f"unexpected repository {full_name!r}"
            raise AssertionError(msg)
        return RecordedRepo(self)


class RecordedGitTreeElement:
    """One entry of ``get_git_tree(..., recursive=True).tree``, blob-typed."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.type = "blob"


class RecordedGitTree:
    """``github.GitTree.GitTree``, narrowed to what candidate discovery reads."""

    def __init__(self, paths: Sequence[str], *, truncated: bool) -> None:
        self.tree = [RecordedGitTreeElement(path) for path in paths]
        self.truncated = truncated


class RecordedRepo:
    """``github.Repository.Repository``, narrowed to what stage 1 calls."""

    def __init__(self, owner: RecordedGitHub) -> None:
        self._owner = owner

    def get_git_tree(self, sha: str, recursive: bool = False) -> RecordedGitTree:
        del sha, recursive  # unused: this fake answers from repository_tree regardless
        owner = self._owner
        if owner.raise_on_tree is not None:
            raise owner.raise_on_tree
        paths = owner.repository_tree or list(owner.recorded.head_text.keys())
        return RecordedGitTree(paths, truncated=owner.tree_truncated)

    def get_pull(self, number: int) -> RecordedPull:
        if self._owner.raise_on_pull is not None:
            raise self._owner.raise_on_pull
        if number != self._owner.recorded.number:
            msg = f"unexpected pull request #{number}"
            raise AssertionError(msg)
        return RecordedPull(self._owner)

    def get_contents(self, path: str, ref: str | None = None) -> RecordedContents:
        owner = self._owner
        owner.head_reads.append((path, ref or ""))
        if owner.raise_on_contents is not None:
            raise owner.raise_on_contents
        if path in owner.fail_head_for:
            msg = f"recorded failure reading {path}"
            raise OSError(msg)
        text = owner.recorded.head_text.get(path)
        if text is None:
            msg = f"no head text recorded for {path!r}"
            raise LookupError(msg)
        return RecordedContents(text)


class RecordedPull:
    """``github.PullRequest.PullRequest``, narrowed to what stage 1 calls."""

    def __init__(self, owner: RecordedGitHub) -> None:
        self._owner = owner
        self.base = RecordedRef(owner.recorded.base_sha)
        self.head = RecordedRef(owner.recorded.head_sha)

    def get_files(self) -> list[RecordedFile]:
        if self._owner.raise_on_files is not None:
            raise self._owner.raise_on_files
        entries = (
            self._owner.entries if self._owner.entries is not None else self._owner.recorded.entries
        )
        return [RecordedFile(entry) for entry in entries]

    def get_issue_comments(self) -> list[RecordedIssueComment]:
        if self._owner.raise_on_comment is not None:
            raise self._owner.raise_on_comment
        return list(self._owner.comments)

    def create_issue_comment(self, body: str) -> RecordedIssueComment:
        if self._owner.raise_on_comment is not None:
            raise self._owner.raise_on_comment
        comment = RecordedIssueComment(body)
        self._owner.comments.append(comment)
        return comment

    def get_reviews(self) -> list[RecordedPullRequestReview]:
        if self._owner.raise_on_review is not None:
            raise self._owner.raise_on_review
        return list(self._owner.reviews)

    def create_review(self, *, body: str, event: str) -> RecordedPullRequestReview:
        if self._owner.raise_on_review is not None:
            raise self._owner.raise_on_review
        review = RecordedPullRequestReview(body, event)
        self._owner.reviews.append(review)
        return review


@pytest.fixture
def recorded_github(recorded_pr: RecordedPullRequest) -> RecordedGitHub:
    """A fresh PyGithub stand-in over the recording, per test."""
    return RecordedGitHub(recorded=recorded_pr)
