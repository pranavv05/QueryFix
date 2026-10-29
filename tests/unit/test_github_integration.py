"""The GitHub integration and the ingest stage it feeds.

Driven entirely by ``tests/fixtures/diffs/`` through the PyGithub stand-in in
``conftest.py``: no network, no credentials, no ``monkeypatch`` of module state.

Half of this file is about the token. That is proportionate — this is the only
module in QueryFix that handles a credential, so it is the only place one can
escape from, and "no token is ever logged" is a claim that has to be checked
rather than intended.
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path

import pytest
from github.GithubException import GithubException, RateLimitExceededException

from queryfix.config import MissingConfiguration, override_settings
from queryfix.integrations import github
from queryfix.integrations.github import GitHubUnavailable, redact
from queryfix.models.diff import ChangeStatus, SkipReason
from queryfix.models.report import Report, RunContext
from queryfix.pipeline.extract import ExtractorRegistry, SqlExtractor
from queryfix.pipeline.ingest import ingest_pull_request
from queryfix.pipeline.runner import AnalysisRunner
from tests.conftest import RecordedGitHub, RecordedPullRequest

REPOSITORY = Path(__file__).resolve().parents[2]
PACKAGE = REPOSITORY / "queryfix"

#: Shaped like a real classic personal access token so the redactor has to earn
#: its pass. Not a real credential: 40 characters of 'A' after the prefix.
TOKEN = "ghp_" + "A" * 36

ORDER_REPOSITORY = "repository/OrderRepository.java"
DELETED_ENUM = "domain/OrderStatus.java"
README = "queryfix-sandbox/README.md"


# --- Resolving the pull request ------------------------------------------------


def test_fetch_pull_request_records_both_shas(
    recorded_github: RecordedGitHub, recorded_pr: RecordedPullRequest
) -> None:
    context = github.fetch_pull_request(
        recorded_pr.repo, recorded_pr.number, client=recorded_github.client
    )

    assert context.repo == recorded_pr.repo
    assert context.pr_number == recorded_pr.number
    assert context.base_sha == recorded_pr.base_sha
    assert context.head_sha == recorded_pr.head_sha


def test_a_run_id_is_generated_unless_one_is_supplied(
    recorded_github: RecordedGitHub, recorded_pr: RecordedPullRequest
) -> None:
    generated = github.fetch_pull_request(
        recorded_pr.repo, recorded_pr.number, client=recorded_github.client
    )
    supplied = github.fetch_pull_request(
        recorded_pr.repo, recorded_pr.number, run_id="delivery-7", client=recorded_github.client
    )

    assert generated.run_id
    assert supplied.run_id == "delivery-7"


def test_changed_files_are_narrowed_to_models_not_pygithub_objects(
    recorded_github: RecordedGitHub, recorded_pr: RecordedPullRequest
) -> None:
    context = github.fetch_pull_request(
        recorded_pr.repo, recorded_pr.number, client=recorded_github.client
    )

    files = github.fetch_changed_files(context, client=recorded_github.client)
    renamed = next(f for f in files if f.status is ChangeStatus.RENAMED)

    assert len(files) == len(recorded_pr.entries)
    assert renamed.previous_path is not None
    assert renamed.previous_path != renamed.path
    assert renamed.previous_path.endswith("OrderItemRepository.java")
    assert renamed.path.endswith("OrderLineRepository.java")


def test_head_reads_are_pinned_to_the_head_sha(
    recorded_github: RecordedGitHub, recorded_pr: RecordedPullRequest
) -> None:
    # Not to the branch name. A branch moves, and a run that read half its files
    # before a force-push and half after would describe a tree that never existed.
    context = github.fetch_pull_request(
        recorded_pr.repo, recorded_pr.number, client=recorded_github.client
    )

    github.fetch_diff(context, client=recorded_github.client)

    assert recorded_github.head_reads
    assert {ref for _, ref in recorded_github.head_reads} == {recorded_pr.head_sha}


def test_read_file_at_ref_is_pinned_to_the_ref_it_is_given(
    recorded_github: RecordedGitHub, recorded_pr: RecordedPullRequest
) -> None:
    # read_head_file is one caller of this building block, pinned to head_sha.
    # The PR-head Liquibase rebuild in queryfix.cli is another, and must be
    # able to pin to an explicit SHA rather than only ever reading the head of
    # whatever context it is given.
    context = github.fetch_pull_request(
        recorded_pr.repo, recorded_pr.number, client=recorded_github.client
    )
    path = recorded_pr.path(ORDER_REPOSITORY)

    text = github.read_file_at_ref(
        context, path, ref=recorded_pr.head_sha, client=recorded_github.client
    )

    assert text == recorded_pr.head_text[path]
    assert (path, recorded_pr.head_sha) in recorded_github.head_reads


def test_reading_head_without_a_head_sha_is_refused(recorded_github: RecordedGitHub) -> None:
    # Falling back to the default branch here would analyze a different tree and
    # report line numbers against a file the pull request never contained.
    context = RunContext(run_id="r", repo="acme/x", pr_number=1)

    with pytest.raises(GitHubUnavailable, match="no head SHA"):
        github.read_head_file(context, "a.sql", client=recorded_github.client)


def test_fetch_diff_returns_one_source_per_analyzable_file(
    recorded_github: RecordedGitHub, recorded_pr: RecordedPullRequest
) -> None:
    context = github.fetch_pull_request(
        recorded_pr.repo, recorded_pr.number, client=recorded_github.client
    )

    sources = github.fetch_diff(context, client=recorded_github.client)
    paths = [source.path for source in sources]

    assert len(sources) == 4
    assert recorded_pr.path(DELETED_ENUM) not in paths
    assert recorded_pr.path(README) not in paths
    assert all(source.hunks for source in sources)


def test_fetch_diff_honours_an_injected_language_set(
    recorded_github: RecordedGitHub, recorded_pr: RecordedPullRequest
) -> None:
    sql_only = ExtractorRegistry()
    sql_only.register(".sql", SqlExtractor())
    context = github.fetch_pull_request(
        recorded_pr.repo, recorded_pr.number, client=recorded_github.client
    )

    sources = github.fetch_diff(context, client=recorded_github.client, registry=sql_only)

    assert [source.path.rsplit("/", 1)[-1] for source in sources] == [
        "V1__create_tables.sql",
        "V2__reporting_indexes.sql",
    ]


# --- The ingest stage ----------------------------------------------------------


def test_ingest_returns_the_context_and_every_account_of_the_diff(
    recorded_github: RecordedGitHub, recorded_pr: RecordedPullRequest
) -> None:
    result = ingest_pull_request(
        recorded_pr.repo, recorded_pr.number, run_id="run-1", client=recorded_github.client
    )

    assert result.context.run_id == "run-1"
    assert result.context.head_sha == recorded_pr.head_sha
    assert len(result.sources) == 4
    assert {skip.reason for skip in result.skipped} == {
        SkipReason.DELETED,
        SkipReason.UNSUPPORTED_LANGUAGE,
    }
    assert result.degraded_stages == []


def test_one_run_resolves_the_repository_through_one_client(
    recorded_github: RecordedGitHub, recorded_pr: RecordedPullRequest
) -> None:
    # The client is built once in ingest and threaded through every call. Without
    # that, each of the seven requests in this run would authenticate separately.
    ingest_pull_request(recorded_pr.repo, recorded_pr.number, client=recorded_github.client)

    assert set(recorded_github.repo_lookups) == {recorded_pr.repo}


def test_a_file_that_raises_during_ingest_degrades_that_file_only(
    recorded_github: RecordedGitHub, recorded_pr: RecordedPullRequest
) -> None:
    recorded_github.fail_head_for = {recorded_pr.path(ORDER_REPOSITORY)}

    result = ingest_pull_request(
        recorded_pr.repo, recorded_pr.number, client=recorded_github.client
    )

    assert result.degraded_stages == [f"ingest:{recorded_pr.path(ORDER_REPOSITORY)}"]
    assert len(result.sources) == 3


def test_ingest_output_drives_the_rest_of_the_pipeline(
    recorded_github: RecordedGitHub, recorded_pr: RecordedPullRequest
) -> None:
    # The end the milestone is aimed at: a real recorded pull request goes diff ->
    # extract -> rules and produces findings anchored to head-file lines.
    result = ingest_pull_request(
        recorded_pr.repo, recorded_pr.number, run_id="run-1", client=recorded_github.client
    )

    report = AnalysisRunner().run(
        repo=result.context.repo,
        pr_number=result.context.pr_number,
        sources=result.sources,
        run_id=result.context.run_id,
    )

    assert report.findings
    by_path = {source.path: source for source in result.sources}
    for finding in report.findings:
        source = by_path[finding.provenance.file]
        assert finding.provenance.line is not None
        # Every anchor lands inside the head file it names — the property that
        # makes a line number in the PR comment clickable rather than misleading.
        assert 1 <= finding.provenance.line <= len(source.content.split("\n"))


def test_the_deleted_file_never_reaches_the_rule_engine(
    recorded_github: RecordedGitHub, recorded_pr: RecordedPullRequest
) -> None:
    result = ingest_pull_request(
        recorded_pr.repo, recorded_pr.number, client=recorded_github.client
    )
    report = AnalysisRunner().run(
        repo=result.context.repo, pr_number=result.context.pr_number, sources=result.sources
    )

    assert recorded_pr.path(DELETED_ENUM) not in {q.provenance.file for q in report.queries}


# --- Failures cross the boundary as one type -----------------------------------


def test_a_github_error_becomes_one_exception_type_carrying_the_status(
    recorded_github: RecordedGitHub, recorded_pr: RecordedPullRequest
) -> None:
    recorded_github.raise_on_pull = GithubException(404, {"message": "Not Found"}, None)

    with pytest.raises(GitHubUnavailable) as raised:
        github.fetch_pull_request(
            recorded_pr.repo, recorded_pr.number, client=recorded_github.client
        )

    assert "404" in str(raised.value)
    assert "Not Found" in str(raised.value)
    assert f"{recorded_pr.repo}#{recorded_pr.number}" in str(raised.value)


def test_a_non_github_failure_carries_only_its_type_name(
    recorded_github: RecordedGitHub, recorded_pr: RecordedPullRequest
) -> None:
    # An exception from a layer we do not control gets no opportunity to put its
    # own string in front of a user — which is also why it cannot leak one.
    recorded_github.raise_on_files = TimeoutError("connect to 10.0.0.1 timed out")
    context = github.fetch_pull_request(
        recorded_pr.repo, recorded_pr.number, client=recorded_github.client
    )

    with pytest.raises(GitHubUnavailable) as raised:
        github.fetch_changed_files(context, client=recorded_github.client)

    assert "TimeoutError" in str(raised.value)
    assert "10.0.0.1" not in str(raised.value)


def test_the_original_exception_is_not_chained_onto_ours(
    recorded_github: RecordedGitHub, recorded_pr: RecordedPullRequest
) -> None:
    # Chaining would hang the original off ours, and a traceback printer renders
    # the chain — undoing every guarantee about what the message contains.
    recorded_github.raise_on_pull = GithubException(401, {"message": f"token {TOKEN}"}, None)

    with pytest.raises(GitHubUnavailable) as raised:
        github.fetch_pull_request(
            recorded_pr.repo, recorded_pr.number, client=recorded_github.client
        )

    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__


@pytest.mark.parametrize(
    ("status", "exc_type"),
    [
        (404, github.FileNotFoundAtRef),
        (401, github.PermissionDenied),
        (403, github.PermissionDenied),
        (422, github.InvalidRef),
        (500, github.TransientGitHubError),
        (503, github.TransientGitHubError),
        (400, GitHubUnavailable),  # unclassified stays the base type
    ],
)
def test_a_github_status_is_classified_into_the_right_subclass(
    status: int,
    exc_type: type[GitHubUnavailable],
    recorded_github: RecordedGitHub,
    recorded_pr: RecordedPullRequest,
) -> None:
    recorded_github.raise_on_pull = GithubException(status, {"message": "boom"}, None)

    with pytest.raises(exc_type):
        github.fetch_pull_request(
            recorded_pr.repo, recorded_pr.number, client=recorded_github.client
        )


def test_a_not_found_classification_logs_at_info_not_error(
    recorded_github: RecordedGitHub,
    recorded_pr: RecordedPullRequest,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The real defect the task surfaced: an expected miss (a speculatively
    # guessed path that does not exist) read exactly like a real outage in the
    # logs. A 404 must not be logged at ERROR.
    recorded_github.raise_on_pull = GithubException(404, {"message": "Not Found"}, None)

    with caplog.at_level(logging.DEBUG), pytest.raises(github.FileNotFoundAtRef):
        github.fetch_pull_request(
            recorded_pr.repo, recorded_pr.number, client=recorded_github.client
        )

    records = [r for r in caplog.records if r.name == github.logger.name]
    assert len(records) == 1
    assert records[0].levelno == logging.INFO
    assert getattr(records[0], "classification", None) == "not_found"
    assert getattr(records[0], "status", None) == 404


def test_a_permission_denied_classification_still_logs_at_error(
    recorded_github: RecordedGitHub,
    recorded_pr: RecordedPullRequest,
    caplog: pytest.LogCaptureFixture,
) -> None:
    recorded_github.raise_on_pull = GithubException(403, {"message": "Forbidden"}, None)

    with caplog.at_level(logging.DEBUG), pytest.raises(github.PermissionDenied):
        github.fetch_pull_request(
            recorded_pr.repo, recorded_pr.number, client=recorded_github.client
        )

    records = [r for r in caplog.records if r.name == github.logger.name]
    assert len(records) == 1
    assert records[0].levelno == logging.ERROR
    assert getattr(records[0], "classification", None) == "permission_denied"


def test_rate_limit_is_classified_as_transient_not_permission_denied(
    recorded_github: RecordedGitHub, recorded_pr: RecordedPullRequest
) -> None:
    # A 403 from rate limiting is a materially different fact than a bad token:
    # the request might simply succeed on retry once the window resets.
    recorded_github.raise_on_pull = RateLimitExceededException(
        403, {"message": "API rate limit exceeded"}, None
    )

    with pytest.raises(github.TransientGitHubError):
        github.fetch_pull_request(
            recorded_pr.repo, recorded_pr.number, client=recorded_github.client
        )


def test_a_connection_failure_is_classified_as_transient(
    recorded_github: RecordedGitHub, recorded_pr: RecordedPullRequest
) -> None:
    recorded_github.raise_on_pull = ConnectionError("connection reset by peer")

    with pytest.raises(github.TransientGitHubError):
        github.fetch_pull_request(
            recorded_pr.repo, recorded_pr.number, client=recorded_github.client
        )


def test_read_file_at_ref_logs_the_path_and_ref_on_failure(
    recorded_github: RecordedGitHub,
    recorded_pr: RecordedPullRequest,
    caplog: pytest.LogCaptureFixture,
) -> None:
    context = github.fetch_pull_request(
        recorded_pr.repo, recorded_pr.number, client=recorded_github.client
    )
    recorded_github.raise_on_contents = GithubException(404, {"message": "Not Found"}, None)

    with caplog.at_level(logging.DEBUG), pytest.raises(github.FileNotFoundAtRef):
        github.read_file_at_ref(
            context, "missing.java", ref=recorded_pr.head_sha, client=recorded_github.client
        )

    record = next(r for r in caplog.records if r.name == github.logger.name)
    assert getattr(record, "path", None) == "missing.java"
    assert getattr(record, "ref", None) == recorded_pr.head_sha


def test_a_rate_limit_is_reported_as_the_status_it_is(
    recorded_github: RecordedGitHub, recorded_pr: RecordedPullRequest
) -> None:
    recorded_github.raise_on_pull = RateLimitExceededException(
        403, {"message": "API rate limit exceeded"}, None
    )

    with pytest.raises(GitHubUnavailable, match="403"):
        github.fetch_pull_request(
            recorded_pr.repo, recorded_pr.number, client=recorded_github.client
        )


def test_our_own_exception_is_not_wrapped_twice(
    recorded_github: RecordedGitHub, recorded_pr: RecordedPullRequest
) -> None:
    # read_head_file raises GitHubUnavailable itself for a missing head SHA. If the
    # guard re-wrapped it, the message would accumulate a prefix per layer.
    context = RunContext(run_id="r", repo=recorded_pr.repo, pr_number=recorded_pr.number)

    with pytest.raises(GitHubUnavailable) as raised:
        github.read_head_file(context, "a.sql", client=recorded_github.client)

    assert str(raised.value).count("GitHub returned") == 0


# --- The token ------------------------------------------------------------------


@pytest.mark.parametrize(
    "secret",
    [
        "ghp_" + "A" * 36,
        "gho_" + "B" * 36,
        "ghu_" + "C" * 36,
        "ghs_" + "D" * 36,
        "ghr_" + "E" * 36,
        "github_pat_" + "F" * 22 + "_" + "G" * 59,
    ],
    ids=["classic-pat", "oauth", "user-to-server", "server-to-server", "refresh", "fine-grained"],
)
def test_every_credential_shape_github_issues_is_redacted(secret: str) -> None:
    redacted = redact(f"Bad credentials: {secret} rejected")

    assert secret not in redacted
    assert github.REDACTED in redacted


def test_redaction_does_not_eat_ordinary_text() -> None:
    # A redactor that fires on prose gets disabled by the first person it confuses.
    text = "GitHub returned 404 while reading acme/billing#12: Not Found"

    assert redact(text) == text


def test_a_token_inside_githubs_own_error_message_never_reaches_the_caller(
    recorded_github: RecordedGitHub, recorded_pr: RecordedPullRequest
) -> None:
    # The realistic leak: GitHub echoes something containing a credential, and we
    # forward its message verbatim.
    recorded_github.raise_on_pull = GithubException(
        401, {"message": f"Bad credentials for {TOKEN}"}, None
    )

    with pytest.raises(GitHubUnavailable) as raised:
        github.fetch_pull_request(
            recorded_pr.repo, recorded_pr.number, client=recorded_github.client
        )

    assert TOKEN not in str(raised.value)
    assert github.REDACTED in str(raised.value)


def test_a_token_in_an_unfamiliar_exception_never_reaches_the_caller(
    recorded_github: RecordedGitHub, recorded_pr: RecordedPullRequest
) -> None:
    recorded_github.raise_on_pull = ValueError(f"invalid header Authorization: Bearer {TOKEN}")

    with pytest.raises(GitHubUnavailable) as raised:
        github.fetch_pull_request(
            recorded_pr.repo, recorded_pr.number, client=recorded_github.client
        )

    assert TOKEN not in str(raised.value)
    assert "Authorization" not in str(raised.value)


def test_no_log_record_from_a_failing_run_carries_the_token(
    recorded_github: RecordedGitHub,
    recorded_pr: RecordedPullRequest,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # End to end, and through the noisiest path there is: the head read fails with
    # a token-bearing error, ingest catches it at the stage boundary and calls
    # logger.exception, which renders a traceback. Nothing in it may be the token.
    recorded_github.raise_on_contents = GithubException(
        401, {"message": f"Bad credentials {TOKEN}"}, None
    )

    with caplog.at_level(logging.DEBUG), override_settings(github_token=TOKEN) as settings:
        result = ingest_pull_request(
            recorded_pr.repo, recorded_pr.number, client=recorded_github.client
        )

        assert TOKEN not in repr(settings)

    assert len(result.degraded_stages) == 4
    for record in caplog.records:
        assert TOKEN not in record.getMessage()
        assert TOKEN not in str(record.exc_info)
        assert TOKEN not in str(record.__dict__)


def test_a_call_without_an_injected_client_cannot_silently_reach_the_network() -> None:
    # What makes "unit tests run from the recorded fixture with no network calls" a
    # property rather than a habit. Isolate configuration so a developer's real
    # token cannot turn this unit test into a live API call.
    with override_settings(), pytest.raises(MissingConfiguration, match="GITHUB_TOKEN"):
        github.fetch_pull_request("acme/billing", 1)


def test_the_token_is_read_in_exactly_one_place(recorded_pr: RecordedPullRequest) -> None:
    # The structural half of the guarantee: a value that is only ever read once,
    # in a function that hands it straight to Auth.Token, cannot be interpolated
    # into a log line somewhere else. Asserted against the source tree so a second
    # reader added later fails here rather than in an incident.
    del recorded_pr

    readers = [
        path.relative_to(REPOSITORY).as_posix()
        for path in PACKAGE.rglob("*.py")
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "require_github_token"
    ]

    assert readers == ["queryfix/integrations/github.py"]


def test_the_module_never_interpolates_the_token_into_a_string() -> None:
    # `new_client` is the only function that holds the value. Anything it does
    # beyond passing it to Auth.Token — an f-string, a log call, a URL — would
    # show up as the token flowing somewhere else, so pin its whole body.
    source = (PACKAGE / "integrations" / "github.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    new_client = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "new_client"
    )

    calls = [
        node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        for node in ast.walk(new_client)
        if isinstance(node, ast.Call)
    ]

    assert sorted(calls) == ["Github", "Token", "get_settings", "require_github_token"]
    assert not [node for node in ast.walk(new_client) if isinstance(node, ast.JoinedStr)]


# --- Comment upsert -----------------------------------------------------------


def test_first_run_creates_exactly_one_comment(
    recorded_github: RecordedGitHub, recorded_pr: RecordedPullRequest
) -> None:
    context = github.fetch_pull_request(
        recorded_pr.repo, recorded_pr.number, client=recorded_github.client
    )
    report = Report(context=context, queries=[], findings=[], degraded_stages=[])

    comment_id = github.upsert_report_comment(context, report, client=recorded_github.client)

    assert isinstance(comment_id, int) and comment_id > 0
    assert len(recorded_github.comments) == 1
    assert github.COMMENT_MARKER in recorded_github.comments[0].body


def test_second_run_edits_rather_than_creates(
    recorded_github: RecordedGitHub, recorded_pr: RecordedPullRequest
) -> None:
    context = github.fetch_pull_request(
        recorded_pr.repo, recorded_pr.number, client=recorded_github.client
    )
    report1 = Report(context=context, queries=[], findings=[], degraded_stages=[])

    id1 = github.upsert_report_comment(context, report1, client=recorded_github.client)
    assert id1 is not None
    assert len(recorded_github.comments) == 1
    assert "Critical" not in recorded_github.comments[0].body

    # Create a second report with a critical finding to ensure the edit updates the body
    from queryfix.models.finding import Finding, Severity
    from queryfix.models.query import Provenance

    finding = Finding(
        rule_id="missing-where",
        severity=Severity.CRITICAL,
        title="Unqualified write",
        explanation="Explanation",
        impact="Impact",
        suggestions=[],
        provenance=Provenance(file="a.sql", line=1),
    )
    report2 = Report(context=context, queries=[], findings=[finding], degraded_stages=[])

    id2 = github.upsert_report_comment(context, report2, client=recorded_github.client)
    assert id2 == id1
    assert len(recorded_github.comments) == 1  # count stays 1
    assert "Critical" in recorded_github.comments[0].body  # body was updated


def test_changed_comment_marker_is_not_matched(
    recorded_github: RecordedGitHub, recorded_pr: RecordedPullRequest
) -> None:
    context = github.fetch_pull_request(
        recorded_pr.repo, recorded_pr.number, client=recorded_github.client
    )
    # If the user edits the comment or the COMMENT_MARKER changes, the old comment is orphaned.
    from tests.conftest import RecordedIssueComment

    old_comment = RecordedIssueComment("<!-- queryfix:old-report -->\nBody")
    recorded_github.comments.append(old_comment)

    report = Report(context=context, queries=[], findings=[], degraded_stages=[])
    comment_id = github.upsert_report_comment(context, report, client=recorded_github.client)

    # A new comment is created because the old one did not carry COMMENT_MARKER
    assert comment_id != old_comment.id
    assert len(recorded_github.comments) == 2


def test_github_api_failure_during_comment_upsert_raises_unavailable(
    recorded_github: RecordedGitHub, recorded_pr: RecordedPullRequest
) -> None:
    context = github.fetch_pull_request(
        recorded_pr.repo, recorded_pr.number, client=recorded_github.client
    )
    report = Report(context=context, queries=[], findings=[], degraded_stages=[])

    recorded_github.raise_on_comment = GithubException(403, {"message": "Forbidden"}, None)

    with pytest.raises(GitHubUnavailable, match="403"):
        github.upsert_report_comment(context, report, client=recorded_github.client)


def test_queryfix_never_pushes_edits_files_or_merges() -> None:
    # Verify that github.py only retrieves pull requests, reads files, lists files,
    # manages issue comments, and manages its own Pull Request Review. It must not
    # touch ref updates, git commits, other pull requests, or merges. create_review
    # is deliberately permitted (invariant 3, CLAUDE.md) — QueryFix's own review is
    # its one write action against a PR — but never with event="APPROVE" (checked
    # separately below), and PullRequestReview.edit/.dismiss operate only on the
    # review QueryFix itself created and identified by REVIEW_MARKER.
    source = (PACKAGE / "integrations" / "github.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}

    prohibited = {
        "merge",
        "create_commit",
        "update_file",
        "create_git_ref",
        "delete_ref",
        "create_pull",
        "create_git_tag",
        "create_issue",
        "add_to_collaborators",
    }

    intersection = attributes.intersection(prohibited)
    assert not intersection, f"QueryFix calls prohibited PyGithub write APIs: {intersection}"


def test_queryfix_never_approves_a_pull_request() -> None:
    # The one event value invariant 3 forbids outright, independent of the
    # attribute-call check above: no code path may pass the exact string
    # "APPROVE" as a value (an event=... argument, a dict entry). Checked as
    # an exact string-constant match via the AST, not a substring search of
    # the raw source, so this cannot be defeated by mentioning APPROVE in a
    # docstring or comment explaining that it is forbidden.
    source = (PACKAGE / "integrations" / "github.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    literal_approves = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and node.value == "APPROVE"
    ]
    assert not literal_approves
