"""``upsert_review`` against the recorded PyGithub stand-in — no network, ever.

Mirrors ``test_github_integration.py``'s comment-upsert tests in structure and
intent, for the Pull Request Review path: idempotency, GitHub's own
dismiss-ability constraint, marker isolation, and fail-soft on a GitHub
failure.
"""

from __future__ import annotations

import pytest
from github.GithubException import GithubException

from queryfix.integrations import github
from queryfix.integrations.github import GitHubUnavailable
from queryfix.models.finding import Finding, Severity
from queryfix.models.query import Provenance
from queryfix.models.report import Report, RunContext
from queryfix.policy import EnforcementPolicy, EnforcementStatus, ReviewResult
from tests.conftest import RecordedGitHub, RecordedPullRequest, RecordedPullRequestReview


def _context(recorded_pr: RecordedPullRequest) -> RunContext:
    return RunContext(
        run_id="r",
        repo=recorded_pr.repo,
        pr_number=recorded_pr.number,
        head_sha=recorded_pr.head_sha,
    )


def _finding(severity: Severity) -> Finding:
    return Finding(
        rule_id="missing-where",
        severity=severity,
        title="Unqualified write",
        explanation="Explanation",
        impact="Impact",
        provenance=Provenance(file="a.sql", line=1),
    )


def _blocked_result(context: RunContext) -> ReviewResult:
    report = Report(context=context, findings=[_finding(Severity.HIGH)])
    return EnforcementPolicy().evaluate(report)


def _pass_result(context: RunContext) -> ReviewResult:
    report = Report(context=context, findings=[])
    return EnforcementPolicy().evaluate(report)


# --- First run / idempotent edit --------------------------------------------------


def test_first_run_creates_exactly_one_review(
    recorded_github: RecordedGitHub, recorded_pr: RecordedPullRequest
) -> None:
    context = _context(recorded_pr)

    review_id = github.upsert_review(
        context, _blocked_result(context), client=recorded_github.client
    )

    assert isinstance(review_id, int) and review_id > 0
    assert len(recorded_github.reviews) == 1
    assert github.REVIEW_MARKER in recorded_github.reviews[0].body
    assert recorded_github.reviews[0].state == "CHANGES_REQUESTED"


def test_blocked_status_requests_changes_pass_status_comments(
    recorded_github: RecordedGitHub, recorded_pr: RecordedPullRequest
) -> None:
    context = _context(recorded_pr)

    github.upsert_review(context, _pass_result(context), client=recorded_github.client)

    assert recorded_github.reviews[0].state == "COMMENTED"


def test_second_run_with_the_same_verdict_edits_in_place(
    recorded_github: RecordedGitHub, recorded_pr: RecordedPullRequest
) -> None:
    context = _context(recorded_pr)

    first_id = github.upsert_review(
        context, _blocked_result(context), client=recorded_github.client
    )
    assert "Critical" not in recorded_github.reviews[0].body

    from queryfix.models.finding import Finding as F

    critical = F(
        rule_id="missing-where",
        severity=Severity.CRITICAL,
        title="Unqualified write",
        explanation="Explanation",
        impact="Impact",
        provenance=Provenance(file="a.sql", line=1),
    )
    report = Report(context=context, findings=[critical])
    second_id = github.upsert_review(
        context, EnforcementPolicy().evaluate(report), client=recorded_github.client
    )

    assert second_id == first_id
    assert len(recorded_github.reviews) == 1  # count stays 1: edited, not recreated
    assert "Critical" in recorded_github.reviews[0].body
    assert recorded_github.reviews[0].state == "CHANGES_REQUESTED"


# --- Verdict transitions, around GitHub's own dismiss-ability constraint ----------


def test_blocked_to_pass_dismisses_the_changes_requested_review_and_creates_a_new_one(
    recorded_github: RecordedGitHub, recorded_pr: RecordedPullRequest
) -> None:
    context = _context(recorded_pr)
    first_id = github.upsert_review(
        context, _blocked_result(context), client=recorded_github.client
    )

    second_id = github.upsert_review(context, _pass_result(context), client=recorded_github.client)

    assert second_id != first_id
    assert len(recorded_github.reviews) == 2
    original = next(r for r in recorded_github.reviews if r.id == first_id)
    assert original.state == "DISMISSED"
    assert original.dismissal_message is not None
    assert "PASS" in original.dismissal_message
    newest = next(r for r in recorded_github.reviews if r.id == second_id)
    assert newest.state == "COMMENTED"


