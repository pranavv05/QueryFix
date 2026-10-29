"""PyGithub wrapper — diff retrieval, review upsert, and comment upsert.

Three things this module owns:

- Fetching the PR diff and base/head SHAs.
- Upserting a single **idempotent, tagged** GitHub Pull Request Review — the
  product's primary output (CLAUDE.md invariant 3/4). :func:`upsert_review`
  creates, edits in place, or dismisses-and-recreates QueryFix's own review,
  identified by :data:`REVIEW_MARKER`, with ``REQUEST_CHANGES`` when a
  blocking finding exists and ``COMMENT`` otherwise — never ``APPROVE``.
- Upserting a single **idempotent, tagged** Markdown *comment*
  (:func:`upsert_report_comment`), kept as a narrower building block some
  callers may still want; :data:`COMMENT_MARKER` and :data:`REVIEW_MARKER`
  are deliberately distinct strings, since a comment and a review are
  different GitHub objects with independent idempotency.

QueryFix's only write actions against a pull request are its own review and
its own comment. It never pushes commits, edits files, merges, or touches
another actor's review or comment.

Keeping the token out of everything
-----------------------------------

This is the only module that handles a GitHub credential, so it is the only place
the credential can escape from. Three rules, each backed by a test in
``tests/unit/test_github_integration.py``:

1. **The token is never interpolated into anything.** It is read from
   :mod:`queryfix.config`, handed to :class:`github.Auth.Token`, and never
   touched again. It appears in no URL, no log record, and no f-string.
2. **No exception from PyGithub is re-raised as-is.** Every call is wrapped, and
   what crosses this module's boundary is a :class:`GitHubUnavailable` whose
   message QueryFix composed: the HTTP status, the operation, and — for a
   :class:`github.GithubException.GithubException` — GitHub's own ``message``
   field. For anything else only the exception's *type name* is carried out, so
   an unfamiliar library cannot leak its own message through us.
3. **Even those are redacted.** :func:`redact` runs over every string this module
   emits, replacing anything token-shaped. It is belt-and-braces against rule 1
   being broken later, and against a token that reached the text from somewhere
   other than our own configuration.

For what it is worth, PyGithub itself already strips ``Authorization`` from the
request headers before logging them — but that is its invariant to keep, not
ours, and rules 1–3 hold whether or not it does.
"""

from __future__ import annotations

import logging
import re
import uuid
from typing import Literal

from github import Auth, Github
from github.GithubException import GithubException, RateLimitExceededException
from github.PullRequest import PullRequest
from github.PullRequestReview import PullRequestReview

from queryfix.models.diff import ChangedFile, ChangeStatus
from queryfix.models.query import SourceFile
from queryfix.models.report import Report, RunContext
from queryfix.pipeline.diff import build_sources
from queryfix.pipeline.extract import ExtractorRegistry
from queryfix.policy import EnforcementStatus, ReviewResult

__all__ = [
    "COMMENT_MARKER",
    "REVIEW_MARKER",
    "FileNotFoundAtRef",
    "GitHubUnavailable",
    "InvalidRef",
    "PermissionDenied",
    "TransientGitHubError",
    "fetch_changed_files",
    "fetch_diff",
    "fetch_pull_request",
    "list_files_at_ref",
    "new_client",
    "read_file_at_ref",
    "read_head_file",
    "redact",
    "upsert_report_comment",
    "upsert_review",
]

logger = logging.getLogger(__name__)

#: Hidden marker identifying QueryFix's own comment. Changing it orphans every
#: comment already posted, so a re-run would add a second one instead of editing.
COMMENT_MARKER = "<!-- queryfix:report -->"

#: Hidden marker identifying QueryFix's own Pull Request Review. Distinct
#: from :data:`COMMENT_MARKER` — a review and a comment are different GitHub
#: objects, each with its own idempotency, and this module never mixes them
#: up when searching for "the" existing one of either kind.
REVIEW_MARKER = "<!-- queryfix:review -->"

#: The event QueryFix submits a review with, per status. Never "APPROVE" —
#: CLAUDE.md invariant 3. FAILED maps to "COMMENT", not "REQUEST_CHANGES":
#: an infrastructure failure that prevented reliable analysis is not evidence
#: of a real problem in the pull request, so it must never read as a
#: substantive blocking decision.
_EVENT_FOR_STATUS: dict[EnforcementStatus, str] = {
    EnforcementStatus.BLOCKED: "REQUEST_CHANGES",
    EnforcementStatus.PASS: "COMMENT",
    EnforcementStatus.DEGRADED: "COMMENT",
    EnforcementStatus.FAILED: "COMMENT",
}

