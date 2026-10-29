"""Cross-cutting invariants of the static pipeline.

The stage tests ask "does this rule fire". These ask the questions that only show up
once the stages are assembled and put under load:

* is the output **deterministic** — the same diff twice must not produce two
  different PR comments, or reviewers stop believing the tool
* is it **safe to share** — one ``AnalysisRunner`` serves concurrent requests from
  FastAPI's threadpool
* does it **serialize losslessly** — the report crosses an HTTP boundary and will
  later cross a Markdown renderer
* is it **internally consistent** — every finding must point at a query that is
  actually in the report, at the line that query actually came from
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from queryfix.models import Report, Severity, SqlSource
from queryfix.pipeline.runner import AnalysisRunner

# Two sources whose findings interleave by severity: each yields a HIGH (no-limit)
# and a MEDIUM (select-star), so ranking and tie-breaking are both observable.
SOURCES = [
    SqlSource(path="a.sql", content="SELECT * FROM orders"),
    SqlSource(path="b.sql", content="SELECT * FROM customers"),
]


def run(runner: AnalysisRunner | None = None, sources: list[SqlSource] | None = None) -> Report:
    """One run with a fixed identity, so only the analysis can vary."""
    return (runner or AnalysisRunner()).run(
        repo="acme/billing-service",
        pr_number=42,
        sources=SOURCES if sources is None else sources,
        run_id="fixed-run-id",
    )


# --------------------------------------------------------------------------------
# Determinism.
# --------------------------------------------------------------------------------


def test_the_same_input_produces_byte_identical_output() -> None:
    # The PR comment is rewritten in place on every push. If the same queries render
    # differently run to run, the comment churns and the tool looks unreliable.
    assert run().model_dump_json() == run().model_dump_json()


def test_determinism_does_not_depend_on_reusing_the_runner() -> None:
    shared = AnalysisRunner()

    assert run(shared).model_dump_json() == run(AnalysisRunner()).model_dump_json()


def test_source_order_is_preserved_in_the_report() -> None:
    forwards = run(sources=SOURCES)
    backwards = run(sources=list(reversed(SOURCES)))

    assert [query.id for query in forwards.queries] == ["a.sql:1", "b.sql:1"]
    assert [query.id for query in backwards.queries] == ["b.sql:1", "a.sql:1"]


# --------------------------------------------------------------------------------
# Ordering.
# --------------------------------------------------------------------------------


def test_findings_are_ranked_worst_first_and_ties_keep_source_order() -> None:
    findings = run().findings

    assert [(finding.rule_id, finding.query_id) for finding in findings] == [
        ("no-limit", "a.sql:1"),
        ("no-limit", "b.sql:1"),
        ("select-star", "a.sql:1"),
        ("select-star", "b.sql:1"),
    ]
    # Severity is the primary key; source order breaks ties. An unstable sort here
    # would reorder equal-severity findings between runs — see the determinism tests.
    assert [finding.severity for finding in findings] == [
        Severity.HIGH,
        Severity.HIGH,
        Severity.MEDIUM,
        Severity.MEDIUM,
    ]


def test_a_critical_finding_from_the_last_file_outranks_a_medium_from_the_first() -> None:
    report = run(
        sources=[
            SqlSource(path="a.sql", content="SELECT * FROM orders WHERE id = 1"),
            SqlSource(path="z.sql", content="DELETE FROM customers"),
        ]
    )

    # This is what a single engine call over the whole query set buys: ranking across
    # files, not per-file lists concatenated in file order.
    assert report.findings[0].severity is Severity.CRITICAL
    assert report.findings[0].provenance.file == "z.sql"


def test_degraded_stages_are_listed_in_source_order() -> None:
    report = run(
        sources=[
            SqlSource(path="bad-1.sql", content="SELECT FROM WHERE"),
            SqlSource(path="ok.sql", content="SELECT id FROM t WHERE id = 1"),
            SqlSource(path="bad-2.sql", content="SELECT FROM WHERE"),
        ]
    )

    assert report.degraded_stages == ["extract:bad-1.sql", "extract:bad-2.sql"]


# --------------------------------------------------------------------------------
# Duplicates.
# --------------------------------------------------------------------------------


def test_identical_statements_are_reported_once_each_not_merged() -> None:
    # Two copies of the same bad query are two things to fix, in two places. They must
    # stay distinguishable by query id, and neither may be silently dropped.
    report = run(sources=[SqlSource(path="m.sql", content="SELECT * FROM t; SELECT * FROM t;")])

    assert [query.id for query in report.queries] == ["m.sql:1", "m.sql:2"]
    assert {finding.query_id for finding in report.findings} == {"m.sql:1", "m.sql:2"}


def test_the_same_source_supplied_twice_is_analyzed_twice() -> None:
    duplicated = [SOURCES[0], SOURCES[0]]

    report = run(sources=duplicated)

    # Known wart: ids are derived from path, so a repeated path repeats the id. The
    # report is still complete; this pins the behaviour so a future id scheme has to
    # decide deliberately rather than change it by accident.
    assert len(report.queries) == 2
    assert [query.id for query in report.queries] == ["a.sql:1", "a.sql:1"]


def test_one_rule_reports_a_query_once_however_many_times_it_matches() -> None:
    # `SELECT *, t.*` holds two stars. One finding, not two — a rule that emits per
    # match turns a single mistake into a wall of identical comments.
    report = run(sources=[SqlSource(path="m.sql", content="SELECT *, t.* FROM t WHERE id = 1")])

    assert [finding.rule_id for finding in report.findings] == ["select-star"]


# --------------------------------------------------------------------------------
# Internal consistency — every finding must be anchorable to the diff.
# --------------------------------------------------------------------------------


def test_every_finding_points_at_a_query_that_is_in_the_report() -> None:
    report = run(
        sources=[
            *SOURCES,
            SqlSource(path="c.sql", content="DELETE FROM customers"),
            SqlSource(path="bad.sql", content="SELECT FROM WHERE"),
        ]
    )

    by_id = {query.id: query for query in report.queries}
    assert report.findings

    for finding in report.findings:
        assert finding.query_id in by_id, finding.rule_id
        # Provenance is copied from the query, so a mismatch means findings and
        # queries drifted apart — exactly the failure the span/statement desync caused.
        assert finding.provenance == by_id[finding.query_id].provenance
        assert finding.provenance.line is not None


def test_unanalyzable_queries_are_reported_but_never_carry_findings() -> None:
    report = run(
        sources=[SqlSource(path="bad.sql", content="SELECT FROM WHERE"), *SOURCES],
    )

    unanalyzable = {query.id for query in report.queries if query.parse_error is not None}
    assert unanalyzable
    assert not [finding for finding in report.findings if finding.query_id in unanalyzable]


# --------------------------------------------------------------------------------
# Serialization — the report crosses an HTTP boundary and a Markdown renderer.
# --------------------------------------------------------------------------------


def test_the_report_round_trips_through_json_without_loss() -> None:
    report = run()

    assert Report.model_validate(json.loads(report.model_dump_json())) == report


def test_the_json_form_holds_only_json_native_types() -> None:
    payload = json.loads(run().model_dump_json())

    # `mode="json"` and the serializer must agree; a mismatch means something is a
    # Python object in one path and a string in the other.
    assert run().model_dump(mode="json") == payload
    # Enums serialize as their values, not as `Severity.HIGH`.
    assert {finding["severity"] for finding in payload["findings"]} <= {
        "info",
        "low",
        "medium",
        "high",
        "critical",
    }


def test_non_ascii_sql_survives_the_http_round_trip(client: TestClient) -> None:
    sql = "SELECT * FROM bestellungen WHERE stadt = 'Zürich'"

    response = client.post(
        "/analyze",
        json={"repo": "acme/billing-service", "pr_number": 42, "sql": sql},
    )

    assert response.status_code == 200
    (query,) = response.json()["report"]["queries"]
    assert query["text"] == sql


# --------------------------------------------------------------------------------
# Concurrency — FastAPI runs the sync route in a threadpool over one shared runner.
# --------------------------------------------------------------------------------


def test_a_shared_runner_gives_every_concurrent_run_the_same_answer() -> None:
    runner = AnalysisRunner()
    expected = run(runner).model_dump_json()

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = [pool.submit(run, runner) for _ in range(40)]
        payloads = {future.result().model_dump_json() for future in results}

    # One distinct payload: no run saw another run's queries, findings, or degradations.
    assert payloads == {expected}


def test_concurrent_runs_do_not_collide_on_run_ids() -> None:
    runner = AnalysisRunner()

    def unidentified() -> str:
        return runner.run(repo="acme/x", pr_number=1, sources=SOURCES).context.run_id

    with ThreadPoolExecutor(max_workers=8) as pool:
        run_ids = [future.result() for future in [pool.submit(unidentified) for _ in range(60)]]

    assert len(set(run_ids)) == 60


def test_concurrent_requests_through_the_api_stay_independent(client: TestClient) -> None:
    payloads = [
        {"repo": "acme/billing-service", "pr_number": 42, "sql": "SELECT * FROM orders"},
        {"repo": "acme/billing-service", "pr_number": 42, "sql": "DELETE FROM customers"},
        {"repo": "acme/billing-service", "pr_number": 42, "sql": "SELECT FROM WHERE"},
    ]

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(client.post, "/analyze", json=payloads[n % 3]) for n in range(30)]
        bodies = [future.result().json() for future in futures]

    for index, body in enumerate(bodies):
        expected = payloads[index % 3]["sql"]
        assert [query["text"] for query in body["report"]["queries"]] == [expected]
    assert len({body["run_id"] for body in bodies}) == 30
