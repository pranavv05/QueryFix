"""``rank_findings`` and ``cap_findings`` — dedup, ordering, and the report cap."""

from __future__ import annotations

import pytest

from queryfix.models import Finding, Provenance, Severity
from queryfix.pipeline.report import DEFAULT_MAX_FINDINGS, cap_findings, rank_findings


def _finding(
    *,
    rule_id: str = "select-star",
    severity: Severity = Severity.MEDIUM,
    title: str = "t",
    file: str = "a.sql",
    line: int | None = 1,
    query_id: str | None = "a.sql:1",
) -> Finding:
    return Finding(
        rule_id=rule_id,
        severity=severity,
        title=title,
        explanation="e",
        impact="i",
        provenance=Provenance(file=file, line=line),
        query_id=query_id,
    )


# --- Deduplication ---------------------------------------------------------------


def test_an_exact_repeat_collapses_to_one() -> None:
    # Same rule, same query, same location, same title — the shape produced when a
    # caller submits the same source twice in one request.
    findings = [_finding(), _finding()]

    assert len(rank_findings(findings)) == 1


def test_a_repeat_at_a_different_location_is_not_a_duplicate() -> None:
    # Two migrations with the same copy-pasted mistake are two things to fix.
    findings = [
        _finding(file="a.sql", query_id="a.sql:1"),
        _finding(file="b.sql", query_id="b.sql:1"),
    ]

    assert len(rank_findings(findings)) == 2


def test_two_rules_on_the_same_query_are_both_kept() -> None:
    findings = [
        _finding(rule_id="select-star", title="SELECT * finding"),
        _finding(rule_id="no-limit", title="no-limit finding"),
    ]

    assert {finding.rule_id for finding in rank_findings(findings)} == {"select-star", "no-limit"}


def test_the_same_query_id_reused_across_distinct_extracted_queries_still_dedupes() -> None:
    # test_pipeline_contracts.py::test_the_same_source_supplied_twice_is_analyzed_twice
    # pins that a repeated source repeats its query id. The rule engine then raises
    # the identical finding against each `ExtractedQuery` object; this is where that
    # collapses back to one line in the comment.
    findings = [_finding(query_id="a.sql:1"), _finding(query_id="a.sql:1")]

    assert len(rank_findings(findings)) == 1


def test_first_occurrence_is_kept() -> None:
    first = _finding(title="first")
    second = first.model_copy(update={"title": "first"})  # identical key, distinct object

    assert rank_findings([first, second])[0] is first


# --- Ordering ----------------------------------------------------------------------


def test_severity_order_is_worst_first() -> None:
    findings = [
        _finding(severity=Severity.LOW, query_id="q1"),
        _finding(severity=Severity.CRITICAL, query_id="q2"),
        _finding(severity=Severity.MEDIUM, query_id="q3"),
        _finding(severity=Severity.HIGH, query_id="q4"),
    ]

    ranked = rank_findings(findings)

    assert [finding.severity for finding in ranked] == [
        Severity.CRITICAL,
        Severity.HIGH,
        Severity.MEDIUM,
        Severity.LOW,
    ]


def test_ranking_is_stable_for_equal_severity() -> None:
    findings = [_finding(query_id=f"q{i}", severity=Severity.MEDIUM) for i in range(10)]

    assert [finding.query_id for finding in rank_findings(findings)] == [
        finding.query_id for finding in findings
    ]


def test_ranking_the_same_input_twice_gives_the_same_output() -> None:
    findings = [
        _finding(query_id="q1", severity=Severity.HIGH),
        _finding(query_id="q2", severity=Severity.CRITICAL),
        _finding(query_id="q3", severity=Severity.HIGH),
    ]

    assert rank_findings(list(findings)) == rank_findings(list(findings))


def test_ranking_an_already_sorted_list_is_a_no_op() -> None:
    # The rule engine already severity-sorts its own output; re-sorting it here must
    # not perturb an order that was already correct.
    findings = [
        _finding(query_id="q1", severity=Severity.CRITICAL),
        _finding(query_id="q2", severity=Severity.HIGH),
        _finding(query_id="q3", severity=Severity.MEDIUM),
    ]

    assert rank_findings(findings) == findings


# --- Capping -------------------------------------------------------------------


def test_capping_under_the_limit_changes_nothing() -> None:
    findings = [_finding(query_id=f"q{i}") for i in range(5)]

    kept, omitted = cap_findings(findings, max_findings=20)

    assert kept == findings
    assert omitted == 0


def test_capping_over_the_limit_keeps_only_the_highest_priority_prefix() -> None:
    findings = [_finding(query_id=f"q{i}") for i in range(25)]

    kept, omitted = cap_findings(findings, max_findings=20)

    assert kept == findings[:20]
    assert omitted == 5


def test_capping_uses_the_documented_default_when_not_given() -> None:
    findings = [_finding(query_id=f"q{i}") for i in range(DEFAULT_MAX_FINDINGS + 3)]

    kept, omitted = cap_findings(findings)

    assert len(kept) == DEFAULT_MAX_FINDINGS
    assert omitted == 3


def test_capping_is_configurable_per_call() -> None:
    findings = [_finding(query_id=f"q{i}") for i in range(10)]

    kept, omitted = cap_findings(findings, max_findings=3)

    assert len(kept) == 3
    assert omitted == 7


def test_a_negative_cap_is_rejected() -> None:
    with pytest.raises(ValueError, match="must not be negative"):
        cap_findings([_finding()], max_findings=-1)


def test_capping_drops_exactly_what_ranking_put_last() -> None:
    # The cap must never reach past a higher-severity finding to drop a lower one.
    findings = rank_findings(
        [
            _finding(query_id="q1", severity=Severity.LOW),
            _finding(query_id="q2", severity=Severity.CRITICAL),
            _finding(query_id="q3", severity=Severity.MEDIUM),
        ]
    )

    kept, omitted = cap_findings(findings, max_findings=2)

    assert [finding.query_id for finding in kept] == ["q2", "q3"]
    assert omitted == 1