#: The ``review.state`` GitHub reports back for each event this module ever
#: submits (see :data:`_EVENT_FOR_STATUS` — never "APPROVE", so no entry for
#: it here) — not the same strings as the event itself (``"COMMENT"`` in,
#: ``"COMMENTED"`` state out). Used to tell "the existing review already
#: reflects this verdict, edit it in place" from "the verdict changed,
#: something must be dismissed or recreated".
_STATE_FOR_EVENT: dict[str, str] = {
    "REQUEST_CHANGES": "CHANGES_REQUESTED",
    "COMMENT": "COMMENTED",
}

#: States GitHub's own REST API will let a review be dismissed from. A
#: ``COMMENTED``-state review cannot be dismissed at all — this is GitHub's
#: constraint, not a QueryFix choice — which is why :func:`upsert_review`
#: falls back to creating a fresh review rather than dismissing in that case.
_DISMISSIBLE_STATES = frozenset({"CHANGES_REQUESTED", "APPROVED"})

#: States that do not count as "the" existing QueryFix review when searching
#: for one to update: ``PENDING`` is an unsubmitted draft (unreachable through
#: this module, which always submits with an event, but defensive rather than
#: assumed), and ``DISMISSED`` is already inert — treating it as absent means
#: a dismissed review is replaced by a fresh one rather than edited back to life.
_INACTIVE_REVIEW_STATES = frozenset({"PENDING", "DISMISSED"})

#: What a redacted secret is replaced with. A fixed string, not a length-preserving
#: mask: how long a token is, is itself something an attacker does not have to guess.
REDACTED = "<redacted>"

#: Every credential shape GitHub currently issues — classic and fine-grained personal
#: tokens (``ghp_``, ``github_pat_``), OAuth (``gho_``), user-to-server and
#: server-to-server installation tokens (``ghu_``, ``ghs_``), and refresh tokens
#: (``ghr_``). Matched by shape rather than by value so redaction does not require
#: holding the secret in order to remove it.
_TOKEN_SHAPED = re.compile(r"gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{20,}")


class GitHubUnavailable(RuntimeError):
    """GitHub could not be reached, or refused a request.

    Deliberately the only exception type every caller *needs* to catch — every
    subclass below is-a :class:`GitHubUnavailable`, so existing
    ``except GitHubUnavailable`` call sites keep working unchanged. The
    subclasses exist for callers, current or future, that want to react
    differently to *why* GitHub was unavailable (a 404 while speculatively
    resolving a type is routine; a 401 never is) without parsing the message
    string to find out.
    """


class FileNotFoundAtRef(GitHubUnavailable):
    """The requested resource does not exist at the given ref (HTTP 404).

    Not necessarily a problem: a 404 while speculatively resolving a
    repository interface's path from a naming convention
    (:mod:`queryfix.cli`'s ``_java_source_resolver``) or while probing a
    Liquibase discovery candidate (:mod:`queryfix.db.discovery`) is an
    expected, routine outcome, not an infrastructure failure — see
    :class:`_guard`'s logging, which reflects that.
    """


class PermissionDenied(GitHubUnavailable):
    """The token was rejected or lacks access (HTTP 401/403)."""


class InvalidRef(GitHubUnavailable):
    """The given ref (SHA/branch) could not be resolved (HTTP 422)."""


class TransientGitHubError(GitHubUnavailable):
    """A failure a retry might not repeat: GitHub 5xx or a connection problem."""


#: HTTP status -> the classification :class:`_guard` raises for it. Anything not
#: listed here (including 404 on a repository/PR lookup, which behaves the same
#: as any other "not found") still raises the base :class:`GitHubUnavailable`
#: with the same message — only classification-dependent behavior (a narrower
#: ``except``, the log level below) uses this table.
_STATUS_CLASSIFICATION: dict[int, type[GitHubUnavailable]] = {
    401: PermissionDenied,
    403: PermissionDenied,
    404: FileNotFoundAtRef,
    422: InvalidRef,
}


