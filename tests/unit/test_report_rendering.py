"""Snapshot tests for the PR comment body.

The comment format is user-facing, so it is pinned byte for byte: a change to it
is a change a reviewer sees on every pull request, and it should have to be
approved deliberately rather than arriving as a side effect.

The reports here are built by hand rather than driven through the rule engine.
Snapshotting real rule output would mean that editing one rule's explanation
breaks the renderer's snapshot, which tests nothing about rendering — the rule
suites already pin their own text.

To regenerate after an intended format change::

    python -m tests.unit.test_report_rendering
"""

from __future__ import annotations

from pathlib import Path

import pytest

from queryfix.integrations.github import COMMENT_MARKER
from queryfix.models import (
    Evidence,
    ExtractedQuery,
    Finding,
    Provenance,
    QueryKind,
    Report,
    RunContext,
    Severity,
    Suggestion,
)
from queryfix.pipeline.report import render_markdown

SNAPSHOTS = Path(__file__).resolve().parents[1] / "fixtures" / "reports"


def context() -> RunContext:
    return RunContext(run_id="fixed-run-id", repo="acme/billing-service", pr_number=42)


def select_star_query() -> ExtractedQuery:
    return ExtractedQuery(
        id="migrations/001_orders.sql:1",
        kind=QueryKind.RAW_SQL,
        text="SELECT * FROM orders",
        provenance=Provenance(file="migrations/001_orders.sql", line=12),
    )


def unqualified_update() -> ExtractedQuery:
    return ExtractedQuery(
        id="migrations/003_customers.sql:1",
        kind=QueryKind.RAW_SQL,
        text="UPDATE customers\nSET loyalty_tier = 'gold'",
        provenance=Provenance(file="migrations/003_customers.sql", line=4),
    )


def unparseable_query() -> ExtractedQuery:
    return ExtractedQuery(
        id="migrations/002_broken.sql:1",
        kind=QueryKind.RAW_SQL,
        text="SELECT FROM WHERE",
        provenance=Provenance(file="migrations/002_broken.sql", line=1),
        parse_error="Invalid expression / Unexpected token.\nLine 1, Col: 10.\n  SELECT FROM WHERE",
    )


def missing_where_finding() -> Finding:
    return Finding(
        rule_id="missing-where",
        severity=Severity.CRITICAL,
        title="`UPDATE` rewrites every row: no `WHERE` clause",
        explanation=(
            "This UPDATE has no WHERE clause, so it matches every row in `customers` "
            "rather than a subset."
        ),
        impact=(
            "Every row in the table is rewritten, and the previous values are gone. On a "
            "table this size that is both a long write and an unrecoverable one without a "
            "restore."
        ),
        provenance=Provenance(file="migrations/003_customers.sql", line=4),
        query_id="migrations/003_customers.sql:1",
        suggestions=[
            Suggestion(
                description=(
                    "Scope the write to the rows it is meant to touch. If it really is "
                    "every row, say so explicitly in the migration's comment so the next "
                    "reader does not have to guess."
                ),
                sql="UPDATE customers SET loyalty_tier = 'gold' WHERE lifetime_value >= 1000;",
            )
        ],
    )


def select_star_finding() -> Finding:
    return Finding(
        rule_id="select-star",
        severity=Severity.MEDIUM,
        title="Query selects every column with `SELECT *`",
        explanation=(
            "The projection is `*`, so the query returns every column of every row it "
            "matches, including ones the caller never reads."
        ),
        impact=(
            "Every added column silently widens this query's result set, so a schema "
            "change elsewhere makes it slower with no change to this file."
        ),
        provenance=Provenance(file="migrations/001_orders.sql", line=12),
        query_id="migrations/001_orders.sql:1",
        suggestions=[Suggestion(description="Name the columns the caller actually uses.")],
    )


# --- The four required cases, plus one for the optional finding fields ---------


def empty_report() -> Report:
    return Report(context=context())


