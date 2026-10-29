"""Targeted retrieval of repository interfaces the pull request did not change.

This is the seam that makes real pull requests work: most of them edit a call site
and leave the repository alone. These tests cover the CLI-side resolver — that it
asks for the right path, asks once, and degrades quietly — using the recorded
GitHub fixture, so nothing here touches the network.
"""

from __future__ import annotations

from github.GithubException import GithubException

from queryfix import cli
from queryfix.models.report import RunContext
from tests.conftest import RecordedGitHub


def context(recorded: RecordedGitHub) -> RunContext:
    """A run context aimed at the recorded pull request, so the fake will answer."""
    return RunContext(
        run_id="run-1",
        repo=recorded.recorded.repo,
        pr_number=recorded.recorded.number,
        base_sha=recorded.recorded.base_sha,
        head_sha=recorded.recorded.head_sha,
    )


def test_a_resolver_reads_at_the_head_commit(recorded_github: RecordedGitHub) -> None:
    run = context(recorded_github)
    resolve = cli._java_source_resolver(run, recorded_github.client)
    assert resolve is not None

    path = recorded_github.recorded.path("OrderRepository.java")
    content = resolve(path)

    assert content is not None
    assert "interface OrderRepository" in content
    # Pinned to the SHA, never to a branch name that could move mid-run.
    assert recorded_github.head_reads == [(path, recorded_github.recorded.head_sha)]


def test_a_missing_file_is_cached_as_absent(recorded_github: RecordedGitHub) -> None:
    """A 404 must cost one request, however many call sites want the type."""
    run = context(recorded_github)
    resolve = cli._java_source_resolver(run, recorded_github.client)
    assert resolve is not None

    first = resolve("src/main/java/com/example/data/Absent.java")
    second = resolve("src/main/java/com/example/data/Absent.java")

    assert first is None
    assert second is None
    assert len(recorded_github.head_reads) == 1


def test_a_successful_read_is_cached(recorded_github: RecordedGitHub) -> None:
    run = context(recorded_github)
    resolve = cli._java_source_resolver(run, recorded_github.client)
    assert resolve is not None

    path = recorded_github.recorded.path("OrderRepository.java")
    assert resolve(path) == resolve(path)
    assert len(recorded_github.head_reads) == 1


def test_github_being_unavailable_degrades_resolution_not_the_run(
    recorded_github: RecordedGitHub,
) -> None:
    recorded_github.raise_on_contents = GithubException(502, {"message": "bad gateway"}, None)
    run = context(recorded_github)
    resolve = cli._java_source_resolver(run, recorded_github.client)
    assert resolve is not None

    # No exception escapes: the analyzer treats None as "not available" and falls
    # back to name-convention resolution.
    assert resolve("src/main/java/com/example/data/ThingRepository.java") is None


def test_no_head_sha_means_no_resolver(recorded_github: RecordedGitHub) -> None:
    """A read that cannot be pinned to a commit is not worth making."""
    unpinned = context(recorded_github).model_copy(update={"head_sha": None})

    assert cli._java_source_resolver(unpinned, recorded_github.client) is None


def test_a_full_review_resolves_across_files_without_network(
    recorded_github: RecordedGitHub,
) -> None:
    """End to end through the CLI against the recorded pull request."""
    report = cli.review(
        recorded_github.recorded.repo,
        recorded_github.recorded.number,
        client=recorded_github.client,
        dry_run=True,
    ).report

    assert "nplusone" not in report.degraded_stages
    # The recorded PR is a repository-only change, so there is no call site in it
    # to report — the point here is that the stage ran and stayed quiet.
    assert all(not finding.rule_id.startswith("nplusone") for finding in report.findings)
