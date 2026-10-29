"""SQL dialect: detection from the JDBC URL, and its effect on a real run."""

from __future__ import annotations

import dataclasses

import pytest

from queryfix import cli
from queryfix.config import override_settings
from queryfix.db.dialect import config_candidates_for, detect_sql_dialect, is_known_dialect
from queryfix.models.query import SourceFile
from queryfix.pipeline.runner import AnalysisRunner
from tests.conftest import RecordedGitHub, RecordedPullRequest

PROPS = "src/main/resources/application.properties"
YAML = "src/main/resources/application.yml"


def _reader(files: dict[str, str]):  # type: ignore[no-untyped-def]
    return lambda path: files.get(path)


# --- detection --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("jdbc:mysql://host:3306/db", "mysql"),
        ("jdbc:mariadb://host/db", "mysql"),
        ("jdbc:postgresql://host/db", "postgres"),
        ("jdbc:oracle:thin:@//host:1521/svc", "oracle"),
        ("jdbc:sqlserver://host;databaseName=x", "tsql"),
        ("JDBC:MYSQL://host/db", "mysql"),
    ],
)
def test_the_jdbc_protocol_names_the_dialect(url: str, expected: str) -> None:
    files = {PROPS: f"spring.datasource.url={url}\n"}

    assert detect_sql_dialect(_reader(files)) == expected


def test_a_yaml_config_is_read_too() -> None:
    files = {YAML: "spring:\n  datasource:\n    url: jdbc:mysql://h/db\n"}

    assert detect_sql_dialect(_reader(files)) == "mysql"


@pytest.mark.parametrize(
    "text",
    [
        "spring.datasource.url=${DB_URL}\n",  # a placeholder says nothing
        "spring.datasource.url=jdbc:h2:mem:test\n",  # in-memory test database
        "# spring.datasource.url=jdbc:mysql://h/db\n",  # commented out
        "spring.application.name=svc\n",
        "",
    ],
)
def test_no_answer_is_better_than_a_wrong_one(text: str) -> None:
    assert detect_sql_dialect(_reader({PROPS: text})) is None


def test_a_missing_config_is_no_answer_not_an_error() -> None:
    assert detect_sql_dialect(_reader({})) is None


def test_h2_does_not_mask_the_real_database_in_a_later_file() -> None:
    files = {
        PROPS: "spring.datasource.url=jdbc:h2:mem:test\n",
        YAML: "spring:\n  datasource:\n    url: jdbc:postgresql://h/db\n",
    }

    assert detect_sql_dialect(_reader(files)) == "postgres"


def test_known_dialects() -> None:
    assert is_known_dialect("mysql")
    assert not is_known_dialect("not-a-database")


# --- effect on the runner ---------------------------------------------------------

MYSQL_MIGRATION = (
    "SELECT `id`, `status` FROM payment WHERE `status` = 'NEW';\n"
    "UPDATE payment AS p JOIN bulk_file AS s ON p.n = s.n SET p.t = s.t;\n"
)


def _run(dialect: str | None):  # type: ignore[no-untyped-def]
    return AnalysisRunner(dialect=dialect).run(
        repo="acme/x",
        pr_number=1,
        sources=[SourceFile(path="db/001.sql", content=MYSQL_MIGRATION)],
    )


def test_mysql_migration_is_unanalyzable_as_postgres() -> None:
    report = _run(None)

    assert report.degraded_stages == ["extract:db/001.sql"]


def test_the_same_migration_extracts_cleanly_as_mysql() -> None:
    report = _run("mysql")

    assert report.degraded_stages == []
    assert len(report.queries) == 2
    assert {query.dialect for query in report.queries} == {"mysql"}


def test_a_non_sql_source_is_left_alone() -> None:
    report = AnalysisRunner(dialect="mysql").run(
        repo="acme/x",
        pr_number=1,
        sources=[SourceFile(path="README.md", content="SELECT 1")],
    )

    assert report.queries == []


# --- end to end through cli.review ------------------------------------------------


def _with_files(recorded: RecordedPullRequest, files: dict[str, str]) -> RecordedGitHub:
    head_text = {**recorded.head_text, **files}
    return RecordedGitHub(recorded=dataclasses.replace(recorded, head_text=head_text))


def _entry(path: str) -> dict[str, object]:
    return {
        "filename": path,
        "status": "added",
        "patch": "@@ -0,0 +1,2 @@\n+x\n+y",
        "additions": 2,
        "deletions": 0,
        "changes": 2,
    }


