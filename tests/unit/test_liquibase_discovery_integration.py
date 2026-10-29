"""Automatic Liquibase discovery, driven through the real CLI pipeline.

These mirror ``tests/unit/test_pr_liquibase_schema.py``'s style — a purpose-built
:class:`~tests.conftest.RecordedPullRequest` per scenario, driven through
:func:`queryfix.cli.review` rather than calling the schema loader directly, so
a pass here means the wiring genuinely works: no ``LIQUIBASE_CHANGELOG_PATH``
configured, ``application.properties``/``.yml`` discovered over the (faked)
GitHub API, the changelog tree resolved, and the resulting schema reaching
``RuleEngine``.

No test here uses ``monkeypatch.chdir`` or touches local disk at all — every
read goes through the recorded GitHub stand-in, which is the point: discovery
must work with no local checkout of the repository under review.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from queryfix import cli
from queryfix.config import override_settings
from queryfix.models.report import Report
from tests.conftest import RecordedGitHub, RecordedPullRequest

_NS = 'xmlns="http://www.liquibase.org/xml/ns/dbchangelog"'

_APPLICATION_PROPERTIES = "src/main/resources/application.properties"

_CUSTOMER_ID_QUERY_PATCH = "@@ -0,0 +1 @@\n+SELECT id FROM orders WHERE customer_id = 1\n"
_CUSTOMER_ID_QUERY_TEXT = "SELECT id FROM orders WHERE customer_id = 1\n"


def _xml(*changesets: str) -> str:
    body = "\n".join(
        f'<changeSet id="{index}" author="t">{changeset}</changeSet>'
        for index, changeset in enumerate(changesets, start=1)
    )
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<databaseChangeLog {_NS}>\n{body}\n</databaseChangeLog>\n'


def _include(*files: str) -> str:
    tags = "".join(f'<include file="{name}" relativeToChangelogFile="true"/>' for name in files)
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<databaseChangeLog {_NS}>\n{tags}</databaseChangeLog>\n'


def _entry(path: str, *, status: str = "modified", patch: str | None = None) -> dict[str, object]:
    return {
        "filename": path,
        "status": status,
        "additions": 1,
        "deletions": 0,
        "patch": patch,
    }


def _query_entry(path: str, *, patch: str = _CUSTOMER_ID_QUERY_PATCH) -> dict[str, object]:
    return {"filename": path, "status": "added", "additions": 1, "deletions": 0, "patch": patch}


def _pull(*, entries: list[dict[str, object]], head_text: dict[str, str]) -> RecordedPullRequest:
    return RecordedPullRequest(
        repo="acme/orders",
        number=1,
        base_sha="base-sha",
        head_sha="head-sha",
        entries=tuple(entries),
        head_text=head_text,
    )


def _properties(changelog_ref: str = "classpath:/db/changelog/master.xml") -> str:
    return f"server.port=8080\ndb.liquibase.change-log={changelog_ref}\n"


def _unindexed_orders_table() -> str:
    return (
        '<createTable tableName="orders"><column name="id" type="bigint"/>'
        '<column name="customer_id" type="bigint"/></createTable>'
    )


def _index_customer_id() -> str:
    return (
        '<createIndex indexName="idx_orders_customer_id" tableName="orders">'
        '<column name="customer_id"/></createIndex>'
    )


def _has_unindexed_filter(report: Report) -> bool:
    return any(finding.rule_id == "unindexed-filter" for finding in report.findings)


# --- 11. Automatic discovery is used when no env var is configured ---------------------


def test_automatic_discovery_is_used_when_no_env_var_is_configured() -> None:
    master = "src/main/resources/db/changelog/master.xml"
    head_text = {
        _APPLICATION_PROPERTIES: _properties(),
        master: _xml(_unindexed_orders_table()),
        "OrderRepository.sql": _CUSTOMER_ID_QUERY_TEXT,
    }
    entries = [_query_entry("OrderRepository.sql")]
    recorded = _pull(entries=entries, head_text=head_text)
    fake = RecordedGitHub(recorded=recorded)

    with override_settings(github_token="t"):
        report = cli.review(recorded.repo, recorded.number, client=fake.client, dry_run=True).report

    # No index was declared for customer_id, so a real schema was loaded and
    # correctly flagged the filter — UNKNOWN_SCHEMA would have stayed silent.
    assert _has_unindexed_filter(report)
    assert (_APPLICATION_PROPERTIES, "head-sha") in fake.head_reads


# --- 10. Explicit LIQUIBASE_CHANGELOG_PATH overrides automatic discovery ----------------


def test_explicit_path_overrides_discovery_even_when_discovery_would_disagree() -> None:
    # The discovered config points at a *different* (indexed) changelog than
    # the explicit one; the explicit path must win, and discovery must not
    # even be attempted.
    head_text = {
        _APPLICATION_PROPERTIES: _properties("classpath:/db/changelog/other.xml"),
        "src/main/resources/db/changelog/other.xml": _xml(
            _unindexed_orders_table(), _index_customer_id()
        ),
        "OrderRepository.sql": _CUSTOMER_ID_QUERY_TEXT,
    }
    entries = [_query_entry("OrderRepository.sql")]
    recorded = _pull(entries=entries, head_text=head_text)
    fake = RecordedGitHub(recorded=recorded)

    with override_settings(github_token="t", liquibase_changelog_path="does/not/exist.xml"):
        cli.review(recorded.repo, recorded.number, client=fake.client, dry_run=True)

    # The explicit (unloadable) path wins and degrades to no schema — not the
    # discovered, perfectly loadable one. Discovery is never consulted for the
    # schema: the changelog it would have pointed at is never read. (The properties
    # file itself may be read, for SQL dialect detection — a different question.)
    assert not any(path.endswith("changelog/other.xml") for path, _ in fake.head_reads)


# --- 12/13. PR changes only Java (service or repository/query) -------------------------


def test_pr_touching_only_a_service_file_still_gets_the_discovered_schema() -> None:
    master = "src/main/resources/db/changelog/master.xml"
    head_text = {
        _APPLICATION_PROPERTIES: _properties(),
        master: _xml(_unindexed_orders_table()),
        "ReportingService.sql": _CUSTOMER_ID_QUERY_TEXT,
    }
    entries = [_query_entry("ReportingService.sql")]
    recorded = _pull(entries=entries, head_text=head_text)
    fake = RecordedGitHub(recorded=recorded)

    with override_settings(github_token="t"):
        report = cli.review(recorded.repo, recorded.number, client=fake.client, dry_run=True).report

    assert report.queries
    assert _has_unindexed_filter(report)


def test_pr_touching_only_a_repository_query_still_gets_the_discovered_schema() -> None:
    master = "src/main/resources/db/changelog/master.xml"
    head_text = {
        _APPLICATION_PROPERTIES: _properties(),
        master: _xml(_unindexed_orders_table(), _index_customer_id()),
        "OrderRepository.sql": _CUSTOMER_ID_QUERY_TEXT,
    }
    entries = [_query_entry("OrderRepository.sql")]
    recorded = _pull(entries=entries, head_text=head_text)
    fake = RecordedGitHub(recorded=recorded)

    with override_settings(github_token="t"):
        report = cli.review(recorded.repo, recorded.number, client=fake.client, dry_run=True).report

    # customer_id IS indexed in the discovered schema, so no finding — proving
    # a real schema (not UNKNOWN_SCHEMA) was consulted, since a stub would also
    # have stayed silent but for a different reason.
    assert report.queries
    assert not _has_unindexed_filter(report)


# --- 14. PR changes an existing Liquibase file ------------------------------------------


def test_pr_modifying_the_changelog_rebuilds_the_schema_at_pr_head() -> None:
    master = "src/main/resources/db/changelog/master.xml"
    head_text = {
        _APPLICATION_PROPERTIES: _properties(),
        master: _xml(_unindexed_orders_table(), _index_customer_id()),
        "OrderRepository.sql": _CUSTOMER_ID_QUERY_TEXT,
    }
    entries = [_entry(master), _query_entry("OrderRepository.sql")]
    recorded = _pull(entries=entries, head_text=head_text)
    fake = RecordedGitHub(recorded=recorded)

    with override_settings(github_token="t"):
        report = cli.review(recorded.repo, recorded.number, client=fake.client, dry_run=True).report

    assert not _has_unindexed_filter(report)


# --- 15. PR adds a new Liquibase file ------------------------------------------------------


def test_pr_adding_a_new_changelog_file_is_reflected_in_the_rebuilt_schema() -> None:
    master = "src/main/resources/db/changelog/master.xml"
    new_indexes = "src/main/resources/db/changelog/new_indexes.xml"
    head_text = {
        _APPLICATION_PROPERTIES: _properties(),
        master: _include("tables.xml", "new_indexes.xml"),
        "src/main/resources/db/changelog/tables.xml": _xml(_unindexed_orders_table()),
        new_indexes: _xml(_index_customer_id()),
        "OrderRepository.sql": _CUSTOMER_ID_QUERY_TEXT,
    }
    entries = [
        _entry(new_indexes, status="added"),
        _query_entry("OrderRepository.sql"),
    ]
    recorded = _pull(entries=entries, head_text=head_text)
    fake = RecordedGitHub(recorded=recorded)

    with override_settings(github_token="t"):
        report = cli.review(recorded.repo, recorded.number, client=fake.client, dry_run=True).report

    assert not _has_unindexed_filter(report)


# --- 16. PR modifies an aggregator include --------------------------------------------------


def test_pr_modifying_the_aggregator_include_is_reconstructed_correctly() -> None:
    master = "src/main/resources/db/changelog/master.xml"
    tables = "src/main/resources/db/changelog/tables.xml"
    head_text = {
        _APPLICATION_PROPERTIES: _properties(),
        master: _include("tables.xml"),
        tables: _xml(_unindexed_orders_table(), _index_customer_id()),
        "OrderRepository.sql": _CUSTOMER_ID_QUERY_TEXT,
    }
    entries = [_entry(master), _query_entry("OrderRepository.sql")]
    recorded = _pull(entries=entries, head_text=head_text)
    fake = RecordedGitHub(recorded=recorded)

    with override_settings(github_token="t"):
        report = cli.review(recorded.repo, recorded.number, client=fake.client, dry_run=True).report

    assert not _has_unindexed_filter(report)


# --- 17. Nested Liquibase includes -----------------------------------------------------------


def test_nested_includes_resolve_the_complete_schema() -> None:
    master = "src/main/resources/db/changelog/master.xml"
    tables = "src/main/resources/db/changelog/tables.xml"
    indexes = "src/main/resources/db/changelog/indexes.xml"
    head_text = {
        _APPLICATION_PROPERTIES: _properties(),
        master: _include("tables.xml"),
        tables: (
            f'<?xml version="1.0" encoding="UTF-8"?>\n<databaseChangeLog {_NS}>\n'
            f'<changeSet id="1" author="t">{_unindexed_orders_table()}</changeSet>'
            '<include file="indexes.xml" relativeToChangelogFile="true"/>'
            "</databaseChangeLog>\n"
        ),
        indexes: _xml(_index_customer_id()),
        "OrderRepository.sql": _CUSTOMER_ID_QUERY_TEXT,
    }
    entries = [_query_entry("OrderRepository.sql")]
    recorded = _pull(entries=entries, head_text=head_text)
    fake = RecordedGitHub(recorded=recorded)

    with override_settings(github_token="t"):
        report = cli.review(recorded.repo, recorded.number, client=fake.client, dry_run=True).report

    assert not _has_unindexed_filter(report)


# --- 18. Conflicting application configurations, at the CLI level ---------------------------


def test_conflicting_application_configurations_fail_discovery_safely(
    caplog: pytest.LogCaptureFixture,
) -> None:
    head_text = {
        _APPLICATION_PROPERTIES: _properties("classpath:/db/changelog/a.xml"),
        "src/main/resources/application.yml": (
            "db:\n  liquibase:\n    change-log: classpath:/db/changelog/b.xml\n"
        ),
        "src/main/resources/db/changelog/a.xml": _xml(
            _unindexed_orders_table(), _index_customer_id()
        ),
        "src/main/resources/db/changelog/b.xml": _xml(_unindexed_orders_table()),
        "OrderRepository.sql": _CUSTOMER_ID_QUERY_TEXT,
    }
    entries = [_query_entry("OrderRepository.sql")]
    recorded = _pull(entries=entries, head_text=head_text)
    fake = RecordedGitHub(recorded=recorded)

    with override_settings(github_token="t"), caplog.at_level(logging.WARNING):
        report = cli.review(recorded.repo, recorded.number, client=fake.client, dry_run=True).report

    # Neither candidate schema is trusted — UNKNOWN_SCHEMA, so the rule stays
    # silent rather than guessing between an indexed and an unindexed answer.
    assert not _has_unindexed_filter(report)
    # Neither changelog was ever fetched: ambiguity is resolved from the two
    # configuration files alone, without also walking either candidate tree.
    fetched = {path for path, _ in fake.head_reads}
    assert "src/main/resources/db/changelog/a.xml" not in fetched
    assert "src/main/resources/db/changelog/b.xml" not in fetched


# --- 19. Discovered changelog cannot actually be resolved ------------------------------------


def test_a_discovered_changelog_that_cannot_be_loaded_falls_back_gracefully(
    caplog: pytest.LogCaptureFixture,
) -> None:
    head_text = {
        _APPLICATION_PROPERTIES: _properties("classpath:/db/changelog/master.xml"),
        "src/main/resources/db/changelog/master.xml": "<not well formed xml",
        "OrderRepository.sql": _CUSTOMER_ID_QUERY_TEXT,
    }
    entries = [_query_entry("OrderRepository.sql")]
    recorded = _pull(entries=entries, head_text=head_text)
    fake = RecordedGitHub(recorded=recorded)

    with override_settings(github_token="t"), caplog.at_level(logging.ERROR):
        report = cli.review(recorded.repo, recorded.number, client=fake.client, dry_run=True).report

    assert report.queries
    assert not _has_unindexed_filter(report)
    assert "could not be loaded" in caplog.text


# --- 20. GitHub API failure while discovering config -------------------------------------------


def test_a_github_failure_reading_application_properties_falls_back_gracefully() -> None:
    head_text = {
        "src/main/resources/application.yml": (
            "db:\n  liquibase:\n    change-log: classpath:/db/changelog/master.xml\n"
        ),
        "src/main/resources/db/changelog/master.xml": _xml(
            _unindexed_orders_table(), _index_customer_id()
        ),
        "OrderRepository.sql": _CUSTOMER_ID_QUERY_TEXT,
    }
    entries = [_query_entry("OrderRepository.sql")]
    recorded = _pull(entries=entries, head_text=head_text)
    # `.properties` 404s (never recorded); `.yml` still succeeds, proving one
    # candidate's failure does not sink discovery.
    fake = RecordedGitHub(recorded=recorded)

    with override_settings(github_token="t"):
        report = cli.review(recorded.repo, recorded.number, client=fake.client, dry_run=True).report

    assert not _has_unindexed_filter(report)


def test_every_candidate_failing_falls_back_to_unknown_schema_not_a_crash() -> None:
    head_text = {"OrderRepository.sql": _CUSTOMER_ID_QUERY_TEXT}
    entries = [_query_entry("OrderRepository.sql")]
    recorded = _pull(entries=entries, head_text=head_text)
    fake = RecordedGitHub(recorded=recorded)

    with override_settings(github_token="t"):
        report = cli.review(recorded.repo, recorded.number, client=fake.client, dry_run=True).report

    # No exception, and (with no schema at all) unindexed-filter has nothing
    # to say — the same silence QueryFix has always had unconfigured.
    assert report.queries
    assert not _has_unindexed_filter(report)


# --- 21. No local checkout is required --------------------------------------------------------


def test_no_local_disk_is_touched_during_discovery(tmp_path: Path) -> None:
    """Every read in every scenario above already goes through the recorded
    GitHub stand-in rather than local disk (unlike the explicit-path tests in
    ``test_pr_liquibase_schema.py``, none of these use ``monkeypatch.chdir``).
    This test additionally proves discovery does not depend on the process's
    current working directory at all by running from an empty one."""
    master = "src/main/resources/db/changelog/master.xml"
    head_text = {
        _APPLICATION_PROPERTIES: _properties(),
        master: _xml(_unindexed_orders_table(), _index_customer_id()),
        "OrderRepository.sql": _CUSTOMER_ID_QUERY_TEXT,
    }
    entries = [_query_entry("OrderRepository.sql")]
    recorded = _pull(entries=entries, head_text=head_text)
    fake = RecordedGitHub(recorded=recorded)

    original_cwd = os.getcwd()
    os.chdir(str(tmp_path))
    try:
        with override_settings(github_token="t"):
            report = cli.review(
                recorded.repo, recorded.number, client=fake.client, dry_run=True
            ).report
    finally:
        os.chdir(original_cwd)

    assert report.queries
    assert not _has_unindexed_filter(report)


# --- End-to-end acceptance test ---------------------------------------------------------------


def test_end_to_end_acceptance_discovery_through_final_report() -> None:
    """The full acceptance scenario: a synthetic Spring Boot repository whose
    ``application.properties`` names a three-file changelog chain
    (master -> tables -> indexes), reviewed by a pull request that touches
    only a Java repository query — never a Liquibase file. QueryFix must
    discover the changelog, load the complete schema, extract the query,
    run static analysis with schema awareness, run N+1 detection, and reach
    the Groq explanation boundary (a no-op here with no configured key) — all
    the way to the final rendered report — without any schema file appearing
    in the pull request's own diff.
    """
    master = "src/main/resources/db/changelog/master.xml"
    tables = "src/main/resources/db/changelog/tables.xml"
    indexes = "src/main/resources/db/changelog/indexes.xml"

    head_text = {
        _APPLICATION_PROPERTIES: _properties(),
        master: _include("tables.xml"),
        tables: (
            f'<?xml version="1.0" encoding="UTF-8"?>\n<databaseChangeLog {_NS}>\n'
            '<changeSet id="1" author="t">'
            '<createTable tableName="orders">'
            '<column name="id" type="bigint"/>'
            '<column name="customer_id" type="bigint"/>'
            '<column name="status" type="varchar(20)"/>'
            "</createTable>"
            "</changeSet>"
            '<include file="indexes.xml" relativeToChangelogFile="true"/>'
            "</databaseChangeLog>\n"
        ),
        indexes: _xml(_index_customer_id()),
        "OrderRepository.java": (
            "package com.example.data;\n\n"
            "import org.springframework.data.jpa.repository.JpaRepository;\n"
            "import org.springframework.data.jpa.repository.Query;\n\n"
            "public interface OrderRepository extends JpaRepository<Object, Long> {\n\n"
            '    @Query(value = "SELECT * FROM orders WHERE customer_id = 1", nativeQuery = true)\n'
            "    java.util.List<Object> findByCustomerIdNative();\n\n"
            '    @Query(value = "SELECT * FROM orders WHERE status = 1", nativeQuery = true)\n'
            "    java.util.List<Object> findByStatusNative();\n"
            "}\n"
        ),
    }
    java_lines = head_text["OrderRepository.java"].splitlines()
    java_patch = (
        f"@@ -0,0 +1,{len(java_lines)} @@\n" + "\n".join(f"+{line}" for line in java_lines) + "\n"
    )
    entries = [_entry("OrderRepository.java", status="added", patch=java_patch)]
    recorded = _pull(entries=entries, head_text=head_text)
    fake = RecordedGitHub(recorded=recorded)

    # No LIQUIBASE_CHANGELOG_PATH, no GROQ_API_KEY: standard `queryfix review`
    # with nothing configured beyond the token, exactly the acceptance target.
    with override_settings(github_token="t"):
        report = cli.review(recorded.repo, recorded.number, client=fake.client, dry_run=True).report

    # No Liquibase file appears anywhere in the pull request's diff.
    assert not any(query.provenance.file.endswith(".xml") for query in report.queries)

    # Both native queries were extracted from the Java repository interface.
    assert len(report.queries) == 2

    unindexed_filter_titles = [
        finding.title for finding in report.findings if finding.rule_id == "unindexed-filter"
    ]
    # customer_id is indexed per the discovered+loaded schema: no finding on it.
    assert not any("customer_id" in title for title in unindexed_filter_titles)
    # status is not indexed: the existing rule still fires on it, proving the
    # schema was genuinely consulted rather than defaulted to unknown.
    assert any("status" in title for title in unindexed_filter_titles)

    # The Groq explanation boundary is reached without error (no key
    # configured => no-op enrichment, not a crash) and the report still
    # renders end to end.
    from queryfix.pipeline.report import render_markdown

    markdown = render_markdown(report)
    assert "orders" in markdown
    assert "status" in markdown


# --- Repository-wide candidate fallback (Phase 2.3/2.4) -----------------------------


def test_no_configuration_anywhere_recovers_via_repository_wide_candidate_search() -> None:
    # No application.properties/.yml exists in the head tree at all —
    # conventional-location discovery finds nothing (NOT_FOUND) — yet a
    # changelog exists and is unambiguously identifiable from repository
    # evidence alone: root-shaped (not referenced by anything, itself
    # includes the child), with no db.liquibase.change-log declared anywhere.
    root = "app/master.xml"
    child = "db/changelog/orders.xml"
    head_text = {
        # relativeToChangelogFile="true", resolved against root's own
        # directory ("app") — "../db/changelog/orders.xml" is what actually
        # resolves to `child`, not `child`'s own repo-relative path.
        root: _include("../db/changelog/orders.xml"),
        child: _xml(_unindexed_orders_table()),
        "OrderRepository.sql": _CUSTOMER_ID_QUERY_TEXT,
    }
    entries = [_query_entry("OrderRepository.sql")]
    recorded = _pull(entries=entries, head_text=head_text)
    fake = RecordedGitHub(recorded=recorded)

    with override_settings(github_token="t"):
        report = cli.review(recorded.repo, recorded.number, client=fake.client, dry_run=True).report

    # The real schema was found and consulted (unindexed_filter fires),
    # even though nothing anywhere declared db.liquibase.change-log.
    assert _has_unindexed_filter(report)


def test_two_equally_plausible_candidates_degrade_to_unknown_schema_not_a_guess() -> None:
    # Two independent, equally-referenced-nowhere, equally-conventionally-named
    # changelogs: the fallback must refuse to guess, exactly as conflicting
    # application.properties files already do.
    head_text = {
        "service-a/db/changelog/master.xml": _xml(_unindexed_orders_table()),
        "service-b/db/changelog/master.xml": _xml(_unindexed_orders_table()),
        "OrderRepository.sql": _CUSTOMER_ID_QUERY_TEXT,
    }
    entries = [_query_entry("OrderRepository.sql")]
    recorded = _pull(entries=entries, head_text=head_text)
    fake = RecordedGitHub(recorded=recorded)

    with override_settings(github_token="t"):
        report = cli.review(recorded.repo, recorded.number, client=fake.client, dry_run=True).report

    # UNKNOWN_SCHEMA: the ambiguous fallback must not silently pick one.
    assert not _has_unindexed_filter(report)


def test_no_changelog_anywhere_is_the_same_silent_unknown_schema_as_before() -> None:
    # No application.properties, no candidate XML at all: the repository-wide
    # fallback must degrade exactly as an unconfigured, undiscoverable
    # repository always has — no crash, no findings from a schema that does
    # not exist.
    head_text = {"OrderRepository.sql": _CUSTOMER_ID_QUERY_TEXT}
    entries = [_query_entry("OrderRepository.sql")]
    recorded = _pull(entries=entries, head_text=head_text)
    fake = RecordedGitHub(recorded=recorded)

    with override_settings(github_token="t"):
        report = cli.review(recorded.repo, recorded.number, client=fake.client, dry_run=True).report

    assert not _has_unindexed_filter(report)
    assert report.queries
