"""RuleEngine — parsing, aggregation, ranking, failure isolation, log hygiene."""

from __future__ import annotations

import logging

import pytest

from queryfix.models.finding import Finding, Severity
from queryfix.models.query import ExtractedQuery, Provenance, QueryKind
from queryfix.pipeline.extract.derived import parse_derived_method
from queryfix.pipeline.static_rules import (
    RULES,
    RuleContext,
    RuleEngine,
    register,
    run_static_rules,
)
from queryfix.pipeline.static_rules.engine import (
    _INFERRED_CONFIDENCE,
    _CommandFallbackFilter,
    silence_sqlglot_fallback_warnings,
)


def _query(sql: str, query_id: str = "q1", **kwargs: object) -> ExtractedQuery:
    return ExtractedQuery(
        id=query_id,
        kind=QueryKind.RAW_SQL,
        text=sql,
        provenance=Provenance(file="Repo.java", line=1),
        **kwargs,  # type: ignore[arg-type]
    )


class _ExplodingRule:
    rule_id = "exploding"

    def check(self, context: RuleContext) -> list[Finding]:
        raise ValueError("deliberate failure")


class _QuietRule:
    rule_id = "quiet"

    def check(self, context: RuleContext) -> list[Finding]:
        return []


def test_registry_holds_every_implemented_rule() -> None:
    assert sorted(rule.rule_id for rule in RULES) == [
        "missing-where",
        "no-limit",
        "non-sargable",
        "select-star",
        "unindexed-filter",
    ]


def test_aggregates_findings_across_rules_and_queries() -> None:
    findings = RuleEngine().analyze(
        [_query("SELECT * FROM orders", "q1"), _query("DELETE FROM customers", "q2")]
    )

    by_query = {finding.query_id for finding in findings}
    assert by_query == {"q1", "q2"}
    assert {finding.rule_id for finding in findings} >= {
        "select-star",
        "no-limit",
        "missing-where",
    }


def test_ranks_findings_worst_first() -> None:
    findings = RuleEngine().analyze(
        [_query("SELECT * FROM orders", "q1"), _query("DELETE FROM customers", "q2")]
    )

    severities = [finding.severity for finding in findings]
    assert severities[0] is Severity.CRITICAL
    assert severities == sorted(
        severities,
        key=[
            Severity.CRITICAL,
            Severity.HIGH,
            Severity.MEDIUM,
            Severity.LOW,
            Severity.INFO,
        ].index,
    )


def test_skips_queries_the_extract_stage_already_failed_on() -> None:
    # Re-reporting a parse failure as a rule finding would double-count one problem.
    findings = RuleEngine().analyze(
        [_query("SELECT FROM WHERE", "q1", parse_error="unexpected token")]
    )

    assert findings == []


def test_skips_unparseable_queries_without_raising() -> None:
    findings = RuleEngine().analyze([_query("this is not sql at all !!", "q1")])

    assert findings == []


def test_skips_opaque_command_statements() -> None:
    # sqlglot parses these to a Command node with no structure to inspect. Applying
    # rules to the raw text instead would mean regexing SQL.
    findings = RuleEngine().analyze([_query("SHOW search_path", "q1")])

    assert findings == []