def redact(text: str) -> str:
    """Replace anything token-shaped in ``text``.

    Applied to every string this module logs or raises. It cannot make an
    arbitrary secret safe — only known GitHub credential shapes are recognized —
    which is why it is the second line of defence and not the first.
    """
    return _TOKEN_SHAPED.sub(REDACTED, text)


def new_client() -> Github:
    """Build an authenticated client from the process configuration.

    The single place a GitHub credential is read. The value goes straight into
    :class:`github.Auth.Token` and is not retained, logged, or returned.
    """
    # Keep configuration at the point a real client is constructed. This leaves
    # the injectable client seam usable by the explicitly selected offline fixture
    # CLI path, without changing the requirement for every normal GitHub run.
    from queryfix.config import get_settings

    return Github(auth=Auth.Token(get_settings().require_github_token()))


def _resolve(client: Github | None) -> Github:
    """The caller's client, or a new one built from configuration.

    Every entry point takes an optional client for the same reason the extract
    dispatcher takes an optional registry: it is a real seam. A test drives these
    functions against a fake with no token, no network, and no monkeypatching.
    """
    return client if client is not None else new_client()


def fetch_pull_request(
    repo: str,
    pr_number: int,
    *,
    run_id: str | None = None,
    client: Github | None = None,
) -> RunContext:
    """Resolve a PR to a run context, including base and head SHAs.

    ``run_id`` is generated unless supplied; callers pass one when the identifier
    comes from elsewhere — a webhook delivery ID, or a test fixture.
    """
    resolved = _resolve(client)
    with _guard(f"reading {repo}#{pr_number}"):
        pull = resolved.get_repo(repo).get_pull(pr_number)
        base_sha = pull.base.sha
        head_sha = pull.head.sha

    logger.info(
        "ingest: resolved %s#%d base=%s head=%s",
        repo,
        pr_number,
        base_sha,
        head_sha,
        extra={"repo": repo, "pr_number": pr_number, "base_sha": base_sha, "head_sha": head_sha},
    )

    return RunContext(
        run_id=run_id if run_id is not None else str(uuid.uuid4()),
        repo=repo,
        pr_number=pr_number,
        base_sha=base_sha,
        head_sha=head_sha,
    )


def fetch_changed_files(context: RunContext, *, client: Github | None = None) -> list[ChangedFile]:
    """Every file the pull request touched, as models rather than PyGithub objects.

    Narrowing here rather than downstream is what keeps the rest of stage 1 pure:
    :func:`~queryfix.pipeline.diff.build_sources` sees
    :class:`~queryfix.models.diff.ChangedFile` values, so it can be driven from
    a recorded payload without a client at all.
    """
    resolved = _resolve(client)
    with _guard(f"listing files in {context.repo}#{context.pr_number}"):
        pull = resolved.get_repo(context.repo).get_pull(context.pr_number)
        # PyGithub paginates this lazily; materializing it inside the guard means a
        # failure on page two is reported the same way as one on page one.
        return [
            ChangedFile(
                # `filename` is the head-side path — the new one for a rename, which
                # is the path a reviewer will be looking at and the only one a
                # finding may anchor to.
                path=changed.filename,
                status=ChangeStatus.parse(changed.status),
                previous_path=getattr(changed, "previous_filename", None),
                patch=changed.patch,
                additions=changed.additions,
                deletions=changed.deletions,
            )
            for changed in pull.get_files()
        ]


def read_file_at_ref(
    context: RunContext, path: str, *, ref: str, client: Github | None = None
) -> str:
    """The text of ``path`` as it stands at commit ``ref``.

    The building block behind :func:`read_head_file` and the PR-head Liquibase
    rebuild in :mod:`queryfix.cli`: both need a specific commit's content, and a
    SHA rather than a branch name, so a force-push mid-run cannot make two reads
    describe two different trees.
    """
    resolved = _resolve(client)
    with _guard(f"reading {path} at {ref[:8]}", path=path, ref=ref):
        contents = resolved.get_repo(context.repo).get_contents(path, ref=ref)
        if isinstance(contents, list):
            # A directory. Nothing in a changed-files list should resolve to one,
            # so this is a bug or a path collision rather than a normal outcome.
            msg = f"{path} is a directory at {ref[:8]}, not a file"
            raise GitHubUnavailable(msg)
        return contents.decoded_content.decode("utf-8", errors="replace")


