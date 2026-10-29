"""Tests for the API dependency layer's Liquibase wiring.

These drive the runner through its public :meth:`AnalysisRunner.run` — the
same entry point the ``/analyze`` route and the CLI use — rather than reaching
into its private engine, so a pass here means the wiring genuinely works end
to end: config -> loader -> ``StaticSchemaProvider`` -> ``RuleEngine`` ->
``AnalysisRunner``, not just that the pieces work in isolation.

``get_analysis_runner`` is process-cached (see its docstring), so these tests
own clearing that cache themselves rather than relying on any other test's
cleanup — a leaked runner from an earlier test would silently make these pass
or fail for the wrong reason.
"""

from __future__ import annotations

from pathlib import Path

from queryfix.api.deps import get_analysis_runner
from queryfix.config import override_settings
from queryfix.models.query import SqlSource

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "liquibase"

# The real native query from SettlementTrackingRepository.deleteByPaymentId,
# audited in a real Spring Boot service. Given as a .sql source (routed to
# SqlExtractor) rather than reproducing the surrounding Java/@Query annotation
# syntax — extraction correctness is covered elsewhere; this file is only
# proving the config -> loader -> schema -> engine -> runner wiring.
_SOURCE = SqlSource(
    path="SettlementTrackingRepository.sql",
    content="DELETE FROM settlement_tracking WHERE payment_id = :paymentId",
)


def teardown_function() -> None:
    get_analysis_runner.cache_clear()


def test_runner_stays_silent_with_no_changelog_configured() -> None:
    get_analysis_runner.cache_clear()

    with override_settings(liquibase_changelog_path=None):
        runner = get_analysis_runner()
        report = runner.run(repo="acme/payments-service", pr_number=1, sources=[_SOURCE])

    assert report.findings == []


def test_runner_loads_the_configured_changelog_and_flags_the_unindexed_query() -> None:
    get_analysis_runner.cache_clear()

    with override_settings(
        liquibase_changelog_path=str(FIXTURES / "settlement_tracking_without_index.xml")
    ):
        runner = get_analysis_runner()
        report = runner.run(repo="acme/payments-service", pr_number=1, sources=[_SOURCE])

    assert len(report.findings) == 1
    assert report.findings[0].rule_id == "unindexed-filter"


def test_runner_loads_the_configured_changelog_and_stays_silent_when_indexed() -> None:
    get_analysis_runner.cache_clear()

    with override_settings(
        liquibase_changelog_path=str(FIXTURES / "settlement_tracking_with_index.xml")
    ):
        runner = get_analysis_runner()
        report = runner.run(repo="acme/payments-service", pr_number=1, sources=[_SOURCE])

    assert report.findings == []
