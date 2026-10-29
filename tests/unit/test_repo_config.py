"""``.queryfix.yml``: parsing, policy layering, path ignores, and the end-to-end wiring."""

from __future__ import annotations

import dataclasses

import pytest
import yaml

from queryfix import cli
from queryfix.models.finding import Severity
from queryfix.models.query import SourceFile
from queryfix.policy import EnforcementPolicy, EnforcementStatus, ReviewResult
from queryfix.repo_config import (
    CONFIG_PATH,
    InvalidRepoConfig,
    RepoConfig,
    apply_to_policy,
    filter_ignored_sources,
    parse_repo_config,
)
from tests.conftest import RecordedGitHub, RecordedPullRequest

# --- parsing ----------------------------------------------------------------------


def test_an_empty_file_is_all_defaults() -> None:
    assert parse_repo_config("") == RepoConfig()
    assert parse_repo_config("# just a comment\n") == RepoConfig()


def test_every_documented_key_parses() -> None:
    config = parse_repo_config(
        "block-severities: [CRITICAL]\n"
        "warn-rules: [no-limit]\n"
        "block-rules: [missing-where]\n"
        "disable-rules: [select-star]\n"
        "ignore-paths: ['src/test/**']\n"
    )

    assert config.block_severities == [Severity.CRITICAL]
    assert config.warn_rules == ["no-limit"]
    assert config.block_rules == ["missing-where"]
    assert config.disable_rules == ["select-star"]
    assert config.ignore_paths == ["src/test/**"]


def test_an_empty_block_list_is_distinct_from_unset() -> None:
    # `block-severities: []` is how a team says "advisory only"; it must not
    # collapse into "unset, use the default".
    assert parse_repo_config("block-severities: []").block_severities == []
    assert parse_repo_config("").block_severities is None


@pytest.mark.parametrize(
    "text",
    [
        "block-severity: [high]",  # a typo must not silently do nothing
        "block-severities: [urgent]",
        "warn-rules: [' ']",
        "- just\n- a list",
        "key: [unclosed",
    ],
)
def test_invalid_config_is_rejected_not_ignored(text: str) -> None:
    with pytest.raises(InvalidRepoConfig, match=r"\.queryfix\.yml"):
        parse_repo_config(text)


# --- layering onto the org policy -------------------------------------------------


def test_the_file_sets_blocking_severities_when_the_org_did_not() -> None:
    policy = apply_to_policy(
        EnforcementPolicy(),
        parse_repo_config("block-severities: [critical]"),
        org_sets_block_severities=False,
    )

    assert policy.blocking_severities == frozenset({Severity.CRITICAL})


def test_the_org_setting_outranks_the_file() -> None:
    org = EnforcementPolicy(blocking_severities=frozenset({Severity.CRITICAL, Severity.HIGH}))
    policy = apply_to_policy(
        org, parse_repo_config("block-severities: []"), org_sets_block_severities=True
    )

    assert policy.blocking_severities == org.blocking_severities


def test_rule_lists_are_unioned_with_the_orgs() -> None:
    org = dataclasses.replace(EnforcementPolicy(), warning_rule_ids=frozenset({"select-star"}))
    policy = apply_to_policy(
        org,
        parse_repo_config("warn-rules: [no-limit]\nblock-rules: [missing-where]"),
        org_sets_block_severities=False,
    )

    assert policy.warning_rule_ids == {"select-star", "no-limit"}
    assert policy.blocking_rule_ids == {"missing-where"}


# --- ignore-paths -----------------------------------------------------------------


def _sources(*paths: str) -> list[SourceFile]:
    return [SourceFile(path=path, content="SELECT 1") for path in paths]


@pytest.mark.parametrize(
    ("pattern", "path", "ignored"),
    [
        ("src/test/**", "src/test/java/FooTest.java", True),
        ("src/test/**", "src/main/java/Foo.java", False),
        ("**/legacy/**", "a/b/legacy/c.sql", True),
        ("**/legacy/**", "legacy/c.sql", True),
        ("**/legacy/**", "a/legacyish/c.sql", False),
        ("*.sql", "top.sql", True),
        ("*.sql", "db/nested.sql", False),  # a single * does not cross directories
        ("/db/*.sql", "db/x.sql", True),
        ("db/V?__x.sql", "db/V1__x.sql", True),
    ],
)
def test_ignore_path_globs(pattern: str, path: str, ignored: bool) -> None:
    kept, dropped = filter_ignored_sources(
        _sources(path), parse_repo_config(yaml.safe_dump({"ignore-paths": [pattern]}))
    )

    assert (dropped == [path]) is ignored
    assert (kept == []) is ignored