def list_files_at_ref(context: RunContext, *, ref: str, client: Github | None = None) -> list[str]:
    """Every file path in the repository tree at commit ``ref``, one API call.

    Deliberately used in exactly one place: Liquibase discovery's repository-wide
    candidate search (:mod:`queryfix.db.candidate_discovery`), reached only
    once the narrow, conventional-location discovery in
    :mod:`queryfix.db.discovery` has already failed. The primary discovery
    path stays exactly as targeted as
    ``docs/liquibase-schema-discovery.md`` documents — a handful of reads, never
    a repository scan; this function exists for the fallback that scan
    deliberately is.

    ``Repository.get_git_tree(sha, recursive=True)`` returns the whole tree —
    blobs and directories both — in one request rather than one per file, so
    only blob (file) paths are returned here.
    """
    resolved = _resolve(client)
    with _guard(f"listing the repository tree at {ref[:8]}", ref=ref):
        tree = resolved.get_repo(context.repo).get_git_tree(ref, recursive=True)
        if tree.truncated:
            # GitHub truncates a very large tree (its own size ceiling, not
            # ours) rather than erroring. Fail-soft: candidate search still
            # runs over whatever it got, just possibly incomplete — worth
            # knowing about, not worth failing the run over.
            logger.warning(
                "github: repository tree at %s was truncated by GitHub's API; "
                "Liquibase candidate search may miss files",
                ref[:8],
                extra={"repo": context.repo, "ref": ref},
            )
        return [entry.path for entry in tree.tree if entry.type == "blob"]


def read_head_file(context: RunContext, path: str, *, client: Github | None = None) -> str:
    """The text of ``path`` as it stands at the pull request's head commit.

    Pinned to ``head_sha`` rather than to the branch name: a branch moves, and a
    run that read half its files before a force-push and half after would produce
    a report describing a tree that never existed.
    """
    ref = context.head_sha
    if ref is None:
        msg = f"cannot read {path}: the run context carries no head SHA"
        raise GitHubUnavailable(msg)
    return read_file_at_ref(context, path, ref=ref, client=client)


def fetch_diff(
    context: RunContext,
    *,
    client: Github | None = None,
    registry: ExtractorRegistry | None = None,
) -> list[SourceFile]:
    """Fetch the PR's diff as one source per changed file, ready for stage 2.

    Each source carries the head-side text in full and the hunks the pull request
    touched, so a finding's line number already refers to the head file rather
    than the base. :mod:`queryfix.pipeline.diff` explains why the content is the
    whole file and not the patch.

    Deleted files produce no source, and neither do files in a language no
    extractor claims. This returns the sources alone; a caller that also needs to
    know *which* files were skipped or unreadable wants
    :func:`queryfix.pipeline.ingest.ingest_pull_request`, which returns all
    three.
    """
    resolved = _resolve(client)
    return build_sources(
        context,
        fetch_changed_files(context, client=resolved),
        lambda path: read_head_file(context, path, client=resolved),
        registry=registry,
    ).sources


def upsert_report_comment(
    context: RunContext,
    report: Report,
    *,
    client: Github | None = None,
) -> int:
    """Create or update QueryFix's single comment on the PR.

    Searches the PR's comments for :data:`COMMENT_MARKER`; edits that comment if
    found, otherwise creates a new one. Returns the comment ID.

    QueryFix comments only. It never pushes commits, edits files, or
    approves/blocks a merge — invariant 3 from CLAUDE.md.
    """
    from queryfix.pipeline.report import render_markdown

    body = render_markdown(report)
    resolved = _resolve(client)

    with _guard(f"upserting comment on {context.repo}#{context.pr_number}"):
        pull = resolved.get_repo(context.repo).get_pull(context.pr_number)

        for comment in pull.get_issue_comments():
            if COMMENT_MARKER in comment.body:
                comment.edit(body)
                logger.info(
                    "github: edited existing comment %d on %s#%d",
                    comment.id,
                    context.repo,
                    context.pr_number,
                    extra={
                        "comment_id": comment.id,
                        "repo": context.repo,
                        "pr_number": context.pr_number,
                    },
                )
                return comment.id

        created = pull.create_issue_comment(body)
        logger.info(
            "github: created comment %d on %s#%d",
            created.id,
            context.repo,
            context.pr_number,
            extra={
                "comment_id": created.id,
                "repo": context.repo,
                "pr_number": context.pr_number,
            },
        )
        return created.id


