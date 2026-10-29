"""Deterministic merge-enforcement policy tests."""

from __future__ import annotations

import pytest

from queryfix.config import Settings
from queryfix.models import (
    ExtractedQuery,
    Finding,
    Provenance,
    QueryKind,
    Report,
    RunContext,
    Severity,
)
from queryfix.policy import EnforcementPolicy, EnforcementStatus, InvalidEnforcementPolicy


def context() -> RunContext:
    return RunContext(run_id="r", repo="acme/billing", pr_number=1)


def finding(
    severity: Severity,
    *,
    rule_id: str = "example-rule",
    title: str | None = None,
    explanation: str = "deterministic evidence",
) -> Finding:
    return Finding(
        rule_id=rule_id,
        severity=severity,
        title=title if title is not None else f"{severity.name} finding",
        explanation=explanation,
        impact="impact",
        provenance=Provenance(file="a.sql", line=1),
    )


def evaluate(*findings: Finding, policy: EnforcementPolicy | None = None) -> EnforcementStatus:
    report = Report(context=context(), findings=list(findings))
    result = (policy if policy is not None else EnforcementPolicy()).evaluate(report)
    return result.status


# --- PASS / BLOCKED, by severity -------------------------------------------------


@pytest.mark.parametrize("severity", [Severity.CRITICAL, Severity.HIGH])
def test_default_policy_blocks_critical_and_high(severity: Severity) -> None:
    assert evaluate(finding(severity)) is EnforcementStatus.BLOCKED


@pytest.mark.parametrize("severity", [Severity.MEDIUM, Severity.LOW, Severity.INFO])
def test_default_policy_allows_medium_low_and_info(severity: Severity) -> None:
    assert evaluate(finding(severity)) is EnforcementStatus.PASS


def test_multiple_findings_with_one_high_are_blocked() -> None:
    result = EnforcementPolicy().evaluate(
        Report(
            context=context(),
            findings=[
                finding(Severity.INFO),
                finding(Severity.MEDIUM),
                finding(Severity.HIGH),
            ],
        )
    )

    assert result.status is EnforcementStatus.BLOCKED
    assert [item.severity for item in result.blocking_findings] == [Severity.HIGH]
    assert [item.severity for item in result.warning_findings] == [
        Severity.INFO,
        Severity.MEDIUM,
    ]


def test_no_findings_and_nothing_degraded_pass() -> None:
    assert EnforcementPolicy().evaluate(Report(context=context())).status is EnforcementStatus.PASS


# --- FAILED: no reliable analysis was possible ------------------------------------


def test_zero_queries_with_a_degraded_stage_is_failed_not_pass() -> None:
    # The signature of ingest never getting past fetching the pull request:
    # queries=[] because nothing was ever read, degraded_stages names why.
    report = Report(context=context(), queries=[], findings=[], degraded_stages=["ingest:boom"])

    assert EnforcementPolicy().evaluate(report).status is EnforcementStatus.FAILED


def test_zero_queries_with_no_degraded_stages_is_pass_not_failed() -> None:
    # A README-only pull request: nothing to review is a clean result, not a
    # failure. Must not read the same as "could not analyze".
    report = Report(context=context(), queries=[], findings=[], degraded_stages=[])

    assert EnforcementPolicy().evaluate(report).status is EnforcementStatus.PASS


def test_failed_takes_priority_even_conceptually_alongside_no_findings() -> None:
    # There are no findings to block on when nothing was read at all — FAILED
    # is reachable purely from the ingest-failure shape, not from a race with
    # BLOCKED.
    report = Report(context=context(), queries=[], findings=[], degraded_stages=["ingest:boom"])
    result = EnforcementPolicy().evaluate(report)

    assert result.status is EnforcementStatus.FAILED
    assert result.blocking_findings == []


def test_a_blocking_finding_with_zero_queries_and_a_degraded_stage_is_blocked_not_failed() -> None:
    # A real-world run hit exactly this shape: the changed files were
    # Java services with no @Query/derived methods of their own (so
    # extraction's queries list is empty), N+1 detection still found a
    # genuine HIGH finding by reading control flow, and posting the review
    # then failed (degrading a stage). Zero queries plus a degraded stage
    # must not be mistaken for "no reliable analysis was possible" once a
    # finding proves otherwise.
    report = Report(
        context=context(),
        queries=[],
        findings=[finding(Severity.HIGH)],
        degraded_stages=["post_review"],
    )

    result = EnforcementPolicy().evaluate(report)

    assert result.status is EnforcementStatus.BLOCKED
    assert result.blocking_findings


# --- DEGRADED: partial analysis, no blocking finding -------------------------------


def _one_query() -> list[ExtractedQuery]:
    return [
        ExtractedQuery(
            id="a.sql:1",
            kind=QueryKind.RAW_SQL,
            text="SELECT 1",
            provenance=Provenance(file="a.sql", line=1),
        )
    ]


def test_a_degraded_stage_with_no_blocking_finding_is_degraded() -> None:
    report = Report(
        context=context(),
        queries=_one_query(),
        findings=[finding(Severity.MEDIUM)],
        degraded_stages=["extract:b.sql"],
    )

    assert EnforcementPolicy().evaluate(report).status is EnforcementStatus.DEGRADED


