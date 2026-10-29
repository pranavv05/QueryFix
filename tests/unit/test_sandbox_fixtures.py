"""Integrity tests for the queryfix-sandbox fixture project.

The sandbox exists so QueryFix has known-bad input with known-good
counterparts. These tests guard the fixture itself: if someone "fixes" a planted
bug or adds the missing index, the rules that depend on it start silently passing
against nothing. They assert source-level facts only — no JVM, no database.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlglot

SANDBOX = Path(__file__).resolve().parents[2] / "queryfix-sandbox"
JAVA_ROOT = SANDBOX / "src/main/java/com/queryfix/sandbox"
RESOURCES = SANDBOX / "src/main/resources"
MIGRATION = RESOURCES / "db/migration/V1__create_tables.sql"
PROPERTIES = RESOURCES / "application.properties"
SPY_PROPERTIES = RESOURCES / "spy.properties"


def _table_name(statement: sqlglot.exp.Expression) -> str | None:
    """The table a ``CREATE TABLE`` names, or None if the node has none.

    For ``CREATE TABLE``, ``statement.this`` is a Schema wrapping the Table, so
    the Table node has to be reached for rather than read off the wrapper.
    ``find`` is Optional, and saying so here keeps every caller narrowed.
    """
    table = statement.find(sqlglot.exp.Table)
    return table.name if table is not None else None


PLANTED_BUGS = {
    "native_select_star_no_where": JAVA_ROOT / "repository/OrderRepository.java",
    "jpql_unindexed_column": JAVA_ROOT / "repository/CustomerRepository.java",
    "n_plus_one_in_loop": JAVA_ROOT / "service/ReportingService.java",
    "update_no_where": JAVA_ROOT / "repository/CustomerRepository.java",
}


def test_sandbox_project_exists() -> None:
    assert (SANDBOX / "pom.xml").is_file()
    assert MIGRATION.is_file()


def test_maven_wrapper_is_checked_in() -> None:
    # Without the wrapper the sandbox needs a system Maven install, which makes it
    # unbuildable in CI and on a fresh clone.
    assert (SANDBOX / "mvnw").is_file()
    assert (SANDBOX / "mvnw.cmd").is_file()
    assert (SANDBOX / ".mvn/wrapper/maven-wrapper.properties").is_file()


@pytest.mark.parametrize("name", sorted(PLANTED_BUGS))
def test_planted_bug_marker_is_present(name: str) -> None:
    source = PLANTED_BUGS[name].read_text(encoding="utf-8")
    assert "PLANTED BUG:" in source


def test_all_four_planted_bugs_are_present() -> None:
    total = sum(
        path.read_text(encoding="utf-8").count("PLANTED BUG:")
        for path in sorted(set(PLANTED_BUGS.values()))
    )
    assert total == 4, f"expected exactly 4 planted bugs, found {total}"


def test_bug_one_is_select_star_with_no_where() -> None:
    source = (JAVA_ROOT / "repository/OrderRepository.java").read_text(encoding="utf-8")
    assert 'value = "SELECT * FROM orders"' in source
    assert "nativeQuery = true" in source

    parsed = sqlglot.parse_one("SELECT * FROM orders", read="postgres")
    assert parsed.find(sqlglot.exp.Star) is not None
    assert parsed.find(sqlglot.exp.Where) is None


def test_bug_two_filters_a_column_the_migration_leaves_unindexed() -> None:
    source = (JAVA_ROOT / "repository/CustomerRepository.java").read_text(encoding="utf-8")
    assert "WHERE c.country = :country" in source

    # The fixture only works while country has no index.
    indexed_columns = {
        column.name
        for statement in sqlglot.parse(MIGRATION.read_text(encoding="utf-8"), read="postgres")
        if isinstance(statement, sqlglot.exp.Create) and statement.kind == "INDEX"
        for column in statement.find_all(sqlglot.exp.Column)
    }
    assert "country" not in indexed_columns
    # Sanity check that the extraction above actually found the real indexes.
    assert {"customer_id", "placed_at", "order_id"} <= indexed_columns


def test_bug_three_calls_a_derived_method_inside_a_loop() -> None:
    source = (JAVA_ROOT / "service/ReportingService.java").read_text(encoding="utf-8")
    loop_start = source.index("for (Customer customer : customers)")
    loop_body = source[loop_start : source.index("return summaries;", loop_start)]
    assert "orderRepository.findByCustomerId(" in loop_body


def test_bug_four_is_an_update_with_no_where() -> None:
    source = (JAVA_ROOT / "repository/CustomerRepository.java").read_text(encoding="utf-8")
    assert '@Query("UPDATE Customer c SET c.loyaltyTier = :tier")' in source


def test_destructive_fixture_is_guarded_and_defaults_off() -> None:
    properties = PROPERTIES.read_text(encoding="utf-8")
    assert "sandbox.allow-destructive-fixtures=false" in properties

    service = (JAVA_ROOT / "service/MaintenanceService.java").read_text(encoding="utf-8")
    assert "if (!destructiveFixturesAllowed)" in service
    assert "throw new IllegalStateException" in service


def test_healthy_counterparts_exist_for_false_positive_guarding() -> None:
    expected = {
        "repository/OrderRepository.java": "exportRecentOrders",
        "repository/CustomerRepository.java": "promoteHighValueCustomers",
        "service/ReportingService.java": "buildCustomerOrderSummaryBatched",
        "repository/OrderItemRepository.java": "findByOrderIdIn",
    }
    for relative_path, method in expected.items():
        source = (JAVA_ROOT / relative_path).read_text(encoding="utf-8")
        assert method in source, f"{relative_path} is missing {method}"


def test_migration_parses_as_postgres() -> None:
    statements = [
        s for s in sqlglot.parse(MIGRATION.read_text(encoding="utf-8"), read="postgres") if s
    ]
    # For CREATE TABLE, `statement.this` is a Schema wrapping the Table — reach
    # for the Table node rather than reading `.name` off the wrapper.
    tables = {
        name
        for statement in statements
        if isinstance(statement, sqlglot.exp.Create) and statement.kind == "TABLE"
        for name in [_table_name(statement)]
        if name is not None
    }
    assert tables == {"customers", "orders", "order_items"}


def test_country_column_type_matches_the_entity_mapping() -> None:
    # Regression guard. `CHAR(2)` reads as `bpchar` to Postgres, which
    # `ddl-auto=validate` rejects against `@Column(length = 2) String country`, so
    # the app refused to start. Only execution surfaced it — parsing the migration
    # in isolation cannot.
    #
    # Checked against the parsed type rather than the text: `CHAR(2)` is a
    # substring of `VARCHAR(2)`, so no textual assertion can tell them apart.
    customers = next(
        statement
        for statement in sqlglot.parse(MIGRATION.read_text(encoding="utf-8"), read="postgres")
        if isinstance(statement, sqlglot.exp.Create)
        and statement.kind == "TABLE"
        and _table_name(statement) == "customers"
    )
    country = next(
        column for column in customers.find_all(sqlglot.exp.ColumnDef) if column.name == "country"
    )

    assert country.kind is not None
    assert country.kind.this is sqlglot.exp.DataType.Type.VARCHAR
    assert country.kind.sql(dialect="postgres") == "VARCHAR(2)"

    entity = (JAVA_ROOT / "domain/Customer.java").read_text(encoding="utf-8")
    assert "length = 2" in entity


def test_seeder_uses_a_fixed_random_seed() -> None:
    seeder = (JAVA_ROOT / "seed/DataSeeder.java").read_text(encoding="utf-8")
    # Reproducible plans depend on reproducible data.
    assert "new Random(randomSeed)" in seeder
    assert "sandbox.seed.random-seed:20260805" in seeder


def test_seeder_runs_before_the_fixture_exerciser() -> None:
    # Both are ApplicationRunners; unordered, the exerciser could query empty
    # tables and the N+1 fixture would log nothing.
    seeder = (JAVA_ROOT / "seed/DataSeeder.java").read_text(encoding="utf-8")
    exerciser = (JAVA_ROOT / "seed/FixtureExerciser.java").read_text(encoding="utf-8")

    assert "@Order(DataSeeder.RUN_ORDER)" in seeder
    assert "RUN_ORDER = DataSeeder.RUN_ORDER + 1" in exerciser


def test_fixture_exerciser_defaults_off_and_skips_the_destructive_fixture() -> None:
    assert "sandbox.exercise-fixtures=false" in PROPERTIES.read_text(encoding="utf-8")

    exerciser = (JAVA_ROOT / "seed/FixtureExerciser.java").read_text(encoding="utf-8")
    assert "buildCustomerOrderSummary()" in exerciser
    # The UPDATE-without-WHERE fixture must never be reachable from an automated
    # path, only analysed statically.
    assert "promoteAllToTier" not in exerciser
    assert "promoteLoyaltyTier" not in exerciser


def test_datasource_is_routed_through_p6spy() -> None:
    properties = PROPERTIES.read_text(encoding="utf-8")

    assert "spring.datasource.url=jdbc:p6spy:postgresql://" in properties
    assert "spring.datasource.driver-class-name=com.p6spy.engine.spy.P6SpyDriver" in properties


def test_spy_properties_format_matches_what_the_parser_expects() -> None:
    # Cross-file contract: `integrations/p6spy.py` splits on `|` into exactly
    # timestamp, elapsed, category, sql. Reordering these fields silently breaks
    # N+1 evidence gathering, so the two files have to move together.
    from queryfix.integrations import p6spy

    spy = SPY_PROPERTIES.read_text(encoding="utf-8")

    assert "driverlist=org.postgresql.Driver" in spy
    assert (
        "customLogMessageFormat=%(currentTime)|%(executionTime)|%(category)|%(sqlSingleLine)" in spy
    )
    assert p6spy._FIELD_COUNT == 4

    # Row-level categories would swamp the statements with one line per row.
    excluded = next(
        line.split("=", 1)[1] for line in spy.splitlines() if line.startswith("excludecategories=")
    )
    assert {"result", "resultset"} <= set(excluded.split(","))


def test_p6spy_log_is_written_under_target() -> None:
    # p6spy's FileLogger does not create missing parent directories, so the path
    # has to be one the build guarantees exists.
    spy = SPY_PROPERTIES.read_text(encoding="utf-8")
    assert "logfile=target/p6spy-statements.log" in spy