def upsert_review(
    context: RunContext,
    result: ReviewResult,
    *,
    client: Github | None = None,
) -> int:
    """Create or update QueryFix's single Pull Request Review.

    ``REQUEST_CHANGES`` when ``result.status`` is BLOCKED, ``COMMENT``
    otherwise — never ``APPROVE`` (CLAUDE.md invariant 3). Searches the PR's
    reviews for one carrying :data:`REVIEW_MARKER`, and picks a strategy
    around GitHub's own constraint that a ``COMMENTED``-state review cannot be
    dismissed through the API at all (only ``CHANGES_REQUESTED``/``APPROVED``
    can):

    - No prior marked review -> create one.
    - A prior marked review whose state already matches the new verdict ->
      edit its body in place. The common case on every rerun with an
      unchanged verdict, and the only case that keeps the *same* review
      object rather than creating a new one.
    - A prior ``CHANGES_REQUESTED`` review and the verdict changed -> dismiss
      it (naming why), then create a new review with the current verdict.
    - A prior ``COMMENTED`` review and the verdict changed to
      ``REQUEST_CHANGES`` -> cannot dismiss; create a new review. The stale
      ``COMMENTED`` review is left as inert history — it never blocked the
      merge and still does not — rather than actively misleading, which is
      the safest strategy GitHub's own API limits actually allow.

    Invariant 4 (CLAUDE.md): one *active* QueryFix review always exists
    after this returns, never more than one produced by the same call.
    """
    from queryfix.pipeline.report import render_markdown

    body = render_markdown(result.report, enforcement=result)
    event = _EVENT_FOR_STATUS[result.status]
    resolved = _resolve(client)

    with _guard(f"upserting review on {context.repo}#{context.pr_number}"):
        pull = resolved.get_repo(context.repo).get_pull(context.pr_number)
        existing, orphaned = _find_review(pull)

        for stale in orphaned:
            logger.warning(
                "github: more than one QueryFix review found on %s#%d; %d orphaned",
                context.repo,
                context.pr_number,
                len(orphaned),
                extra={
                    "repo": context.repo,
                    "pr_number": context.pr_number,
                    "orphaned_review_id": stale.id,
                },
            )

        if existing is None:
            created = pull.create_review(body=body, event=event)
            logger.info(
                "github: created review %d (%s) on %s#%d",
                created.id,
                event,
                context.repo,
                context.pr_number,
                extra={
                    "review_id": created.id,
                    "event": event,
                    "repo": context.repo,
                    "pr_number": context.pr_number,
                },
            )
            return created.id

        if existing.state == _STATE_FOR_EVENT[event]:
            existing.edit(body)
            logger.info(
                "github: edited existing review %d on %s#%d",
                existing.id,
                context.repo,
                context.pr_number,
                extra={
                    "review_id": existing.id,
                    "event": event,
                    "repo": context.repo,
                    "pr_number": context.pr_number,
                },
            )
            return existing.id

        if existing.state in _DISMISSIBLE_STATES:
            existing.dismiss(f"Superseded by a newer QueryFix review — {result.status.value}.")
            logger.info(
                "github: dismissed review %d (%s -> %s) on %s#%d",
                existing.id,
                existing.state,
                event,
                context.repo,
                context.pr_number,
                extra={
                    "review_id": existing.id,
                    "previous_state": existing.state,
                    "event": event,
                    "repo": context.repo,
                    "pr_number": context.pr_number,
                },
            )
        # else: existing.state == "COMMENTED", which GitHub's API will not
        # let this module dismiss. Falling through to create a new review is
        # the documented, safest-available strategy — see the docstring.

        created = pull.create_review(body=body, event=event)
        logger.info(
            "github: created review %d (%s) on %s#%d, superseding %d",
            created.id,
            event,
            context.repo,
            context.pr_number,
            existing.id,
            extra={
                "review_id": created.id,
                "event": event,
                "superseded_review_id": existing.id,
                "repo": context.repo,
                "pr_number": context.pr_number,
            },
        )
        return created.id


def _find_review(pull: PullRequest) -> tuple[PullRequestReview | None, list[PullRequestReview]]:
    """QueryFix's own active review, and any others found orphaned.

    The most recently submitted active, marked review is authoritative; older
    ones (should not normally occur — one call always leaves at most one
    active) are returned separately so the caller can log them rather than
    silently picking one.
    """
    marked = [
        review
        for review in pull.get_reviews()
        if REVIEW_MARKER in (review.body or "") and review.state not in _INACTIVE_REVIEW_STATES
    ]
    if not marked:
        return None, []
    marked.sort(key=lambda review: (review.submitted_at, review.id))
    return marked[-1], marked[:-1]


