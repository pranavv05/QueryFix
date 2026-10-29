"""CLI orchestration tests, driven entirely by the recorded GitHub fixture."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from github.GithubException import GithubException

from queryfix import cli
from queryfix.config import MissingConfiguration, override_settings
from queryfix.fixtures import load_recorded_fixture
from queryfix.integrations import github
from queryfix.models.report import Report, RunContext
from queryfix.policy import EnforcementPolicy, EnforcementStatus
from tests.conftest import RecordedGitHub, RecordedPullRequest

RECORDED_FIXTURE = Path("tests/fixtures/diffs")


def test_review_parser_accepts_owner_repository_and_positive_pr() -> None:
    args = cli.build_parser().parse_args(["review", "--repo", "acme/billing", "--pr", "42"])

    assert args.repo == "acme/billing"
    assert args.pr == 42


def test_normal_review_requires_github_token() -> None:
    with override_settings(), pytest.raises(MissingConfiguration, match="GITHUB_TOKEN"):
        cli.review("acme/billing", 1)


@pytest.mark.parametrize("number", ["0", "-1", "not-a-number"])
def test_review_parser_rejects_invalid_pr_numbers(number: str) -> None:
    with pytest.raises(SystemExit, match="2"):
        cli.build_parser().parse_args(["review", "--repo", "acme/billing", "--pr", number])


@pytest.mark.parametrize(
    "repo", ["acme", "acme/", "/billing", "acme/billing/extra", "acme billing"]
)
def test_review_parser_rejects_invalid_repository_format(repo: str) -> None:
    with pytest.raises(SystemExit, match="2"):
        cli.build_parser().parse_args(["review", "--repo", repo, "--pr", "42"])


def test_review_runs_recorded_fixture_without_network_and_preserves_ingest_context(
    recorded_github: RecordedGitHub, recorded_pr: RecordedPullRequest
) -> None:
    fake = recorded_github
    pull = recorded_pr
    result = cli.review(pull.repo, pull.number, client=fake.client, dry_run=True)

    assert result.report.context.base_sha == pull.base_sha
    assert result.report.context.head_sha == pull.head_sha
    assert result.report.findings
    assert fake.reviews == []


def test_main_prints_rendered_markdown(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    report = Report(context=RunContext(run_id="r", repo="acme/billing", pr_number=1))
    result = EnforcementPolicy().evaluate(report)
    monkeypatch.setattr(cli, "review", lambda *args, **kwargs: result)

    assert cli.main(["review", "--repo", "acme/billing", "--pr", "1"]) == 0
    assert (
        capsys.readouterr().out == "<!-- queryfix:review -->\n\n## QueryFix\n\nStatus: PASS\n\n"
        "No queries were found in this change.\n"
    )


def test_post_comment_creates_then_updates_one_review(
    recorded_github: RecordedGitHub, recorded_pr: RecordedPullRequest
) -> None:
    # The recorded fixture PR has blocking findings on every run, so this also
    # proves idempotency: the second call edits the same CHANGES_REQUESTED
    # review in place rather than creating a second one.
    fake = recorded_github
    pull = recorded_pr

    cli.review(pull.repo, pull.number, client=fake.client, post_comment=True)
    cli.review(pull.repo, pull.number, client=fake.client, post_comment=True)

    assert len(fake.reviews) == 1
    assert fake.reviews[0].state == "CHANGES_REQUESTED"


def test_dry_run_never_writes_even_when_post_comment_is_requested(
    recorded_github: RecordedGitHub, recorded_pr: RecordedPullRequest
) -> None:
    fake = recorded_github
    pull = recorded_pr

    cli.review(pull.repo, pull.number, client=fake.client, post_comment=True, dry_run=True)

    assert fake.reviews == []


def test_offline_fixture_dry_run_needs_no_token_network_or_writes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def unexpected_client() -> None:
        raise AssertionError("fixture mode must not create a network client")

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(github, "new_client", unexpected_client)
    recorded, client = load_recorded_fixture(RECORDED_FIXTURE)

    # The fixture PR's static findings include a CRITICAL and three HIGH —
    # BLOCKED under the default policy, so this run exits 2, not 0.
    assert (
        cli.main(
            [
                "review",
                "--repo",
                recorded.repo,
                "--pr",
                str(recorded.number),
                "--dry-run",
                "--fixture",
                str(RECORDED_FIXTURE),
            ]
        )
        == cli.EXIT_BLOCKED
    )
    rendered = capsys.readouterr().out
    assert rendered.startswith("<!-- queryfix:review -->\n\n## QueryFix\n\nStatus: BLOCKED\n")
    assert (
        "Reviewed 20 queries and found 7 problems. 2 more findings in code this change did not touch are not shown."
        in rendered
    )
    # The fixture client intentionally has no comment API at all.
    assert not hasattr(
        client.get_repo(recorded.repo).get_pull(recorded.number), "create_issue_comment"
    )


def test_fixture_mode_refuses_comment_posting() -> None:
    recorded, _ = load_recorded_fixture(RECORDED_FIXTURE)

    with pytest.raises(SystemExit, match="2"):
        cli.main(
            [
                "review",
                "--repo",
                recorded.repo,
                "--pr",
                str(recorded.number),
                "--dry-run",
                "--fixture",
                str(RECORDED_FIXTURE),
                "--post-comment",
            ]
        )


def test_comment_failure_is_fail_soft_and_the_report_is_still_printed(
    recorded_github: RecordedGitHub,
    recorded_pr: RecordedPullRequest,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake = recorded_github
    pull = recorded_pr
    fake.raise_on_review = GithubException(403, {"message": "Forbidden"}, None)
    monkeypatch.setattr(github, "new_client", lambda: fake.client)

    # --no-llm: this test is about GitHub review-post failure, not about LLM
    # explanations, and must not depend on whatever GROQ_API_KEY happens to be
    # configured in the ambient environment running the suite. The fixture PR
    # is BLOCKED regardless of the post failure — a posting failure must not
    # hide a real blocking finding — so this exits EXIT_BLOCKED, not 0.
    assert (
        cli.main(
            ["review", "--repo", pull.repo, "--pr", str(pull.number), "--post-comment", "--no-llm"]
        )
        == cli.EXIT_BLOCKED
    )
    assert "Not fully analyzed" in capsys.readouterr().out


def test_ingestion_failure_is_fail_soft_reports_no_fake_findings_and_never_prints_a_token(
    recorded_github: RecordedGitHub,
    recorded_pr: RecordedPullRequest,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # GitHub being unreachable while resolving the PR — expired credentials, a
    # rate limit, a transient outage — is not a QueryFix bug (CLAUDE.md
    # invariant 5): it must degrade, not fail the check, and it must not claim a
    # review that never happened by rendering an empty "no problems" comment.
    fake = recorded_github
    pull = recorded_pr
    token = "ghp_" + "Z" * 36
    fake.raise_on_pull = GithubException(401, {"message": f"Bad credentials {token}"}, None)
    monkeypatch.setattr(github, "new_client", lambda: fake.client)

    # No reliable analysis was possible at all: FAILED, not PASS — the exit
    # code must be able to distinguish "QueryFix could not run" from "ran
    # and found nothing", which the old exit-0-always behavior could not.
    assert cli.main(["review", "--repo", pull.repo, "--pr", str(pull.number)]) == cli.EXIT_FAILED
    output = capsys.readouterr()
    assert token not in output.out
    assert token not in output.err
    assert "<redacted>" in output.out

    assert "QueryFix could not analyze this pull request" in output.out
    assert "No problems" not in output.out
    assert "```sql" not in output.out  # no query text: nothing was ever read
    assert "ingest" in output.out


def test_ingestion_failure_reaches_the_caller_as_a_degraded_report_with_no_findings(
    recorded_github: RecordedGitHub, recorded_pr: RecordedPullRequest
) -> None:
    fake = recorded_github
    pull = recorded_pr
    fake.raise_on_pull = GithubException(404, {"message": "Not Found"}, None)

    result = cli.review(pull.repo, pull.number, client=fake.client)

    assert result.report.context.repo == pull.repo
    assert result.report.context.pr_number == pull.number
    assert result.report.queries == []
    assert result.report.findings == []
    assert any(stage.startswith("ingest:") for stage in result.report.degraded_stages)
    assert result.status is EnforcementStatus.FAILED


def test_a_second_ingestion_failure_shape_is_also_fail_soft(
    recorded_github: RecordedGitHub, recorded_pr: RecordedPullRequest
) -> None:
    # fetch_changed_files, not fetch_pull_request, is the one that fails here —
    # both unguarded calls inside ingest_pull_request must degrade the same way.
    fake = recorded_github
    pull = recorded_pr
    fake.raise_on_files = TimeoutError("connect timed out")

    result = cli.review(pull.repo, pull.number, client=fake.client)

    assert result.report.queries == []
    assert result.report.findings == []
    assert any(stage.startswith("ingest:") for stage in result.report.degraded_stages)


def test_an_ingestion_failure_still_attempts_to_post_a_degraded_review(
    recorded_github: RecordedGitHub, recorded_pr: RecordedPullRequest
) -> None:
    # The review's target is `repo`/`pr_number` alone, known before ingestion
    # ever runs — a total ingestion failure must not also forfeit the review.
    # Uses a failure in listing files, not in resolving the pull request
    # itself, so the later `get_pull` inside the review upsert is unaffected
    # and can actually prove the post succeeds despite ingestion having
    # failed.
    fake = recorded_github
    pull = recorded_pr
    fake.raise_on_files = GithubException(500, {"message": "Internal Server Error"}, None)

    cli.review(pull.repo, pull.number, client=fake.client, post_comment=True)

    assert len(fake.reviews) == 1
    assert github.REVIEW_MARKER in fake.reviews[0].body
    # FAILED never REQUEST_CHANGES: an infrastructure failure that prevented
    # any reliable analysis is not evidence of a real problem in the pull
    # request.
    assert fake.reviews[0].state == "COMMENTED"


def test_a_persistent_github_failure_degrades_both_ingestion_and_the_post(
    recorded_github: RecordedGitHub, recorded_pr: RecordedPullRequest
) -> None:
    # A bad token or a sustained outage blocks every call, not just the first —
    # ingestion and the review upsert both hit it. The two fail-soft layers
    # must compose rather than one undoing the other: still no exception, no
    # review, both failures named.
    fake = recorded_github
    pull = recorded_pr
    fake.raise_on_pull = GithubException(401, {"message": "Bad credentials"}, None)

    result = cli.review(pull.repo, pull.number, client=fake.client, post_comment=True)

    assert fake.reviews == []
    assert result.report.queries == []
    assert result.report.findings == []
    assert any(stage.startswith("ingest:") for stage in result.report.degraded_stages)
    assert "post_review" in result.report.degraded_stages
    assert result.status is EnforcementStatus.FAILED


def test_a_programming_bug_during_ingestion_still_fails_loudly(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The new try/except around ingestion catches exactly GitHubUnavailable. A bug
    # in QueryFix's own code — surfacing here as anything else, a TypeError
    # stands in for one — must still reach the caller. Silently degrading that
    # would hide a real QueryFix failure behind a green check, which invariant 5
    # exists to prevent, not cause.
    def broken(*args: object, **kwargs: object) -> None:
        raise TypeError("build_sources received the wrong shape")

    monkeypatch.setattr("queryfix.pipeline.ingest.ingest_pull_request", broken)
    # A client that is never actually used — `broken` raises before anything
    # touches it — just present so `review` gets past needing a real token.
    monkeypatch.setattr(github, "new_client", lambda: object())

    assert cli.main(["review", "--repo", "acme/billing", "--pr", "1"]) == 1
    assert "build_sources received the wrong shape" not in capsys.readouterr().err


def test_unrecoverable_configuration_error_returns_nonzero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        cli, "review", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("secret"))
    )

    assert cli.main(["review", "--repo", "acme/billing", "--pr", "1"]) == 1
    assert "secret" not in capsys.readouterr().err


def test_import_error_message_and_traceback_are_always_reported(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # ImportError (including ModuleNotFoundError) messages only ever name a
    # module or symbol, never request/response data or a configured secret, so
    # this is always safe to show — no --debug required. This is what turns a
    # bare "ModuleNotFoundError" in CI into something actionable.
    missing = ImportError("cannot import name 'MissingThing' from 'queryfix.example'")
    monkeypatch.setattr(cli, "review", lambda *args, **kwargs: (_ for _ in ()).throw(missing))

    assert cli.main(["review", "--repo", "acme/billing", "--pr", "1"]) == 1
    err = capsys.readouterr().err
    assert "QueryFix failed: ImportError: cannot import name 'MissingThing'" in err
    assert "Traceback (most recent call last):" in err


def test_module_not_found_error_message_and_traceback_are_always_reported(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = ModuleNotFoundError("No module named 'groq'")
    monkeypatch.setattr(cli, "review", lambda *args, **kwargs: (_ for _ in ()).throw(missing))

    assert cli.main(["review", "--repo", "acme/billing", "--pr", "1"]) == 1
    err = capsys.readouterr().err
    assert "QueryFix failed: ModuleNotFoundError: No module named 'groq'" in err
    assert "Traceback (most recent call last):" in err


def test_debug_reports_an_arbitrary_exception_traceback_only_when_passed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Unlike ImportError, an arbitrary third-party exception's message could
    # embed secret-bearing context (e.g. a URL or header), so it stays behind
    # the explicit --debug opt-in rather than being shown unconditionally.
    broken = RuntimeError("request to https://example.com/?token=shh failed")
    monkeypatch.setattr(cli, "review", lambda *args, **kwargs: (_ for _ in ()).throw(broken))

    assert cli.main(["review", "--repo", "acme/billing", "--pr", "1"]) == 1
    assert "token=shh" not in capsys.readouterr().err

    assert cli.main(["review", "--repo", "acme/billing", "--pr", "1", "--debug"]) == 1
    err = capsys.readouterr().err
    assert "token=shh" in err
    assert "Traceback (most recent call last):" in err


def test_cli_only_orchestrates_existing_pipeline_components() -> None:
    tree = ast.parse(Path("queryfix/cli.py").read_text(encoding="utf-8"))
    definitions = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}

    assert (
        not {
            "fetch_pull_request",
            "fetch_diff",
            "ingest",
            "render_markdown",
            "upsert_report_comment",
        }
        & definitions
    )