def test_a_degraded_stage_with_a_blocking_finding_is_blocked_not_degraded() -> None:
    # A blocking finding must never be hidden behind an unrelated stage's
    # failure — BLOCKED outranks DEGRADED.
    report = Report(
        context=context(),
        queries=_one_query(),
        findings=[finding(Severity.HIGH)],
        degraded_stages=["nplusone"],
    )

    result = EnforcementPolicy().evaluate(report)
    assert result.status is EnforcementStatus.BLOCKED
    assert result.blocking_findings


# --- Configuration -----------------------------------------------------------------


def test_custom_blocking_severity_configuration_works() -> None:
    policy = Settings.isolated(block_severities=" medium , low ").enforcement_policy()

    assert evaluate(finding(Severity.MEDIUM), policy=policy) is EnforcementStatus.BLOCKED
    assert evaluate(finding(Severity.LOW), policy=policy) is EnforcementStatus.BLOCKED
    assert evaluate(finding(Severity.HIGH), policy=policy) is EnforcementStatus.PASS


@pytest.mark.parametrize("value", [None, "", "   "])
def test_blank_or_absent_blocking_severities_keep_the_safe_default(value: str | None) -> None:
    settings = Settings.isolated() if value is None else Settings.isolated(block_severities=value)
    policy = settings.enforcement_policy()

    assert evaluate(finding(Severity.HIGH), policy=policy) is EnforcementStatus.BLOCKED


def test_invalid_severity_configuration_fails_clearly() -> None:
    with pytest.raises(InvalidEnforcementPolicy, match="QUERYFIX_BLOCK_SEVERITIES"):
        Settings.isolated(block_severities="HIGH,URGENT").enforcement_policy()


def test_empty_severity_name_fails_clearly() -> None:
    with pytest.raises(InvalidEnforcementPolicy, match="empty severity name"):
        Settings.isolated(block_severities="HIGH,,CRITICAL").enforcement_policy()


def test_prefixed_environment_names_build_the_policy() -> None:
    policy = Settings.from_mapping({"QUERYFIX_BLOCK_SEVERITIES": "MEDIUM"}).enforcement_policy()

    assert evaluate(finding(Severity.MEDIUM), policy=policy) is EnforcementStatus.BLOCKED
    assert evaluate(finding(Severity.HIGH), policy=policy) is EnforcementStatus.PASS


def test_rule_level_overrides_are_authoritative() -> None:
    policy = Settings.isolated(
        block_rules="select-star",
        warn_rules="no-limit",
        ignore_rules="missing-where",
    ).enforcement_policy()

    assert evaluate(finding(Severity.MEDIUM, rule_id="select-star"), policy=policy) is (
        EnforcementStatus.BLOCKED
    )
    assert evaluate(finding(Severity.HIGH, rule_id="no-limit"), policy=policy) is (
        EnforcementStatus.PASS
    )
    assert evaluate(finding(Severity.CRITICAL, rule_id="missing-where"), policy=policy) is (
        EnforcementStatus.PASS
    )


def test_llm_authored_prose_cannot_make_a_high_finding_pass() -> None:
    high = finding(
        Severity.HIGH,
        title="Groq says this is safe",
        explanation="This may not be problematic.",
    )

    assert evaluate(high) is EnforcementStatus.BLOCKED


# --- The report cap: enforcement sees every finding, not just the shown ones -------


def test_enforcement_sees_findings_omitted_from_a_capped_report() -> None:
    # A blocking finding that ranked 21st in a 20-item comment must still
    # block — the comment's length must never change the merge decision.
    shown = [finding(Severity.MEDIUM, rule_id=f"r{i}") for i in range(20)]
    everything_found = [*shown, finding(Severity.CRITICAL, rule_id="the-one-that-matters")]
    report = Report(context=context(), findings=shown, omitted_findings=1)

    result = EnforcementPolicy().evaluate(report, findings=everything_found)

    assert result.status is EnforcementStatus.BLOCKED
    assert result.blocking_findings[0].rule_id == "the-one-that-matters"


# --- advisory mode ----------------------------------------------------------------


def test_advisory_blocks_nothing_even_for_a_critical_finding() -> None:
    policy = EnforcementPolicy(advisory=True)

    assert not policy.should_block(finding(Severity.CRITICAL))
    assert evaluate(finding(Severity.CRITICAL), policy=policy) is EnforcementStatus.PASS


def test_advisory_outranks_an_explicit_blocking_rule() -> None:
    policy = EnforcementPolicy(advisory=True, blocking_rule_ids=frozenset({"rule-x"}))

    assert not policy.should_block(finding(Severity.HIGH, rule_id="rule-x"))


def test_the_default_policy_is_not_advisory() -> None:
    assert EnforcementPolicy().should_block(finding(Severity.HIGH))


def test_settings_build_an_advisory_policy_from_the_env_var() -> None:
    assert Settings(QUERYFIX_ADVISORY="true").enforcement_policy().advisory is True  # type: ignore[call-arg]


@pytest.mark.parametrize("raw", ["", "  ", "false", "0"])
def test_a_blank_or_false_advisory_setting_is_off(raw: str) -> None:
    # An unset workflow variable renders as the empty string, not as false.
    assert Settings(QUERYFIX_ADVISORY=raw).enforcement_policy().advisory is False  # type: ignore[call-arg]


def test_a_blank_block_severities_is_not_advisory() -> None:
    # Documented trap: blank falls back to the blocking default; it is not "off".
    policy = Settings(QUERYFIX_BLOCK_SEVERITIES="").enforcement_policy()  # type: ignore[call-arg]

    assert not policy.advisory
    assert policy.should_block(finding(Severity.HIGH))
