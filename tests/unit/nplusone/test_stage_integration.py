"""The stage inside the pipeline: wiring, fail-soft, invariants, and cost.

Everything here is about the detector as a *stage* rather than as an algorithm —
that it reaches the report, that it cannot take a run down with it, that unrelated
edits do not perturb it, and that it stays cheap enough for CI.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from queryfix.models.finding import Finding, Severity
from queryfix.models.query import SourceFile, SqlSource
from queryfix.pipeline import nplusone as nplusone_module
from queryfix.pipeline.nplusone import LAZY_ASSOCIATION_RULE_ID, REPOSITORY_CALL_RULE_ID
from queryfix.pipeline.report import render_markdown
from queryfix.pipeline.runner import NPLUS_ONE_STAGE, STATIC_RULES_STAGE, AnalysisRunner

Build = Callable[..., SourceFile]
Candidates = Callable[..., list[object]]

SANDBOX = Path("queryfix-sandbox/src/main/java/com/queryfix/sandbox")

LOOP = """
        for (Parent parent : parents) {
            thingRepository.findByParentId(parent.getId());
        }
"""
DERIVED = "List<Thing> findByParentId(Long parentId);"


def nplusone_findings(findings: list[Finding]) -> list[Finding]:
    return [
        finding
        for finding in findings
        if finding.rule_id in {REPOSITORY_CALL_RULE_ID, LAZY_ASSOCIATION_RULE_ID}
    ]


# --- Wiring --------------------------------------------------------------------


def test_the_runner_reports_n_plus_one_findings(service: Build, repository: Build) -> None:
    report = AnalysisRunner().run(
        repo="acme/example",
        pr_number=1,
        sources=[service(LOOP), repository(DERIVED)],
    )

    (finding,) = nplusone_findings(report.findings)
    assert finding.severity is Severity.HIGH
    assert report.degraded_stages == []


def test_the_finding_reaches_the_rendered_comment(service: Build, repository: Build) -> None:
    report = AnalysisRunner().run(
        repo="acme/example",
        pr_number=1,
        sources=[service(LOOP), repository(DERIVED)],
    )

    markdown = render_markdown(report)

    assert "Potential N+1 query pattern" in markdown
    assert REPOSITORY_CALL_RULE_ID in markdown
    # The anchor names the calling method, and the evidence names the repository call
    # that sits inside the loop: this is what a reviewer acts on.
    assert "ExampleService.java:15" in markdown
    assert "ThingRepository.findByParentId" in markdown


def test_a_sql_only_run_is_unaffected() -> None:
    """No Java, no structural analysis, no change to the existing behaviour."""
    report = AnalysisRunner().run(
        repo="acme/example",
        pr_number=1,
        sources=[SqlSource(path="m.sql", content="SELECT * FROM orders")],
    )

    assert nplusone_findings(report.findings) == []
    assert report.degraded_stages == []
    assert [finding.rule_id for finding in report.findings] == ["no-limit", "select-star"]


# --- Fail-soft -----------------------------------------------------------------


def test_a_crashing_stage_degrades_without_losing_static_findings(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def explode(*args: object, **kwargs: object) -> list[Finding]:
        raise RuntimeError("structural analysis blew up")

    monkeypatch.setattr(nplusone_module, "detect_n_plus_one", explode)

    with caplog.at_level(logging.ERROR):
        report = AnalysisRunner().run(
            repo="acme/example",
            pr_number=1,
            sources=[SqlSource(path="m.sql", content="SELECT * FROM orders")],
        )

    assert NPLUS_ONE_STAGE in report.degraded_stages
    assert STATIC_RULES_STAGE not in report.degraded_stages
    # The static findings survive the crash entirely.
    assert [finding.rule_id for finding in report.findings] == ["no-limit", "select-star"]


def test_a_malformed_java_file_does_not_take_the_run_down(service: Build) -> None:
    broken = SourceFile(
        path="src/main/java/com/example/Broken.java",
        content='public class Broken { void run() { for (;;) { "unterminated',
    )

    report = AnalysisRunner().run(repo="acme/example", pr_number=1, sources=[broken, service(LOOP)])

    assert NPLUS_ONE_STAGE not in report.degraded_stages
    assert nplusone_findings(report.findings)


def test_an_empty_source_list_is_quiet() -> None:
    report = AnalysisRunner().run(repo="acme/example", pr_number=1, sources=[])

    assert report.findings == []
    assert report.degraded_stages == []


# --- Invariants ----------------------------------------------------------------


def test_unrelated_java_does_not_change_the_findings(
    candidates: Candidates, service: Build, repository: Build
) -> None:
    baseline = candidates(service(LOOP), repository(DERIVED))
    noise = SourceFile(
        path="src/main/java/com/example/util/Unrelated.java",
        content=(
            "package com.example.util;\n"
            "public final class Unrelated {\n"
            "    public int total(List<Integer> values) {\n"
            "        int sum = 0;\n"
            "        for (Integer value : values) { sum += value; }\n"
            "        return sum;\n"
            "    }\n"
            "}\n"
        ),
    )

    with_noise = candidates(service(LOOP), repository(DERIVED), noise)

    assert baseline == with_noise


def test_reordering_the_sources_does_not_change_the_findings(
    candidates: Candidates, service: Build, repository: Build
) -> None:
    forward = candidates(service(LOOP), repository(DERIVED))
    reversed_order = candidates(repository(DERIVED), service(LOOP))

    assert forward == reversed_order


def test_an_extra_repository_does_not_perturb_an_existing_finding(
    candidates: Candidates, service: Build, repository: Build
) -> None:
    baseline = candidates(service(LOOP), repository(DERIVED))
    extended = candidates(
        service(LOOP),
        repository(DERIVED),
        repository("List<Other> findByOtherId(Long id);", name="OtherRepository"),
    )

    assert baseline == extended


def test_the_same_source_twice_does_not_double_report(
    candidates: Candidates, service: Build, repository: Build
) -> None:
    once = candidates(service(LOOP), repository(DERIVED))
    twice = candidates(service(LOOP), repository(DERIVED), service(LOOP), repository(DERIVED))

    assert len(once) == len(twice) == 1


def test_detection_is_deterministic(
    candidates: Candidates, service: Build, repository: Build
) -> None:
    """The comment is rewritten in place, so identical input must not churn."""
    runs = [candidates(service(LOOP), repository(DERIVED)) for _ in range(5)]

    assert all(run == runs[0] for run in runs)


def test_findings_serialize_losslessly(service: Build, repository: Build) -> None:
    report = AnalysisRunner().run(
        repo="acme/example",
        pr_number=1,
        sources=[service(LOOP), repository(DERIVED)],
    )

    from queryfix.models.report import Report

    assert Report.model_validate_json(report.model_dump_json()) == report


# --- Performance ---------------------------------------------------------------


def test_a_large_pull_request_stays_cheap() -> None:
    """CI budget. Indexes are built once; files are not rescanned per query."""
    sources: list[SourceFile] = []
    for index in range(60):
        methods = "\n".join(
            f"    public void run{n}(List<Parent> parents) {{\n"
            f"        for (Parent parent : parents) {{\n"
            f"            repo{index}.findByParentId(parent.getId());\n"
            f"        }}\n"
            f"    }}"
            for n in range(15)
        )
        sources.append(
            SourceFile(
                path=f"src/main/java/com/example/service/Service{index}.java",
                content=(
                    f"package com.example.service;\n"
                    f"import com.example.data.Repo{index};\n"
                    f"public class Service{index} {{\n"
                    f"    private final Repo{index} repo{index};\n"
                    f"{methods}\n"
                    f"}}\n"
                ),
            )
        )
        sources.append(
            SourceFile(
                path=f"src/main/java/com/example/data/Repo{index}.java",
                content=(
                    f"package com.example.data;\n"
                    f"public interface Repo{index} extends JpaRepository<Thing{index}, Long> {{\n"
                    f"    List<Thing{index}> findByParentId(Long id);\n"
                    f"}}\n"
                ),
            )
        )

    started = time.perf_counter()
    findings = nplusone_module.detect_n_plus_one(sources)
    elapsed = time.perf_counter() - started

    assert len(findings) == 60 * 15
    # Generous: the point is to catch a quadratic regression, not to benchmark.
    assert elapsed < 10.0, f"structural analysis took {elapsed:.2f}s"


# --- Validation against the checked-in sandbox ---------------------------------


@pytest.mark.skipif(not SANDBOX.exists(), reason="sandbox sources are not checked out")
def test_the_sandbox_planted_n_plus_one_is_detected() -> None:
    """The fixture app's planted bug, analyzed as a pull request would be.

    The sandbox is a *validation* target, not a design input: the detector was
    written against generic Java and is pointed at this afterwards. It is the one
    place a real Spring Boot service, entity, and repository are exercised
    together.
    """
    sources = [
        SourceFile(path=str(path).replace("\\", "/"), content=path.read_text(encoding="utf-8"))
        for path in (
            SANDBOX / "service" / "ReportingService.java",
            SANDBOX / "repository" / "OrderRepository.java",
            SANDBOX / "repository" / "CustomerRepository.java",
            SANDBOX / "domain" / "Customer.java",
            SANDBOX / "domain" / "Order.java",
        )
    ]

    findings = nplusone_module.detect_n_plus_one(sources)
    repository_calls = [
        finding for finding in findings if finding.rule_id == REPOSITORY_CALL_RULE_ID
    ]

    # The planted bug: `orderRepository.findByCustomerId` inside a for-each over
    # the result of `customerRepository.findAll()`.
    assert len(repository_calls) == 1
    (finding,) = repository_calls
    assert finding.provenance.file.endswith("ReportingService.java")
    assert finding.provenance.symbol == "buildCustomerOrderSummary"
    detail = " ".join(item.detail for item in finding.evidence)
    assert "OrderRepository.findByCustomerId" in detail
    assert "customer" in detail


@pytest.mark.skipif(not SANDBOX.exists(), reason="sandbox sources are not checked out")
def test_the_sandbox_healthy_counterpart_is_not_flagged() -> None:
    """`buildCustomerOrderSummaryBatched` is the false-positive guard.

    It loops over customers and issues no query inside the loop — the exact
    rewrite this detector recommends. Flagging it would make the suggestion
    self-defeating.
    """
    sources = [
        SourceFile(path=str(path).replace("\\", "/"), content=path.read_text(encoding="utf-8"))
        for path in (
            SANDBOX / "service" / "ReportingService.java",
            SANDBOX / "repository" / "OrderRepository.java",
        )
    ]

    findings = nplusone_module.detect_n_plus_one(sources)

    assert all(
        finding.provenance.symbol != "buildCustomerOrderSummaryBatched" for finding in findings
    )


# --- Scoped to what the pull request changed ---------------------------------------
# LOOP opens at line 14 and the repository call is on line 15.


def _scoped(service: Build, repository: Build, *, changed_line: int):  # type: ignore[no-untyped-def]
    from queryfix.models.query import Hunk

    hunk = Hunk(base_start=changed_line, base_lines=0, head_start=changed_line, head_lines=1)
    return AnalysisRunner().run_review(
        repo="acme/example",
        pr_number=1,
        sources=[service(LOOP).model_copy(update={"hunks": [hunk]}), repository(DERIVED)],
    )


def test_a_pr_that_adds_the_loop_owns_the_finding(service: Build, repository: Build) -> None:
    # The call on line 15 is old; the PR wrapped it in a loop opened on line 14.
    result = _scoped(service, repository, changed_line=14)

    assert len(nplusone_findings(result.findings)) == 1
    assert result.report.unchanged_findings == 0


def test_a_pr_that_edits_the_call_owns_the_finding(service: Build, repository: Build) -> None:
    result = _scoped(service, repository, changed_line=15)

    assert len(nplusone_findings(result.findings)) == 1


def test_a_pr_that_edits_elsewhere_in_the_file_does_not(service: Build, repository: Build) -> None:
    result = _scoped(service, repository, changed_line=40)

    assert nplusone_findings(result.findings) == []
    assert result.report.unchanged_findings == 1


LONG_LOOP = """
        for (Parent parent : parents) {
            audit(parent);
            audit(parent);
            thingRepository.findByParentId(parent.getId());
        }