def test_pass_to_blocked_cannot_dismiss_a_commented_review_so_creates_a_new_one(
    recorded_github: RecordedGitHub, recorded_pr: RecordedPullRequest
) -> None:
    # GitHub's own REST API constraint: a COMMENT-event review's resulting
    # COMMENTED state cannot be dismissed at all. The old review is left as
    # inert history rather than crashing or silently vanishing.
    context = _context(recorded_pr)
    first_id = github.upsert_review(context, _pass_result(context), client=recorded_github.client)

    second_id = github.upsert_review(
        context, _blocked_result(context), client=recorded_github.client
    )

    assert second_id != first_id
    assert len(recorded_github.reviews) == 2
    original = next(r for r in recorded_github.reviews if r.id == first_id)
    assert original.state == "COMMENTED"  # untouched: never dismissed
    newest = next(r for r in recorded_github.reviews if r.id == second_id)
    assert newest.state == "CHANGES_REQUESTED"


def test_a_third_run_after_a_transition_edits_the_newest_review_in_place(
    recorded_github: RecordedGitHub, recorded_pr: RecordedPullRequest
) -> None:
    context = _context(recorded_pr)
    github.upsert_review(context, _blocked_result(context), client=recorded_github.client)
    second_id = github.upsert_review(context, _pass_result(context), client=recorded_github.client)

    third_id = github.upsert_review(context, _pass_result(context), client=recorded_github.client)

    assert third_id == second_id
    assert len(recorded_github.reviews) == 2  # no third review created


# --- Marker isolation ---------------------------------------------------------------


def test_an_unmarked_review_is_never_touched(
    recorded_github: RecordedGitHub, recorded_pr: RecordedPullRequest
) -> None:
    context = _context(recorded_pr)
    human_review = RecordedPullRequestReview("Looks good to me", "APPROVE")
    recorded_github.reviews.append(human_review)

    github.upsert_review(context, _blocked_result(context), client=recorded_github.client)

    assert len(recorded_github.reviews) == 2
    assert human_review.body == "Looks good to me"
    assert human_review.state == "APPROVED"  # never edited or dismissed


def test_orphaned_marked_reviews_do_not_prevent_finding_the_newest_one(
    recorded_github: RecordedGitHub, recorded_pr: RecordedPullRequest
) -> None:
    context = _context(recorded_pr)
    stale = RecordedPullRequestReview(f"{github.REVIEW_MARKER}\nold", "COMMENT")
    recorded_github.reviews.append(stale)
    newest = RecordedPullRequestReview(f"{github.REVIEW_MARKER}\nnewer", "COMMENT")
    recorded_github.reviews.append(newest)

    result_id = github.upsert_review(context, _pass_result(context), client=recorded_github.client)

    assert result_id == newest.id
    assert len(recorded_github.reviews) == 2  # edited the newest, no third created


# --- Failure is fail-soft at the caller, not silently swallowed here --------------


def test_a_github_failure_during_review_upsert_raises_unavailable(
    recorded_github: RecordedGitHub, recorded_pr: RecordedPullRequest
) -> None:
    context = _context(recorded_pr)
    recorded_github.raise_on_review = GithubException(403, {"message": "Forbidden"}, None)

    with pytest.raises(GitHubUnavailable, match="403"):
        github.upsert_review(context, _blocked_result(context), client=recorded_github.client)


def test_a_failed_analysis_never_requests_changes(
    recorded_github: RecordedGitHub, recorded_pr: RecordedPullRequest
) -> None:
    # An infrastructure failure that prevented any reliable analysis is not
    # evidence of a real problem in the pull request — it must never read as
    # a substantive REQUEST_CHANGES decision.
    context = _context(recorded_pr)
    report = Report(context=context, queries=[], findings=[], degraded_stages=["ingest:boom"])
    result = EnforcementPolicy().evaluate(report)
    assert result.status is EnforcementStatus.FAILED

    github.upsert_review(context, result, client=recorded_github.client)

    assert recorded_github.reviews[0].state == "COMMENTED"
