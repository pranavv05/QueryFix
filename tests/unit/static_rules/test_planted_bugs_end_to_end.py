"""Cross-stage test: sandbox planted bug -> extract -> RuleEngine -> Findings.

This is the first test that runs two pipeline stages together, and the first to
assert QueryFix actually catches what the sandbox was built to trip.

It lives under ``unit/`` rather than ``integration/`` on purpose: in this repo
``integration`` means "requires Docker" (see the marker in ``pytest.ini``), and this
path needs no database — the whole point of the static stage is that it runs before
anything is provisioned.

The SQL is not retyped from memory. Each string is asserted to appear verbatim in
the sandbox source it came from, so if a planted bug is edited or "fixed", this test
fails rather than silently testing SQL the sandbox no longer contains.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from queryfix.models.finding import Severity
from queryfix.pipeline.extract import extract_from_sql
from queryfix.pipeline.static_rules import RuleEngine

REPO_ROOT = Path(__file__).resolve().parents[3]
SANDBOX_REPOSITORIES = REPO_ROOT / (
    "queryfix-sandbox/src/main/java/com/queryfix/sandbox/repository"
)

# Planted bug 1: OrderRepository.exportAllOrders, a native query.
SELECT_STAR_NO_WHERE = "SELECT * FROM orders"
SELECT_STAR_SOURCE = SANDBOX_REPOSITORIES / "OrderRepository.java"

# Planted bug 4: CustomerRepository.promoteAllToTier, JPQL.
UPDATE_NO_WHERE = "UPDATE Customer c SET c.loyaltyTier = :tier"
UPDATE_SOURCE = SANDBOX_REPOSITORIES / "CustomerRepository.java"


@pytest.mark.parametrize(
    ("sql", "source"),
    [
        pytest.param(SELECT_STAR_NO_WHERE, SELECT_STAR_SOURCE, id="select-star-no-where"),
        pytest.param(UPDATE_NO_WHERE, UPDATE_SOURCE, id="update-no-where"),
    ],
)
def test_fixture_sql_still_matches_the_sandbox_source(sql: str, source: Path) -> None:
    """The SQL under test is the SQL the sandbox actually contains."""
    assert source.is_file(), f"sandbox source moved: {source}"
    assert sql in source.read_text(encoding="utf-8"), (
        f"{sql!r} is no longer present in {source.name} — either the planted bug was "
        f"changed or this test is now asserting against SQL that does not exist."
    )


def test_select_star_with_no_where_is_flagged_end_to_end() -> None:
    queries = extract_from_sql(SELECT_STAR_SOURCE.name, SELECT_STAR_NO_WHERE)
    findings = RuleEngine().analyze(queries)

    by_rule = {finding.rule_id: finding for finding in findings}

    # Two distinct problems in one query, with different fixes: stop projecting every
    # column, and bound the result set.
    assert by_rule["select-star"].severity is Severity.MEDIUM
    assert by_rule["no-limit"].severity is Severity.HIGH

    # Not a write, so the critical write rule must stay out of it.
    assert "missing-where" not in by_rule


def test_update_with_no_where_is_flagged_critical_end_to_end() -> None:
    queries = extract_from_sql(UPDATE_SOURCE.name, UPDATE_NO_WHERE)
    findings = RuleEngine().analyze(queries)

    by_rule = {finding.rule_id: finding for finding in findings}

    assert by_rule["missing-where"].severity is Severity.CRITICAL
    assert "customer" in by_rule["missing-where"].explanation.lower()

    # A write has no projection and is not a result-set-size problem.
    assert "select-star" not in by_rule
    assert "no-limit" not in by_rule


def test_both_planted_bugs_in_one_run_rank_the_write_first() -> None:
    queries = extract_from_sql("OrderRepository.java", SELECT_STAR_NO_WHERE)
    queries += extract_from_sql("CustomerRepository.java", UPDATE_NO_WHERE)

    findings = RuleEngine().analyze(queries)

    assert findings, "expected findings from both planted bugs"
    # Data loss outranks slowness: the unqualified UPDATE has to be the first thing a
    # reviewer sees in the report.
    assert findings[0].rule_id == "missing-where"
    assert findings[0].severity is Severity.CRITICAL


def test_every_finding_carries_provenance_and_an_actionable_fix() -> None:
    queries = extract_from_sql("OrderRepository.java", SELECT_STAR_NO_WHERE)
    queries += extract_from_sql("CustomerRepository.java", UPDATE_NO_WHERE)

    findings = RuleEngine().analyze(queries)

    for finding in findings:
        assert finding.provenance.file.endswith(".java")
        assert finding.provenance.line is not None
        assert finding.query_id
        # A finding that says what it matched but not why it matters, or offers no
        # fix, is noise in a PR comment.
        assert finding.impact and finding.impact != finding.explanation
        assert finding.suggestions and finding.suggestions[0].description


def test_the_healthy_counterparts_stay_silent() -> None:
    """The false-positive guard, at the pipeline level rather than per rule.

    These are the sandbox's deliberately healthy queries. If the static stage
    reports anything here, QueryFix is a tool a team turns off.
    """
    healthy = [
        # OrderRepository.exportRecentOrders — named columns, indexed predicate.
        "SELECT id, order_number, status, total_amount FROM orders WHERE placed_at >= :since",
        # CustomerRepository.promoteHighValueCustomers — the scoped write.
        "UPDATE Customer c SET c.loyaltyTier = :tier WHERE c.lifetimeValue >= :threshold",
    ]

    queries: list[object] = []
    for index, sql in enumerate(healthy):
        queries += extract_from_sql(f"Healthy{index}.java", sql)

    findings = RuleEngine().analyze(queries)  # type: ignore[arg-type]

    assert findings == [], f"false positives on healthy queries: {findings}"