"""


def test_an_edit_inside_the_loop_body_does_not_make_the_pr_own_the_pattern(
    service: Build, repository: Build
) -> None:
    from queryfix.models.query import Hunk

    # The loop opens on line 14 and the call is on line 17. The PR touched line 15: an
    # unrelated statement in between. It wrote neither the loop nor the call.
    hunk = Hunk(base_start=15, base_lines=0, head_start=15, head_lines=1)
    source = service(LONG_LOOP).model_copy(update={"hunks": [hunk]})

    result = AnalysisRunner().run_review(
        repo="acme/example", pr_number=1, sources=[source, repository(DERIVED)]
    )

    assert nplusone_findings(result.findings) == []
    assert result.report.unchanged_findings == 1


# --- Evidence strength is explained, not a bare number -----------------------------


def test_an_n_plus_one_says_why_it_is_credible_instead_of_printing_a_percentage(
    service: Build, repository: Build
) -> None:
    result = AnalysisRunner().run_review(
        repo="acme/example", pr_number=1, sources=[service(LOOP), repository(DERIVED)]
    )
    (finding,) = nplusone_findings(result.findings)

    assert finding.confidence_basis is not None
    assert "`parent` feeds the call's arguments" in finding.confidence_basis
    markdown = render_markdown(result.report)
    assert "Evidence strength: strong, inferred — the loop variable `parent`" in markdown
    assert "75%" not in markdown


def test_a_call_that_does_not_depend_on_the_loop_is_described_differently(
    service: Build, repository: Build
) -> None:
    independent = """
        for (Parent parent : parents) {
            thingRepository.findByParentId(1L);
        }
"""
    result = AnalysisRunner().run_review(
        repo="acme/example", pr_number=1, sources=[service(independent), repository(DERIVED)]
    )
    (finding,) = nplusone_findings(result.findings)

    assert finding.confidence_basis is not None
    assert "do not depend on the loop element" in finding.confidence_basis
    assert "Evidence strength: moderate" in render_markdown(result.report)
