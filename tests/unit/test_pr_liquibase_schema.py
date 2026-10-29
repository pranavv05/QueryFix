"""PR-aware Liquibase schema: the effective schema reaching RuleEngine.

These drive the real pipeline through :func:`queryfix.cli.review` — ingest,
schema resolution, extraction, static rules — rather than calling
:class:`~queryfix.pipeline.static_rules.engine.RuleEngine` directly, so a
pass here means the wiring genuinely works end to end: PR ingestion -> touch
detection -> PR-head rebuild (or the local-disk fallback) -> RuleEngine ->
AnalysisRunner. See ``queryfix/db/liquibase.py``'s module docstring for why
the PR-head schema is a full rebuild of the changelog tree rather than a delta
applied on top of a base schema; ``tests/unit/db/test_liquibase.py`` covers
that rebuild's structural behaviour in isolation.

The GitHub side is a purpose-built :class:`~tests.conftest.RecordedPullRequest`
per test rather than the shared sandbox fixture in ``tests/fixtures/diffs/``,
because that fixture carries no Liquibase content — these tests need to
control exactly what the "local checkout" and the "pull request's head commit"
each say, which is easiest to see written out per scenario.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from queryfix import cli
from queryfix.config import override_settings
from tests.conftest import RecordedGitHub, RecordedPullRequest

_NS = 'xmlns="http://www.liquibase.org/xml/ns/dbchangelog"'

_QUERY_PATCH = "@@ -0,0 +1 @@\n+SELECT id FROM orders WHERE customer_id = 1\n"
_QUERY_TEXT = "SELECT id FROM orders WHERE customer_id = 1\n"


def _write_changelog(path: Path, *changesets: str) -> None:
    """One local changelog file declaring one ``<changeSet>`` per argument."""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(
        f'<changeSet id="{index}" author="t">{changeset}</changeSet>'
        for index, changeset in enumerate(changesets, start=1)
    )
    path.write_text(
        f'<?xml version="1.0" encoding="UTF-8"?>\n<databaseChangeLog {_NS}>\n{body}\n</databaseChangeLog>\n'
    )


def _xml(*changesets: str) -> str:
    """The same shape as :func:`_write_changelog`, as text for a ``head_text`` entry."""
    body = "\n".join(
        f'<changeSet id="{index}" author="t">{changeset}</changeSet>'
        for index, changeset in enumerate(changesets, start=1)
    )
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<databaseChangeLog {_NS}>\n{body}\n</databaseChangeLog>\n'


def _entry(path: str, *, status: str = "modified") -> dict[str, object]:
    """A changed-file entry for a file whose content this loader never reads
    directly — true of every Liquibase XML path, since they are always
    skipped as an unsupported language before any patch is examined."""
    return {"filename": path, "status": status, "additions": 1, "deletions": 0, "patch": None}


def _query_entry(path: str) -> dict[str, object]:
    """A changed-file entry for the one query file each scenario adds."""
    return {
        "filename": path,
        "status": "added",
        "additions": 1,
        "deletions": 0,
        "patch": _QUERY_PATCH,
    }


def _pull(*, entries: list[dict[str, object]], head_text: dict[str, str]) -> RecordedPullRequest:
    return RecordedPullRequest(
        repo="acme/orders",
        number=1,
        base_sha="base-sha",
        head_sha="head-sha",
        entries=tuple(entries),
        head_text=head_text,
    )


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


def _drop_index_customer_id() -> str:
    return '<dropIndex indexName="idx_orders_customer_id" tableName="orders"/>'


def _has_unindexed_filter(report: object) -> bool:
    return any(finding.rule_id == "unindexed-filter" for finding in report.findings)  # type: ignore[attr-defined]


# --- CREATE INDEX / modifying an existing Liquibase file ------------------------


def test_pr_head_schema_suppresses_unindexed_filter_when_the_pr_adds_an_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    root = "db/changelog-root.xml"
    _write_changelog(tmp_path / root, _unindexed_orders_table())

    head_text = {
        root: _xml(_unindexed_orders_table(), _index_customer_id()),
        "OrderRepository.sql": _QUERY_TEXT,
    }
    entries = [_entry(root), _query_entry("OrderRepository.sql")]
    recorded = _pull(entries=entries, head_text=head_text)
    fake = RecordedGitHub(recorded=recorded)

    with override_settings(liquibase_changelog_path=root):
        report = cli.review(recorded.repo, recorded.number, client=fake.client, dry_run=True).report

    assert report.queries  # TEST 10: the real pipeline actually extracted the query.
    assert not _has_unindexed_filter(report)


# --- DROP INDEX / modifying an existing Liquibase file ---------------------------


def test_pr_head_schema_flags_unindexed_filter_when_the_pr_drops_an_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    root = "db/changelog-root.xml"
    _write_changelog(tmp_path / root, _unindexed_orders_table(), _index_customer_id())

    head_text = {
        root: _xml(_unindexed_orders_table(), _index_customer_id(), _drop_index_customer_id()),
        "OrderRepository.sql": _QUERY_TEXT,
    }
    entries = [_entry(root), _query_entry("OrderRepository.sql")]
    recorded = _pull(entries=entries, head_text=head_text)
    fake = RecordedGitHub(recorded=recorded)

    with override_settings(liquibase_changelog_path=root):
        report = cli.review(recorded.repo, recorded.number, client=fake.client, dry_run=True).report

    assert _has_unindexed_filter(report)


# --- Adding a new changelog file + modifying its aggregator ---------------------


def test_pr_head_schema_resolves_a_new_changelog_file_added_via_its_aggregator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    root = "db/changelog-root.xml"
    tables = "db/tables.xml"
    new_indexes = "db/new_indexes.xml"

    (tmp_path / root).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / root).write_text(
        f'<?xml version="1.0" encoding="UTF-8"?>\n<databaseChangeLog {_NS}>\n'
        '<include file="tables.xml" relativeToChangelogFile="true"/>'
        "</databaseChangeLog>\n"
    )
    _write_changelog(tmp_path / tables, _unindexed_orders_table())

    head_text = {
        root: (
            f'<?xml version="1.0" encoding="UTF-8"?>\n<databaseChangeLog {_NS}>\n'
            '<include file="tables.xml" relativeToChangelogFile="true"/>'
            '<include file="new_indexes.xml" relativeToChangelogFile="true"/>'
            "</databaseChangeLog>\n"
        ),
        tables: _xml(_unindexed_orders_table()),
        new_indexes: _xml(_index_customer_id()),
        "OrderRepository.sql": _QUERY_TEXT,
    }
    entries = [
        _entry(root),
        _entry(new_indexes, status="added"),
        _query_entry("OrderRepository.sql"),
    ]
    recorded = _pull(entries=entries, head_text=head_text)
    fake = RecordedGitHub(recorded=recorded)

    with override_settings(liquibase_changelog_path=root):
        report = cli.review(recorded.repo, recorded.number, client=fake.client, dry_run=True).report

    assert not _has_unindexed_filter(report)


# --- PR contains only Java/query changes -----------------------------------------


def test_local_disk_schema_is_kept_when_the_pr_makes_only_query_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TEST 3 / TEST 7: a pull request touching only a query file must not be
    mistaken for one touching the schema — the local-disk (base) schema is
    reused unchanged, so its own missing index still fires, proving the
    absence of a PR schema change was not read as "indexed"."""
    monkeypatch.chdir(tmp_path)
    root = "db/changelog-root.xml"
    _write_changelog(tmp_path / root, _unindexed_orders_table())

    head_text = {"OrderRepository.sql": _QUERY_TEXT}
    entries = [_query_entry("OrderRepository.sql")]
    recorded = _pull(entries=entries, head_text=head_text)
    fake = RecordedGitHub(recorded=recorded)

    with override_settings(liquibase_changelog_path=root):
        report = cli.review(recorded.repo, recorded.number, client=fake.client, dry_run=True).report

    assert _has_unindexed_filter(report)
    # No Liquibase content was ever requested over the API: the local-disk
    # schema was reused as-is, with no unnecessary rebuild attempted.
    assert root not in {path for path, _ in fake.head_reads}