def findings_only_report() -> Report:
    return Report(
        context=context(),
        queries=[unqualified_update(), select_star_query()],
        findings=[missing_where_finding(), select_star_finding()],
    )


def degraded_only_report() -> Report:
    return Report(
        context=context(),
        queries=[unparseable_query()],
        degraded_stages=["extract:migrations/002_broken.sql"],
    )


def findings_and_degraded_report() -> Report:
    return Report(
        context=context(),
        queries=[unqualified_update(), unparseable_query(), select_star_query()],
        findings=[missing_where_finding(), select_star_finding()],
        degraded_stages=["extract:migrations/002_broken.sql", "static_rules"],
    )


def enriched_report() -> Report:
    """Every optional field a later stage will populate, exercised at once.

    Evidence, confidence, cost deltas, a symbol-anchored provenance, and a
    cross-query finding have no producers yet — stages 5 to 7 are unimplemented.
    They are rendered and pinned here so that when those stages land, the comment
    format is already decided and already tested rather than being invented under
    deadline.
    """
    derived = ExtractedQuery(
        id="src/OrderRepository.java:findByCustomerId",
        kind=QueryKind.SPRING_DATA_DERIVED,
        text="SELECT *\nFROM orders\nWHERE customer_id = ?",
        provenance=Provenance(file="src/OrderRepository.java", line=31, symbol="findByCustomerId"),
    )
    listing = ExtractedQuery(
        id="src/ReportingService.java:1",
        kind=QueryKind.JPQL,
        text="SELECT c FROM Customer c",
        provenance=Provenance(file="src/ReportingService.java", line=48),
    )

    return Report(
        context=context(),
        queries=[listing, derived],
        findings=[
            Finding(
                rule_id="n-plus-one",
                severity=Severity.HIGH,
                title="Repository call inside a loop issues one query per row",
                explanation=(
                    "`findByCustomerId` is called for each customer returned by the "
                    "listing query, so the number of statements grows with the result "
                    "set rather than staying constant."
                ),
                impact=(
                    "At 5,000 customers this is 5,001 round trips where two would do. "
                    "Latency is dominated by the round trips, not by the work."
                ),
                provenance=Provenance(
                    file="src/ReportingService.java", line=52, symbol="buildSummaries"
                ),
                query_id="src/OrderRepository.java:findByCustomerId",
                query_ids=[
                    "src/OrderRepository.java:findByCustomerId",
                    "src/ReportingService.java:1",
                ],
                evidence=[
                    Evidence(label="Statements captured", detail="5,001 in one request"),
                    Evidence(label="Distinct bind values", detail="5,000 — not a cache miss"),
                ],
                suggestions=[
                    Suggestion(
                        description="Fetch the orders for every customer in one query.",
                        sql="SELECT * FROM orders WHERE customer_id IN (:ids);",
                    )
                ],
                confidence=0.82,
                confidence_basis="the model read the diff and no plan was available to check it",
            ),
            Finding(
                rule_id="unindexed-filter",
                severity=Severity.HIGH,
                title="Filter on `orders.customer_id`, which has no index",
                explanation="The WHERE clause filters on a column with no index leading it.",
                impact="Every execution reads the whole table, so cost tracks table size.",
                provenance=Provenance(
                    file="src/OrderRepository.java", line=31, symbol="findByCustomerId"
                ),
                query_id="src/OrderRepository.java:findByCustomerId",
                suggestions=[
                    Suggestion(
                        description="Add an index and measure it before committing.",
                        sql="CREATE INDEX idx_orders_customer_id ON orders (customer_id);",
                        cost_before=18450.25,
                        cost_after=8.31,
                    )
                ],
            ),
        ],
    )


CASES: dict[str, Report] = {
    "empty": empty_report(),
    "findings_only": findings_only_report(),
    "degraded_only": degraded_only_report(),
    "findings_and_degraded": findings_and_degraded_report(),
    "enriched": enriched_report(),
}


# --- Snapshots -----------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(CASES))
def test_rendering_matches_the_snapshot(name: str) -> None:
    expected = (SNAPSHOTS / f"{name}.md").read_text(encoding="utf-8")

    assert render_markdown(CASES[name]) == expected