def _mysql_pr(recorded: RecordedPullRequest, props: str) -> RecordedGitHub:
    head_text = {**recorded.head_text, "db/001.sql": MYSQL_MIGRATION, PROPS: props}
    pull = dataclasses.replace(recorded, entries=(_entry("db/001.sql"),), head_text=head_text)
    return RecordedGitHub(recorded=pull)


def test_review_detects_mysql_from_the_repos_own_config(recorded_pr: RecordedPullRequest) -> None:
    fake = _mysql_pr(recorded_pr, "spring.datasource.url=jdbc:mysql://h/db\n")

    result = cli.review(recorded_pr.repo, recorded_pr.number, client=fake.client, dry_run=True)

    assert not any(stage.startswith("extract:") for stage in result.report.degraded_stages)
    assert len(result.report.queries) == 2


def test_review_without_a_recognisable_url_keeps_the_default(
    recorded_pr: RecordedPullRequest,
) -> None:
    fake = _mysql_pr(recorded_pr, "spring.application.name=svc\n")

    result = cli.review(recorded_pr.repo, recorded_pr.number, client=fake.client, dry_run=True)

    assert "extract:db/001.sql" in result.report.degraded_stages


def test_the_override_beats_detection(recorded_pr: RecordedPullRequest) -> None:
    fake = _mysql_pr(recorded_pr, "spring.datasource.url=jdbc:postgresql://h/db\n")

    with override_settings(sql_dialect="mysql"):
        result = cli.review(recorded_pr.repo, recorded_pr.number, client=fake.client, dry_run=True)

    assert not any(stage.startswith("extract:") for stage in result.report.degraded_stages)


def test_a_typo_in_the_override_is_named_not_silently_ignored(
    recorded_pr: RecordedPullRequest,
) -> None:
    fake = _mysql_pr(recorded_pr, "spring.datasource.url=jdbc:mysql://h/db\n")

    with override_settings(sql_dialect="mysqll"):
        result = cli.review(recorded_pr.repo, recorded_pr.number, client=fake.client, dry_run=True)

    assert any(stage.startswith("sql_dialect:") for stage in result.report.degraded_stages)


# --- multi-module repositories ----------------------------------------------------


def test_candidates_start_with_the_conventional_root_locations_unchanged() -> None:
    from queryfix.db.discovery import DEFAULT_CANDIDATE_PATHS

    candidates = config_candidates_for([])

    assert candidates[: len(DEFAULT_CANDIDATE_PATHS)] == DEFAULT_CANDIDATE_PATHS


def test_the_module_of_each_changed_file_contributes_its_own_config() -> None:
    candidates = config_candidates_for(
        ["svc-a/src/main/java/A.java", "svc-b/src/main/resources/db/1.sql"]
    )

    assert "svc-a/src/main/resources/bootstrap.properties" in candidates
    assert "svc-a/src/main/resources/application.yml" in candidates
    assert "svc-b/src/main/resources/bootstrap.properties" in candidates


def test_nested_modules_use_the_directory_above_src() -> None:
    candidates = config_candidates_for(["platform/payments/src/main/java/A.java"])

    assert "platform/payments/src/main/resources/application.properties" in candidates


def test_a_root_level_src_path_does_not_invent_a_module() -> None:
    candidates = config_candidates_for(["src/main/java/A.java", "README.md", "docs/a.sql"])

    assert not any(path.startswith("/") or path.startswith("src/src") for path in candidates)
    assert len(candidates) == len(set(candidates))  # no duplicates


def test_candidates_are_deterministic_whatever_order_files_arrive_in() -> None:
    forward = config_candidates_for(["a/src/x", "b/src/y"])
    backward = config_candidates_for(["b/src/y", "a/src/x"])

    assert forward == backward


def test_review_detects_the_dialect_from_a_modules_bootstrap_properties(
    recorded_pr: RecordedPullRequest,
) -> None:
    # The shape of the real-world repository: the datasource URL lives in a
    # module's bootstrap.properties, not in the root application.properties.
    module = "svc/src/main/resources"
    head_text = {
        **recorded_pr.head_text,
        f"{module}/db/001.sql": MYSQL_MIGRATION,
        f"{module}/bootstrap.properties": "spring.datasource.url=jdbc:mysql://h/db\n",
    }
    pull = dataclasses.replace(
        recorded_pr, entries=(_entry(f"{module}/db/001.sql"),), head_text=head_text
    )
    fake = RecordedGitHub(recorded=pull)

    result = cli.review(pull.repo, pull.number, client=fake.client, dry_run=True)

    assert not any(stage.startswith("extract:") for stage in result.report.degraded_stages)
    assert len(result.report.queries) == 2