# --- Aggregator modification without new changesets -------------------------------


def test_aggregator_only_edit_does_not_replay_historical_changesets_as_new(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TEST 8: an aggregator can change (a reordered comment) without
    introducing a new changeset. That must not be read as though every
    changeset it has ever included were newly introduced by the pull request
    — here, checked by the column staying indexed, not double-indexed into
    some inconsistent state."""
    monkeypatch.chdir(tmp_path)
    root = "db/changelog-root.xml"
    tables = "db/tables.xml"
    (tmp_path / root).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / root).write_text(
        f'<?xml version="1.0" encoding="UTF-8"?>\n<databaseChangeLog {_NS}>\n'
        '<include file="tables.xml" relativeToChangelogFile="true"/>'
        "</databaseChangeLog>\n"
    )
    _write_changelog(tmp_path / tables, _unindexed_orders_table(), _index_customer_id())

    head_text = {
        # Cosmetically different from the base file above (an added comment),
        # but declares no new <include> and no new <changeSet>.
        root: (
            f'<?xml version="1.0" encoding="UTF-8"?>\n<databaseChangeLog {_NS}>\n'
            "<!-- reorganized -->\n"
            '<include file="tables.xml" relativeToChangelogFile="true"/>'
            "</databaseChangeLog>\n"
        ),
        tables: _xml(_unindexed_orders_table(), _index_customer_id()),
        "OrderRepository.sql": _QUERY_TEXT,
    }
    entries = [_entry(root), _query_entry("OrderRepository.sql")]
    recorded = _pull(entries=entries, head_text=head_text)
    fake = RecordedGitHub(recorded=recorded)

    with override_settings(liquibase_changelog_path=root):
        report = cli.review(recorded.repo, recorded.number, client=fake.client, dry_run=True).report

    assert not _has_unindexed_filter(report)


# --- Unsupported raw <sql> schema changes -----------------------------------------


def test_raw_sql_only_schema_change_still_flags_unindexed_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TEST 9: an index added only inside a <sql> block must not make
    QueryFix confidently claim the column is indexed — the same documented
    limitation the local-disk loader already has."""
    monkeypatch.chdir(tmp_path)
    root = "db/changelog-root.xml"
    _write_changelog(tmp_path / root, _unindexed_orders_table())

    head_text = {
        root: _xml(
            _unindexed_orders_table(),
            "<sql>CREATE INDEX idx_orders_customer_id ON orders(customer_id);</sql>",
        ),
        "OrderRepository.sql": _QUERY_TEXT,
    }
    entries = [_entry(root), _query_entry("OrderRepository.sql")]
    recorded = _pull(entries=entries, head_text=head_text)
    fake = RecordedGitHub(recorded=recorded)

    with override_settings(liquibase_changelog_path=root):
        report = cli.review(recorded.repo, recorded.number, client=fake.client, dry_run=True).report

    assert _has_unindexed_filter(report)


# --- Fail-soft fallback -----------------------------------------------------------


def test_pr_head_rebuild_failure_falls_back_to_the_local_disk_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A pull request's head commit that cannot be rebuilt (malformed XML, in
    this case) must not cost QueryFix schema awareness entirely — the
    local-disk schema it already had is still usable and is kept."""
    monkeypatch.chdir(tmp_path)
    root = "db/changelog-root.xml"
    _write_changelog(tmp_path / root, _unindexed_orders_table(), _index_customer_id())

    head_text = {root: "<not well formed xml", "OrderRepository.sql": _QUERY_TEXT}
    entries = [_entry(root), _query_entry("OrderRepository.sql")]
    recorded = _pull(entries=entries, head_text=head_text)
    fake = RecordedGitHub(recorded=recorded)

    with override_settings(liquibase_changelog_path=root), caplog.at_level(logging.ERROR):
        report = cli.review(recorded.repo, recorded.number, client=fake.client, dry_run=True).report

    # The local-disk schema says the column IS indexed, so falling back to it
    # — rather than losing schema awareness — means no finding here.
    assert not _has_unindexed_filter(report)
    assert "failed to rebuild" in caplog.text