@pytest.mark.parametrize("name", sorted(CASES))
def test_no_snapshot_leaks_the_string_none(name: str) -> None:
    # Every optional field is optional for a reason, and a comment reading
    # `Repo.java:None` or "Suggested fix: None" is worse than one that omits the
    # part it has nothing to say about.
    assert "None" not in (SNAPSHOTS / f"{name}.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("name", sorted(CASES))
def test_the_marker_is_present_and_first(name: str) -> None:
    # The marker is what makes the comment idempotent. If it is not the first
    # line, a reader sees it as stray text at the top of the report.
    rendered = render_markdown(CASES[name])

    assert rendered.splitlines()[0] == COMMENT_MARKER
    assert rendered.count(COMMENT_MARKER) == 1


@pytest.mark.parametrize("name", sorted(CASES))
def test_rendering_is_byte_identical_across_calls(name: str) -> None:
    # The comment is rewritten in place on every run, so an unchanged report that
    # rendered differently would show the reviewer a diff that means nothing.
    report = CASES[name]

    assert render_markdown(report) == render_markdown(report)


# --- Behaviour the snapshots imply but do not state --------------------------


def test_an_empty_report_is_a_clean_bill_of_health_not_an_empty_body() -> None:
    rendered = render_markdown(empty_report())

    assert rendered.strip() != COMMENT_MARKER
    assert "No queries were found" in rendered


def test_an_empty_report_that_is_also_degraded_does_not_read_as_clean() -> None:
    # A change with nothing to review and a pull request that could not be
    # fetched at all are both "zero queries" — but they are not the same
    # outcome, and a reviewer must not be able to mistake the second for the
    # first from the headline alone (an ingestion failure must not render as
    # "no problems found").
    report = empty_report().model_copy(update={"degraded_stages": ["ingest:GitHub returned 404"]})

    rendered = render_markdown(report)

    assert "QueryFix could not analyze this pull request" in rendered
    assert "No queries were found" not in rendered
    assert "No problems" not in rendered


def test_a_clean_report_says_how_much_it_reviewed() -> None:
    # Silence has to mean "analyzed and clean", not "never looked".
    report = Report(context=context(), queries=[select_star_query(), unqualified_update()])

    assert "Reviewed 2 queries and found no problems." in render_markdown(report)


def test_severity_sections_are_ordered_worst_first() -> None:
    rendered = render_markdown(findings_only_report())

    assert rendered.index("### 🔴 Critical") < rendered.index("### 🟡 Medium")


def test_only_severities_present_get_a_section() -> None:
    rendered = render_markdown(findings_only_report())

    assert "High" not in rendered
    assert "Low" not in rendered


def test_degraded_stages_are_a_section_not_a_footnote() -> None:
    # A silent report over a partially read change is the failure mode the
    # fail-soft design exists to prevent, so the caveat outranks the findings.
    rendered = render_markdown(findings_and_degraded_report())

    assert "### ⚠️ Not fully analyzed" in rendered
    assert rendered.index("### ⚠️ Not fully analyzed") < rendered.index("### 🔴 Critical")


def test_a_degraded_run_says_so_in_the_summary() -> None:
    rendered = render_markdown(degraded_only_report())

    assert "**Part of this change could not be reviewed — see below.**" in rendered


def test_a_run_where_nothing_was_readable_does_not_claim_a_clean_review() -> None:
    # Every query found was unparseable. "Found no problems" would be a clean
    # bill of health for a review that never happened.
    rendered = render_markdown(degraded_only_report())

    assert "Found 1 query, none of which could be reviewed." in rendered
    assert "found no problems" not in rendered


def test_a_clean_run_does_not_claim_anything_was_missed() -> None:
    assert "could not be reviewed" not in render_markdown(findings_only_report())


def test_a_qualified_degraded_marker_names_the_file_it_lost() -> None:
    rendered = render_markdown(degraded_only_report())

    assert "**extract** — `migrations/002_broken.sql`" in rendered


def test_a_bare_degraded_marker_renders_without_a_detail() -> None:
    rendered = render_markdown(findings_and_degraded_report())

    assert "- **static_rules**" in rendered


def test_unparseable_queries_are_named_rather_than_omitted() -> None:
    rendered = render_markdown(degraded_only_report())

    assert "### ⚠️ Queries that could not be parsed" in rendered
    assert "`migrations/002_broken.sql:1`" in rendered
    # The reason travels with it, collapsed onto one line so it sits in a list.
    assert "Invalid expression / Unexpected token. Line 1, Col: 10." in rendered


def test_the_summary_counts_analyzed_queries_not_extracted_ones() -> None:
    # Three queries were extracted; one could not be read. Counting it as reviewed
    # would overstate what this report covers.
    rendered = render_markdown(findings_and_degraded_report())

    assert "Reviewed 2 queries and found 2 problems." in rendered


def test_a_finding_quotes_the_query_it_was_raised_against() -> None:
    rendered = render_markdown(findings_only_report())

    assert "```sql\nUPDATE customers\nSET loyalty_tier = 'gold'\n```" in rendered


def test_a_finding_carries_its_anchor_explanation_impact_and_fix() -> None:
    rendered = render_markdown(findings_only_report())

    assert "**Where:** `migrations/003_customers.sql:4`" in rendered
    assert "This UPDATE has no WHERE clause" in rendered
    assert "**Impact:** Every row in the table is rewritten" in rendered
    assert "**Suggested fix:** Scope the write" in rendered
    assert "<sub>`missing-where`</sub>" in rendered


def test_a_symbol_anchored_finding_names_the_method() -> None:
    rendered = render_markdown(enriched_report())

    assert "`src/OrderRepository.java:31` (`findByCustomerId`)" in rendered


def test_a_cross_query_finding_names_the_other_queries_it_spans() -> None:
    rendered = render_markdown(enriched_report())

    assert "with `src/ReportingService.java:48`" in rendered


def test_an_unverified_finding_says_how_strong_the_evidence_is_and_why() -> None:
    # CLAUDE.md: findings from Claude are claims, not facts. Where a claim cannot
    # be checked against a plan or an AST, the report says so, and gives the reason.
    rendered = render_markdown(enriched_report())

    assert (
        "*Evidence strength: strong, inferred — the model read the diff and no plan was "
        "available to check it." in rendered
    )


def test_confidence_is_never_rendered_as_a_percentage() -> None:
    # The value is an ordinal per evidence tier, not a probability. Printing "75%" on
    # every finding read as calibration it does not have.
    rendered = render_markdown(enriched_report())

    assert "82%" not in rendered
    assert "Confidence:" not in rendered


def test_a_measured_index_suggestion_shows_the_cost_delta() -> None:
    rendered = render_markdown(enriched_report())

    # Rounded away from flattering the suggestion: 99.95% must not print as 100%.
    assert "**Simulated cost:** 18,450.25 → 8.31 (99% lower)" in rendered


def test_a_query_containing_a_fence_cannot_break_out_of_its_block() -> None:
    # A query built inside a Markdown-formatted string would otherwise close the
    # block early and spill the rest of the report into the page as prose.
    report = Report(
        context=context(),
        queries=[
            ExtractedQuery(
                id="a.sql:1",
                kind=QueryKind.RAW_SQL,
                text="SELECT '```' FROM t",
                provenance=Provenance(file="a.sql", line=1),
            )
        ],
        findings=[
            Finding(
                rule_id="select-star",
                severity=Severity.LOW,
                title="t",
                explanation="e",
                impact="i",
                provenance=Provenance(file="a.sql", line=1),
                query_id="a.sql:1",
            )
        ],
    )

    rendered = render_markdown(report)

    assert "````sql\nSELECT '```' FROM t\n````" in rendered


def test_the_body_carries_no_run_id_or_other_per_run_value() -> None:
    # The comment is rewritten in place; anything that changes per run would make
    # every re-run look like a new result.
    assert "fixed-run-id" not in render_markdown(findings_and_degraded_report())


def test_two_reports_differing_only_by_run_id_render_identically() -> None:
    first = findings_and_degraded_report()
    second = first.model_copy(
        update={"context": first.context.model_copy(update={"run_id": "a-different-id"})}
    )

    assert render_markdown(first) == render_markdown(second)


def test_a_finding_whose_query_is_absent_still_renders() -> None:
    # Internal consistency is asserted elsewhere; if it is ever violated the
    # renderer must degrade to omitting the quote, not raise while building the
    # only output the user sees.
    report = Report(
        context=context(),
        findings=[
            Finding(
                rule_id="select-star",
                severity=Severity.MEDIUM,
                title="t",
                explanation="e",
                impact="i",
                provenance=Provenance(file="a.sql", line=1),
                query_id="a.sql:1",
            )
        ],
    )

    rendered = render_markdown(report)

    assert "```sql" not in rendered
    assert "**Where:** `a.sql:1`" in rendered


def test_a_derived_method_finding_is_not_quoted_as_literal_sql() -> None:
    # `findByCustomerId` on a `@ManyToOne` compiles to a JOIN Spring Data issues,
    # not the bare `SELECT * FROM order WHERE customer_id = ?` this renders — see
    # CLAUDE.md's caution. A reviewer must not be able to read the fenced block
    # below as the query that actually ran.
    derived = ExtractedQuery(
        id="OrderRepository.java:findByCustomerId",
        kind=QueryKind.SPRING_DATA_DERIVED,
        text="SELECT *\nFROM order\nWHERE customer_id = ?",
        provenance=Provenance(file="OrderRepository.java", line=19, symbol="findByCustomerId"),
    )
    report = Report(
        context=context(),
        queries=[derived],
        findings=[
            Finding(
                rule_id="select-star",
                severity=Severity.MEDIUM,
                title="Query selects every column with `SELECT *`",
                explanation="e",
                impact="i",
                provenance=derived.provenance,
                query_id=derived.id,
            )
        ],
    )

    rendered = render_markdown(report)

    assert "Inferred query shape" in rendered
    assert "not the literal SQL Spring Data will issue" in rendered
    # The disclaimer precedes the fenced text it qualifies, not the other way round.
    assert rendered.index("Inferred query shape") < rendered.index("```sql")


def test_a_raw_sql_finding_carries_no_inferred_disclaimer() -> None:
    rendered = render_markdown(findings_only_report())

    assert "Inferred query shape" not in rendered


def test_a_capped_report_says_how_many_findings_it_left_out() -> None:
    # A report that only names the findings it shows would read as complete. The
    # count it found and the count it is showing must both be visible.
    report = findings_only_report().model_copy(update={"omitted_findings": 5})

    rendered = render_markdown(report)

    assert "Reviewed 2 queries and found 7 problems." in rendered
    assert "Showing the 2 highest-priority; 5 lower-priority findings are omitted." in rendered


def test_a_capped_report_with_one_omitted_finding_uses_the_singular() -> None:
    report = findings_only_report().model_copy(update={"omitted_findings": 1})

    rendered = render_markdown(report)

    assert "1 lower-priority finding is omitted." in rendered


def test_an_uncapped_report_carries_no_omission_note() -> None:
    rendered = render_markdown(findings_only_report())

    assert "omitted" not in rendered


def test_a_report_with_findings_states_it_is_static_analysis_only() -> None:
    # A reviewer who reads "found no problems" or a list of findings without this
    # could take either as a claim about measured behaviour, which static analysis
    # cannot make.
    rendered = render_markdown(findings_only_report())

    assert "Static analysis only" in rendered
    assert "measured execution plan" in rendered


def test_an_empty_report_carries_no_analysis_basis_note() -> None:
    # Nothing was reviewed, so there is nothing to say the review method of.
    rendered = render_markdown(empty_report())

    assert "Static analysis only" not in rendered


def test_a_report_where_nothing_could_be_parsed_carries_no_analysis_basis_note() -> None:
    rendered = render_markdown(degraded_only_report())

    assert "Static analysis only" not in rendered


def test_every_rendered_body_ends_with_exactly_one_newline() -> None:
    for report in CASES.values():
        rendered = render_markdown(report)
        assert rendered.endswith("\n")
        assert not rendered.endswith("\n\n")


def _regenerate() -> None:
    """Rewrite every snapshot from the current renderer. Review the diff."""
    SNAPSHOTS.mkdir(parents=True, exist_ok=True)
    for name, report in CASES.items():
        (SNAPSHOTS / f"{name}.md").write_text(render_markdown(report), encoding="utf-8")
        print(f"wrote {name}.md")


if __name__ == "__main__":
    _regenerate()


# --- Bounded output: GitHub refuses a review body over 65,536 characters -----------


def _big_finding(index: int, *, severity: Severity = Severity.MEDIUM, size: int = 3_000) -> Finding:
    return Finding(
        rule_id="big",
        severity=severity,
        title=f"Finding {index}",
        explanation="x" * size,
        impact="i",
        provenance=Provenance(file=f"f{index}.sql", line=1),
    )


def _one_query() -> ExtractedQuery:
    return ExtractedQuery(
        id="f0.sql:1",
        kind=QueryKind.RAW_SQL,
        text="SELECT 1",
        provenance=Provenance(file="f0.sql", line=1),
    )


def test_a_body_that_is_too_large_sheds_its_lowest_priority_findings_first() -> None:
    findings = [_big_finding(0, severity=Severity.CRITICAL)]
    findings += [_big_finding(i) for i in range(1, 60)]
    report = Report(context=context(), queries=[_one_query()], findings=findings)

    rendered = render_markdown(report, max_chars=20_000)

    assert len(rendered) <= 20_000
    assert "Finding 0" in rendered  # the worst finding always survives
    assert "Finding 59" not in rendered
    assert "lower-priority findings are omitted" in rendered


def test_an_enormous_finding_is_dropped_and_counted_not_silently_lost() -> None:
    report = Report(
        context=context(), queries=[_one_query()], findings=[_big_finding(0, size=200_000)]
    )

    rendered = render_markdown(report, max_chars=10_000)

    assert len(rendered) <= 10_000
    assert "found 1 problem" in rendered
    assert "1 lower-priority finding is omitted" in rendered


def test_when_nothing_else_fits_the_body_is_cut_at_a_boundary_and_says_so() -> None:
    report = Report(
        context=context(), queries=[_one_query()], findings=[_big_finding(0, size=5_000)]
    )

    rendered = render_markdown(report, max_chars=300)

    assert len(rendered) <= 300
    assert "cut short because it exceeded GitHub's size limit" in rendered
    assert rendered.count("```") % 2 == 0  # never leaves a code fence open


def test_the_default_limit_keeps_a_real_review_under_githubs() -> None:
    findings = [_big_finding(i, size=6_000) for i in range(20)]
    report = Report(context=context(), queries=[_one_query()], findings=findings)

    rendered = render_markdown(report)

    assert len(rendered) <= 60_000 < 65_536


def test_a_normal_report_is_untouched_by_the_guard() -> None:
    report = enriched_report()

    assert render_markdown(report) == render_markdown(report, max_chars=10_000_000)


def test_a_long_list_of_unreadable_files_is_capped_and_counted() -> None:
    report = Report(
        context=context(), degraded_stages=[f"extract:db/{i:03}.sql" for i in range(60)]
    )

    rendered = render_markdown(report)

    assert "`db/024.sql`" in rendered
    assert "`db/025.sql`" not in rendered
    assert "…and 35 more" in rendered


def test_one_enormous_error_message_cannot_blow_the_body() -> None:
    report = Report(context=context(), degraded_stages=["ingest:" + "boom " * 50_000])

    rendered = render_markdown(report)

    assert len(rendered) < 2_000
