"""Tests for automatic Liquibase changelog discovery.

Pure unit tests: ``read`` is a plain in-memory dict lookup, not GitHub — the
point here is the parsing and resolution logic in isolation.
``tests/unit/test_liquibase_discovery_integration.py`` drives the same module
through the real CLI pipeline instead, including the PR-head rebuild and the
end-to-end acceptance scenario.
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from queryfix.db.discovery import (
    DEFAULT_CANDIDATE_PATHS,
    PROPERTY_KEY,
    DiscoveryStatus,
    discover_liquibase_changelog,
)
from queryfix.db.liquibase import ReadChangelogFile


def reader(files: Mapping[str, str]) -> ReadChangelogFile:
    """A ``ReadChangelogFile`` backed by a plain dict, raising for anything absent."""

    def read(path: str) -> str:
        if path not in files:
            raise FileNotFoundError(path)
        return files[path]

    return read


# --- 1. application.properties discovery ----------------------------------------


def test_discovers_the_standard_properties_location() -> None:
    result = discover_liquibase_changelog(
        reader(
            {
                "src/main/resources/application.properties": (
                    "db.liquibase.change-log=classpath:/db/changelog/db.changelog-master.xml\n"
                )
            }
        )
    )

    assert result.status is DiscoveryStatus.DISCOVERED
    assert result.changelog_path == "src/main/resources/db/changelog/db.changelog-master.xml"
    assert result.source_file == "src/main/resources/application.properties"


def test_falls_back_to_repository_root_application_properties() -> None:
    result = discover_liquibase_changelog(
        reader(
            {"application.properties": "db.liquibase.change-log=classpath:/db/changelog/m.xml\n"}
        )
    )

    assert result.status is DiscoveryStatus.DISCOVERED
    assert result.changelog_path == "db/changelog/m.xml"
    assert result.source_file == "application.properties"


def test_the_default_candidate_paths_never_include_a_profile_specific_file() -> None:
    assert not any("-" in path.rsplit("/", 1)[-1] for path in DEFAULT_CANDIDATE_PATHS)
    assert "src/main/resources/application-dev.properties" not in DEFAULT_CANDIDATE_PATHS


def test_a_profile_specific_file_is_never_read_even_if_present() -> None:
    # Only base file names are ever candidates; a dev/prod/test profile file
    # existing alongside (or instead of) the base file must not be selected.
    result = discover_liquibase_changelog(
        reader(
            {
                "src/main/resources/application-dev.properties": (
                    "db.liquibase.change-log=classpath:/db/changelog/dev-only.xml\n"
                )
            }
        )
    )

    assert result.status is DiscoveryStatus.NOT_FOUND


# --- 2. whitespace ----------------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        "db.liquibase.change-log=classpath:/db/changelog/master.xml",
        "db.liquibase.change-log = classpath:/db/changelog/master.xml",
        "  db.liquibase.change-log   =   classpath:/db/changelog/master.xml  ",
        "\tdb.liquibase.change-log=classpath:/db/changelog/master.xml\t",
    ],
)
def test_whitespace_around_the_key_and_value_is_tolerated(line: str) -> None:
    result = discover_liquibase_changelog(
        reader({"src/main/resources/application.properties": f"{line}\n"})
    )

    assert result.status is DiscoveryStatus.DISCOVERED
    assert result.changelog_path == "src/main/resources/db/changelog/master.xml"


# --- 3. comments --------------------------------------------------------------------


def test_hash_and_bang_comments_and_blank_lines_are_skipped() -> None:
    text = (
        "# a leading comment\n"
        "\n"
        "! also a comment in Java properties\n"
        "server.port=8080\n"
        "\n"
        "db.liquibase.change-log=classpath:/db/changelog/master.xml\n"
        "# trailing comment\n"
    )

    result = discover_liquibase_changelog(
        reader({"src/main/resources/application.properties": text})
    )

    assert result.status is DiscoveryStatus.DISCOVERED
    assert result.changelog_path == "src/main/resources/db/changelog/master.xml"


# --- 4. YAML discovery ---------------------------------------------------------------


def test_discovers_the_nested_yaml_block_form() -> None:
    text = (
        "spring:\n"
        "  application:\n"
        "    name: example\n"
        "db:\n"
        "  liquibase:\n"
        "    change-log: classpath:/db/changelog/master.xml\n"
    )

    result = discover_liquibase_changelog(reader({"src/main/resources/application.yml": text}))

    assert result.status is DiscoveryStatus.DISCOVERED
    assert result.changelog_path == "src/main/resources/db/changelog/master.xml"
    assert result.source_file == "src/main/resources/application.yml"


def test_discovers_the_compact_dotted_yaml_key_form() -> None:
    text = "db.liquibase.change-log: classpath:/db/changelog/master.xml\n"

    result = discover_liquibase_changelog(reader({"src/main/resources/application.yaml": text}))

    assert result.status is DiscoveryStatus.DISCOVERED
    assert result.changelog_path == "src/main/resources/db/changelog/master.xml"


def test_yaml_sibling_keys_at_the_same_indent_do_not_leak_into_the_path() -> None:
    # `liquibase` appears twice at different nesting depths; only the real
    # `db.liquibase.change-log` path may match.
    text = (
        "logging:\n"
        "  level:\n"
        "    liquibase: DEBUG\n"
        "db:\n"
        "  liquibase:\n"
        "    change-log: classpath:/db/changelog/master.xml\n"
    )

    result = discover_liquibase_changelog(reader({"src/main/resources/application.yml": text}))

    assert result.status is DiscoveryStatus.DISCOVERED
    assert result.changelog_path == "src/main/resources/db/changelog/master.xml"


def test_a_yaml_comment_does_not_confuse_the_walk() -> None:
    text = (
        "db:\n"
        "  # which changelog to run\n"
        "  liquibase:\n"
        "    change-log: classpath:/db/changelog/master.xml # the root file\n"
    )

    result = discover_liquibase_changelog(reader({"src/main/resources/application.yml": text}))

    assert result.status is DiscoveryStatus.DISCOVERED
    assert result.changelog_path == "src/main/resources/db/changelog/master.xml"


def test_properties_files_are_preferred_over_yaml_when_both_declare_the_same_value() -> None:
    result = discover_liquibase_changelog(
        reader(
            {
                "src/main/resources/application.properties": (
                    "db.liquibase.change-log=classpath:/db/changelog/master.xml\n"
                ),
                "src/main/resources/application.yml": (
                    "db:\n  liquibase:\n    change-log: classpath:/db/changelog/master.xml\n"
                ),
            }
        )
    )

    assert result.status is DiscoveryStatus.DISCOVERED
    assert result.source_file == "src/main/resources/application.properties"


# --- 5. classpath resolution ---------------------------------------------------------


def test_classpath_prefixed_values_resolve_under_the_configs_own_resource_root() -> None:
    result = discover_liquibase_changelog(
        reader(
            {
                "service-a/src/main/resources/application.properties": (
                    "db.liquibase.change-log=classpath:/db/changelog/master.xml\n"
                )
            },
        ),
        candidate_paths=("service-a/src/main/resources/application.properties",),
    )

    assert result.changelog_path == "service-a/src/main/resources/db/changelog/master.xml"


def test_a_leading_slash_without_the_classpath_prefix_is_also_stripped() -> None:
    result = discover_liquibase_changelog(
        reader(
            {
                "src/main/resources/application.properties": (
                    "db.liquibase.change-log=/db/changelog/master.xml\n"
                )
            }
        )
    )

    assert result.changelog_path == "src/main/resources/db/changelog/master.xml"


def test_backslashes_are_normalized_to_forward_slashes() -> None:
    result = discover_liquibase_changelog(
        reader(
            {
                "src/main/resources/application.properties": (
                    "db.liquibase.change-log=classpath:\\db\\changelog\\master.xml\n"
                )
            }
        )
    )

    assert result.changelog_path == "src/main/resources/db/changelog/master.xml"


# --- 6. repository-relative resolution -------------------------------------------------


def test_a_bare_value_with_no_classpath_prefix_resolves_the_same_as_classpath() -> None:
    """Liquibase itself treats an unprefixed reference as classpath-relative."""
    result = discover_liquibase_changelog(
        reader(
            {
                "src/main/resources/application.properties": (
                    "db.liquibase.change-log=db/changelog/master.xml\n"
                )
            }
        )
    )

    assert result.changelog_path == "src/main/resources/db/changelog/master.xml"


def test_a_value_already_naming_the_resource_root_is_used_as_is() -> None:
    result = discover_liquibase_changelog(
        reader(
            {
                "src/main/resources/application.properties": (
                    "db.liquibase.change-log=src/main/resources/db/changelog/master.xml\n"
                )
            }
        )
    )

    assert result.changelog_path == "src/main/resources/db/changelog/master.xml"


def test_a_repo_root_config_resolves_a_bare_value_relative_to_the_repo_root() -> None:
    result = discover_liquibase_changelog(
        reader({"application.properties": "db.liquibase.change-log=db/changelog/master.xml\n"})
    )

    assert result.changelog_path == "db/changelog/master.xml"


def test_a_quoted_value_has_its_quotes_stripped() -> None:
    result = discover_liquibase_changelog(
        reader(
            {
                "src/main/resources/application.properties": (
                    'db.liquibase.change-log="classpath:/db/changelog/master.xml"\n'
                )
            }
        )
    )

    assert result.changelog_path == "src/main/resources/db/changelog/master.xml"


# --- 7. missing configuration -----------------------------------------------------------


def test_no_candidate_file_exists_at_all() -> None:
    result = discover_liquibase_changelog(reader({}))

    assert result.status is DiscoveryStatus.NOT_FOUND
    assert result.changelog_path is None
    assert PROPERTY_KEY in (result.reason or "")


# --- 8. missing key -----------------------------------------------------------------------


def test_the_configuration_file_exists_but_never_mentions_the_key() -> None:
    result = discover_liquibase_changelog(
        reader(
            {
                "src/main/resources/application.properties": "server.port=8080\nspring.application.name=svc\n"
            }
        )
    )

    assert result.status is DiscoveryStatus.NOT_FOUND


def test_the_spring_autoconfiguration_key_is_not_mistaken_for_the_project_one() -> None:
    result = discover_liquibase_changelog(
        reader(
            {
                "src/main/resources/application.properties": (
                    "spring.liquibase.change-log=classpath:/db/changelog/wrong.xml\n"
                )
            }
        )
    )

    assert result.status is DiscoveryStatus.NOT_FOUND


# --- 9. malformed configuration ----------------------------------------------------------


def test_a_blank_value_is_invalid_not_discovered() -> None:
    result = discover_liquibase_changelog(
        reader({"src/main/resources/application.properties": "db.liquibase.change-log=\n"})
    )

    assert result.status is DiscoveryStatus.INVALID
    assert result.source_file == "src/main/resources/application.properties"


def test_a_whitespace_only_value_is_also_invalid() -> None:
    result = discover_liquibase_changelog(
        reader({"src/main/resources/application.properties": "db.liquibase.change-log=   \n"})
    )

    assert result.status is DiscoveryStatus.INVALID


def test_garbage_content_that_matches_nothing_is_not_found_not_a_crash() -> None:
    result = discover_liquibase_changelog(
        reader(
            {"src/main/resources/application.properties": "\x00\x01 not a real properties file {{{"}
        )
    )

    assert result.status is DiscoveryStatus.NOT_FOUND


# --- 10/18. ambiguity ---------------------------------------------------------------------


def test_two_configs_declaring_different_changelogs_is_ambiguous() -> None:
    result = discover_liquibase_changelog(
        reader(
            {
                "src/main/resources/application.properties": (
                    "db.liquibase.change-log=classpath:/db/changelog/a.xml\n"
                ),
                "src/main/resources/application.yml": (
                    "db:\n  liquibase:\n    change-log: classpath:/db/changelog/b.xml\n"
                ),
            }
        )
    )

    assert result.status is DiscoveryStatus.AMBIGUOUS
    assert result.changelog_path is None
    assert "application.properties" in (result.reason or "")
    assert "application.yml" in (result.reason or "")


def test_two_configs_agreeing_on_the_same_changelog_is_not_ambiguous() -> None:
    result = discover_liquibase_changelog(
        reader(
            {
                "src/main/resources/application.properties": (
                    "db.liquibase.change-log=classpath:/db/changelog/a.xml\n"
                ),
                "src/main/resources/application.yml": (
                    "db:\n  liquibase:\n    change-log: classpath:/db/changelog/a.xml\n"
                ),
            }
        )
    )

    assert result.status is DiscoveryStatus.DISCOVERED
    assert result.changelog_path == "src/main/resources/db/changelog/a.xml"


# --- Read failures are ordinary "not found", never propagated ------------------------------


def test_a_read_failure_for_one_candidate_does_not_stop_the_others() -> None:
    def read(path: str) -> str:
        if path == "src/main/resources/application.properties":
            raise RuntimeError("simulated GitHub failure")
        if path == "src/main/resources/application.yml":
            return "db:\n  liquibase:\n    change-log: classpath:/db/changelog/master.xml\n"
        raise FileNotFoundError(path)

    result = discover_liquibase_changelog(read)

    assert result.status is DiscoveryStatus.DISCOVERED
    assert result.changelog_path == "src/main/resources/db/changelog/master.xml"


def test_every_candidate_failing_is_not_found_not_a_raised_exception() -> None:
    def read(path: str) -> str:
        raise RuntimeError("simulated GitHub failure")

    result = discover_liquibase_changelog(read)

    assert result.status is DiscoveryStatus.NOT_FOUND