def test_no_ignore_paths_keeps_everything() -> None:
    kept, dropped = filter_ignored_sources(_sources("a.sql", "b.sql"), RepoConfig())

    assert [source.path for source in kept] == ["a.sql", "b.sql"]
    assert dropped == []


# --- end to end through cli.review ------------------------------------------------


def _with_config(recorded: RecordedPullRequest, text: str) -> RecordedGitHub:
    head_text = {**recorded.head_text, CONFIG_PATH: text}
    return RecordedGitHub(recorded=dataclasses.replace(recorded, head_text=head_text))


def _rule_ids(result: ReviewResult) -> set[str]:
    return {finding.rule_id for finding in result.findings}


def test_a_repo_without_a_config_behaves_exactly_as_before(
    recorded_github: RecordedGitHub, recorded_pr: RecordedPullRequest
) -> None:
    result = cli.review(recorded_pr.repo, recorded_pr.number, client=recorded_github.client)

    assert result.status is EnforcementStatus.BLOCKED
    assert {"missing-where", "no-limit"} <= _rule_ids(result)


def test_disable_rules_removes_a_rule_from_the_run(recorded_pr: RecordedPullRequest) -> None:
    fake = _with_config(recorded_pr, "disable-rules: [no-limit]")

    result = cli.review(recorded_pr.repo, recorded_pr.number, client=fake.client)

    assert "no-limit" not in _rule_ids(result)
    assert "missing-where" in _rule_ids(result)


def test_empty_block_severities_makes_the_repo_advisory(recorded_pr: RecordedPullRequest) -> None:
    fake = _with_config(recorded_pr, "block-severities: []")

    result = cli.review(recorded_pr.repo, recorded_pr.number, client=fake.client)

    assert result.findings
    assert result.blocking_findings == []
    assert result.status is not EnforcementStatus.BLOCKED


def test_the_org_setting_keeps_the_gate_closed_despite_the_repo_file(
    recorded_pr: RecordedPullRequest,
) -> None:
    fake = _with_config(recorded_pr, "block-severities: []")

    result = cli.review(
        recorded_pr.repo,
        recorded_pr.number,
        client=fake.client,
        org_sets_block_severities=True,
    )

    assert result.status is EnforcementStatus.BLOCKED


def test_ignore_paths_stops_a_file_being_reviewed(recorded_pr: RecordedPullRequest) -> None:
    fake = _with_config(recorded_pr, "ignore-paths: ['queryfix-sandbox/**']")

    result = cli.review(recorded_pr.repo, recorded_pr.number, client=fake.client)

    assert result.findings == []
    assert result.report.queries == []


def test_an_invalid_config_degrades_loudly_and_never_changes_the_gate(
    recorded_pr: RecordedPullRequest,
) -> None:
    fake = _with_config(recorded_pr, "block-severity: []")  # typo of block-severities

    result = cli.review(recorded_pr.repo, recorded_pr.number, client=fake.client)

    assert any(stage.startswith("repo_config:") for stage in result.report.degraded_stages)
    # Defaults applied, so the real findings still block rather than the typo
    # quietly turning the gate off.
    assert result.status is EnforcementStatus.BLOCKED


def test_advisory_cannot_be_reversed_by_a_repo_asking_for_blocking_rules(
    recorded_pr: RecordedPullRequest,
) -> None:
    fake = _with_config(recorded_pr, "block-rules: [missing-where, no-limit]")

    result = cli.review(
        recorded_pr.repo,
        recorded_pr.number,
        client=fake.client,
        enforcement_policy=EnforcementPolicy(advisory=True),
    )

    assert result.findings  # still reported
    assert result.blocking_findings == []
    assert result.status is not EnforcementStatus.BLOCKED