#: Human-readable name for each classification, used in the structured log
#: field so a log consumer does not have to reverse-engineer it from the
#: exception's class name.
_CLASSIFICATION_NAME: dict[type[GitHubUnavailable], str] = {
    FileNotFoundAtRef: "not_found",
    PermissionDenied: "permission_denied",
    InvalidRef: "invalid_ref",
    TransientGitHubError: "transient",
    GitHubUnavailable: "unclassified",
}


def _classify(error: BaseException) -> tuple[type[GitHubUnavailable], int | None]:
    """The exception type to raise and the HTTP status, if any, behind it.

    Rate limiting is classified as transient rather than as a permission
    problem despite arriving as HTTP 403 — the token is fine, the request
    might simply succeed on retry after the window resets, which is a
    materially different fact for a caller (or a future retry policy) to act
    on than "this token cannot do this".

    A non-:class:`GithubException` failure that is-a :class:`OSError` (a
    socket error, a connection reset, ``requests``' own exception hierarchy,
    which descends from it) is transient for the same reason; anything else
    unfamiliar stays unclassified — see :class:`GitHubUnavailable`'s
    docstring for why guessing further would be worse than saying so.
    """
    if isinstance(error, RateLimitExceededException):
        return TransientGitHubError, error.status
    if isinstance(error, GithubException):
        status = error.status
        if status in _STATUS_CLASSIFICATION:
            return _STATUS_CLASSIFICATION[status], status
        if 500 <= status < 600:
            return TransientGitHubError, status
        return GitHubUnavailable, status
    if isinstance(error, OSError):
        return TransientGitHubError, None
    return GitHubUnavailable, None


class _guard:
    """Context manager translating anything GitHub raises into a safe exception.

    A class rather than ``@contextmanager``, and ``__exit__`` annotated
    ``Literal[False]`` rather than ``bool``: both forms of "may return True" tell
    a type checker the manager might *swallow* an exception, which makes every
    ``with _guard(...): return ...`` look like a function that can fall off its
    end. Saying it never suppresses is both the truth and what keeps the callers
    typeable.

    ``**fields`` are additional structured log fields the caller already knows
    (``path``, ``ref``) — recorded alongside the classification so a log line
    answers "what was being read, from where, and what happened" without a
    reader having to parse the free-text message.
    """

    def __init__(self, operation: str, **fields: object) -> None:
        self._operation = operation
        self._fields = fields

    def __enter__(self) -> None:
        return None

    def __exit__(
        self, kind: object, value: BaseException | None, traceback: object
    ) -> Literal[False]:
        if value is None or isinstance(value, GitHubUnavailable):
            return False

        if isinstance(value, GithubException):
            detail = value.data.get("message") if isinstance(value.data, dict) else None
            suffix = f": {redact(str(detail))}" if detail else ""
            message = f"GitHub returned {value.status} while {self._operation}{suffix}"
        else:
            # Only the type name. An exception from a layer we do not control —
            # a socket error, a JSON decode, a library we have not audited — gets
            # no opportunity to put its own string in front of a user.
            message = f"{type(value).__name__} while {self._operation}"

        exc_type, status = _classify(value)
        classification = _CLASSIFICATION_NAME[exc_type]
        log_extra: dict[str, object] = {
            "operation": self._operation,
            "classification": classification,
            "status": status,
            **self._fields,
        }

        # A classified 404 is data, not an infrastructure failure — it is the
        # routine outcome of speculatively resolving a repository interface's
        # path (queryfix.cli's _java_source_resolver) or probing a Liquibase
        # discovery candidate (queryfix.db.discovery). Logging it at ERROR
        # made an expected miss read exactly like a real GitHub outage, which
        # is the log-noise problem a real-world run surfaced. Every
        # other classification is still worth an operator's attention.
        log = logger.info if exc_type is FileNotFoundAtRef else logger.error
        log("github: %s", message, extra=log_extra)
        # `from None`: chaining would hang the original exception off ours, and a
        # traceback printer renders that chain. Suppressing it is the difference
        # between a message we vouch for and a message we merely prefixed.
        raise exc_type(message) from None