def test_one_failing_rule_does_not_lose_the_others(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from queryfix.pipeline.static_rules import SelectStarRule

    engine = RuleEngine(rules=[_ExplodingRule(), SelectStarRule()])

    with caplog.at_level(logging.ERROR):
        findings = engine.analyze([_query("SELECT * FROM orders")])

    assert [finding.rule_id for finding in findings] == ["select-star"]
    assert "exploding" in caplog.text


def test_failing_rule_logs_the_query_it_failed_on(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.ERROR):
        RuleEngine(rules=[_ExplodingRule()]).analyze([_query("SELECT 1", "q-specific")])

    assert "q-specific" in caplog.text


def test_explicit_rule_list_overrides_the_registry() -> None:
    assert RuleEngine(rules=[_QuietRule()]).analyze([_query("SELECT * FROM orders")]) == []


def test_run_static_rules_is_the_stage_entry_point() -> None:
    findings = run_static_rules([_query("SELECT * FROM orders")])

    assert {finding.rule_id for finding in findings} == {"select-star", "no-limit"}


def test_sqlglot_command_fallback_warning_is_filtered() -> None:
    # The filter is asserted directly as well as end to end: it is installed on a
    # process-global logger, so once any engine exists the end-to-end assertion can
    # no longer distinguish "filtered" from "never logged".
    fallback = logging.LogRecord(
        name="sqlglot",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="'SHOW search_path' contains unsupported syntax. Falling back to parsing as a 'Command'.",
        args=(),
        exc_info=None,
    )
    unrelated = logging.LogRecord(
        name="sqlglot",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="something a maintainer would actually want to see",
        args=(),
        exc_info=None,
    )
    log_filter = _CommandFallbackFilter()

    assert log_filter.filter(fallback) is False
    assert log_filter.filter(unrelated) is True


def test_engine_suppresses_the_warning_in_practice(
    caplog: pytest.LogCaptureFixture,
) -> None:
    engine = RuleEngine()

    with caplog.at_level(logging.WARNING, logger="sqlglot"):
        engine.analyze([_query("SHOW search_path", "q1"), _query("SET ROLE 'x'", "q2")])

    assert "unsupported syntax" not in caplog.text


def test_installing_the_filter_twice_does_not_stack_it() -> None:
    silence_sqlglot_fallback_warnings()
    silence_sqlglot_fallback_warnings()
    RuleEngine()
    RuleEngine()

    installed = [
        existing
        for existing in logging.getLogger("sqlglot").filters
        if isinstance(existing, _CommandFallbackFilter)
    ]
    assert len(installed) == 1


def test_registering_two_rules_with_the_same_id_is_rejected() -> None:
    # Registration is an import side effect, so the failure this closes is a rule
    # reachable by two import paths. Its only symptom would have been every one
    # of its findings appearing twice in the PR comment.
    class Duplicate:
        rule_id = "select-star"

        def check(self, context: RuleContext) -> list[Finding]:
            return []

    before = list(RULES)

    with pytest.raises(ValueError, match="already registered"):
        register(Duplicate())

    assert before == RULES


def test_findings_against_a_derived_method_are_marked_unverified() -> None:
    # `findByCustomerId` on a `@ManyToOne` compiles to a JOIN Spring Data issues and
    # this renderer cannot see (CLAUDE.md's caution). Whatever a rule says about the
    # rendered pseudo-SQL is a claim about SQL the application may not run, so the
    # engine marks it unverified.
    class _AlwaysFires:
        rule_id = "always"

        def check(self, context: RuleContext) -> list[Finding]:
            return [
                context.finding(
                    rule_id="always", severity=Severity.LOW, title="t", explanation="e", impact="i"
                )
            ]

    derived = parse_derived_method("findByCustomerId", entity="OrderRepository", path="Repo.java")
    assert derived is not None

    findings = RuleEngine(rules=[_AlwaysFires()]).analyze([derived])

    assert [finding.rule_id for finding in findings] == ["always"]
    assert findings[0].confidence == _INFERRED_CONFIDENCE


def test_select_star_stays_silent_on_a_derived_method() -> None:
    # The `*` in a derived method's pseudo-SQL is rendered by QueryFix, not written by
    # the author, and the framework loads the whole entity by contract. A pilot on a real
    # codebase showed this was 79 of 88 select-star findings: all noise.
    derived = parse_derived_method("findByCustomerId", entity="OrderRepository", path="Repo.java")
    assert derived is not None

    assert RuleEngine().analyze([derived]) == []


def test_findings_against_real_sql_are_not_marked_unverified() -> None:
    findings = RuleEngine().analyze([_query("SELECT * FROM orders")])

    assert findings[0].confidence is None


def test_a_rules_own_confidence_on_a_derived_method_is_not_overridden() -> None:
    class _ConfidentRule:
        rule_id = "confident"

        def check(self, context: RuleContext) -> list[Finding]:
            return [
                Finding(
                    rule_id="confident",
                    severity=Severity.LOW,
                    title="t",
                    explanation="e",
                    impact="i",
                    provenance=Provenance(file="Repo.java", line=1),
                    confidence=0.9,
                )
            ]

    derived = parse_derived_method("findByCustomerId", entity="OrderRepository", path="Repo.java")
    assert derived is not None

    findings = RuleEngine(rules=[_ConfidentRule()]).analyze([derived])

    assert findings[0].confidence == 0.9


def test_a_rule_with_a_new_id_still_registers() -> None:
    class Fresh:
        rule_id = "test-only-fresh-rule"

        def check(self, context: RuleContext) -> list[Finding]:
            return []

    rule = Fresh()
    try:
        assert register(rule) is rule
        assert rule in RULES
    finally:
        RULES.remove(rule)
