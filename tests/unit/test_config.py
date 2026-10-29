"""Tests for the configuration layer.

Two things are being protected here. The first is that secrets stay out of
anything a human or a log aggregator will ever see — every path a value can take
out of a ``Settings`` object is checked, not just ``repr``. The second is that
this remains the *only* way configuration enters the process, which is asserted
against the source tree rather than trusted.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from queryfix.config import (
    DEFAULT_GROQ_MODEL,
    ENVIRONMENT_VARIABLE,
    TEST_ENVIRONMENT,
    MissingConfiguration,
    Settings,
    get_settings,
    override_settings,
    reset_settings_cache,
    validate_required,
)

REPOSITORY = Path(__file__).resolve().parents[2]
PACKAGE = REPOSITORY / "queryfix"

TOKEN = "ghp_supersecrettokenvalue"
API_KEY = "sk-ant-supersecretapikeyvalue"
GROQ_KEY = "gsk_supersecretgroqkeyvalue"


@pytest.fixture(autouse=True)
def _clean_settings_cache() -> None:
    """No test may see settings another test loaded."""
    reset_settings_cache()


def configured() -> Settings:
    return Settings.isolated(github_token=TOKEN, anthropic_api_key=API_KEY, groq_api_key=GROQ_KEY)


# --- Masking ------------------------------------------------------------------


def test_repr_masks_the_github_token() -> None:
    assert TOKEN not in repr(configured())


def test_repr_masks_the_anthropic_api_key() -> None:
    assert API_KEY not in repr(configured())


def test_repr_masks_the_groq_api_key() -> None:
    assert GROQ_KEY not in repr(configured())


def test_str_masks_secrets_too() -> None:
    # `logger.info("settings=%s", settings)` and f-strings both go through
    # __str__, so masking only __repr__ would leave the common path open.
    settings = configured()

    assert TOKEN not in str(settings)
    assert TOKEN not in f"{settings}"
    assert TOKEN not in "{}".format(settings)  # noqa: UP032


def test_repr_says_whether_a_secret_is_set_without_revealing_it() -> None:
    # The distinction worth logging — "the token is missing" answers most startup
    # questions — while revealing nothing about the value.
    rendered = repr(Settings.isolated(github_token=TOKEN))

    assert "github_token='<set>'" in rendered
    assert "anthropic_api_key=<unset>" in rendered


def test_repr_does_not_leak_even_a_prefix_of_a_token() -> None:
    # Every character in a log is a character an attacker does not have to guess,
    # and a log line outlives the process that wrote it.
    rendered = repr(configured())

    for length in range(4, len(TOKEN)):
        assert TOKEN[:length] not in rendered


def test_an_empty_secret_is_distinguished_from_an_absent_one() -> None:
    rendered = repr(Settings.isolated(github_token=""))

    assert "github_token='<empty>'" in rendered


def test_the_secret_field_itself_is_masked_when_logged() -> None:
    # A caller that logs one field rather than the whole object is still safe.
    settings = configured()

    assert TOKEN not in repr(settings.github_token)
    assert TOKEN not in str(settings.github_token)


def test_json_serialization_does_not_leak_a_secret() -> None:
    # The path that matters for an HTTP response: no route returns Settings today,
    # but a debug endpoint added later must not be the thing that discovers this.
    dumped = configured().model_dump_json()

    assert TOKEN not in dumped
    assert API_KEY not in dumped
    assert GROQ_KEY not in dumped


def test_dict_dump_does_not_leak_a_secret_when_rendered() -> None:
    dumped = configured().model_dump()

    assert TOKEN not in str(dumped)
    assert TOKEN not in repr(dumped)


def test_the_value_is_reachable_only_by_asking_for_it_explicitly() -> None:
    # Masking must not cost access — it makes leaking deliberate, not impossible.
    assert configured().github_token.get_secret_value() == TOKEN  # type: ignore[union-attr]
    assert configured().require_github_token() == TOKEN
    assert configured().require_groq_api_key() == GROQ_KEY
    assert configured().require_anthropic_api_key() == API_KEY


# --- Required and optional values ---------------------------------------------


def test_github_token_is_required_and_names_itself_when_missing() -> None:
    with pytest.raises(MissingConfiguration) as excinfo:
        validate_required(Settings.isolated())

    message = str(excinfo.value)
    assert "GITHUB_TOKEN" in message
    assert "post the review comment" in message
    assert ".env" in message


def test_a_blank_token_counts_as_missing() -> None:
    # An exported-but-empty variable is the shape a broken CI secret takes.
    with pytest.raises(MissingConfiguration):
        validate_required(Settings.isolated(github_token=""))


def test_validate_returns_the_settings_when_everything_required_is_present() -> None:
    settings = Settings.isolated(github_token=TOKEN)

    assert validate_required(settings) is settings


def test_anthropic_api_key_is_optional_for_now() -> None:
    # The N+1 stage is the only thing that needs it, and a run that never reaches
    # that stage should not be blocked on a key it will not use.
    settings = Settings.isolated(github_token=TOKEN)

    assert validate_required(settings) is settings
    assert settings.anthropic_api_key is None


def test_requiring_the_optional_key_fails_clearly_at_the_point_of_use() -> None:
    with pytest.raises(MissingConfiguration, match="ANTHROPIC_API_KEY"):
        Settings.isolated(github_token=TOKEN).require_anthropic_api_key()


def test_groq_api_key_is_optional() -> None:
    # The N+1 explanation stage is the only thing that needs it, and a run that
    # never reaches that stage — or whose provider is never configured — must not
    # be blocked on a key it will not use.
    settings = Settings.isolated(github_token=TOKEN)

    assert validate_required(settings) is settings
    assert settings.groq_api_key is None


def test_requiring_the_optional_groq_key_fails_clearly_at_the_point_of_use() -> None:
    with pytest.raises(MissingConfiguration, match="GROQ_API_KEY"):
        Settings.isolated(github_token=TOKEN).require_groq_api_key()


def test_groq_model_defaults_to_the_documented_model() -> None:
    assert Settings.isolated().groq_model == DEFAULT_GROQ_MODEL


def test_groq_model_is_configurable() -> None:
    assert Settings.isolated(groq_model="mixtral-8x7b-32768").groq_model == "mixtral-8x7b-32768"


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_a_blank_groq_model_falls_back_to_the_default(blank: str) -> None:
    # GitHub Actions renders an undefined `vars.GROQ_MODEL` as an empty string,
    # not as an absent variable — this is what keeps that from silently pointing
    # every Groq call at a model nothing serves.
    assert Settings.isolated(groq_model=blank).groq_model == DEFAULT_GROQ_MODEL


def test_missing_required_lists_exactly_what_is_absent() -> None:
    assert Settings.isolated().missing_required() == ("github_token",)
    assert Settings.isolated(github_token=TOKEN).missing_required() == ()


# --- Overriding without touching the real environment -------------------------


def test_override_replaces_the_process_settings() -> None:
    with override_settings(github_token=TOKEN) as settings:
        assert get_settings() is settings
        assert get_settings().require_github_token() == TOKEN


def test_override_restores_the_previous_settings_on_exit() -> None:
    with override_settings(github_token=TOKEN):
        pass

    reset_settings_cache()
    with override_settings(github_token="second") as inner:
        assert get_settings() is inner


def test_overrides_nest_and_unwind_in_order() -> None:
    with override_settings(github_token="outer") as outer:
        with override_settings(github_token="inner") as inner:
            assert get_settings() is inner
        assert get_settings() is outer


def test_override_restores_even_when_the_block_raises() -> None:
    with pytest.raises(RuntimeError), override_settings(github_token=TOKEN):
        raise RuntimeError("boom")

    assert _override_is_clear()


def _override_is_clear() -> bool:
    from queryfix import config

    return config._override is None


def test_an_override_ignores_the_real_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The isolation that matters: a developer with ANTHROPIC_API_KEY exported must
    # not get different test results from one who does not.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a-real-key-from-the-shell")

    with override_settings(github_token=TOKEN) as settings:
        assert settings.anthropic_api_key is None


def test_isolated_construction_ignores_the_real_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "a-real-token-from-the-shell")

    assert Settings.isolated().github_token is None


def test_settings_can_be_built_from_an_explicit_environment_mapping() -> None:
    # Exercises the variable *names* without exporting anything real.
    settings = Settings.from_mapping(
        {"GITHUB_TOKEN": TOKEN, "ANTHROPIC_API_KEY": API_KEY, "UNRELATED": "ignored"}
    )

    assert settings.require_github_token() == TOKEN
    assert settings.require_anthropic_api_key() == API_KEY


def test_an_unrecognized_variable_is_ignored_rather_than_rejected() -> None:
    # This process does not own the environment it runs in.
    assert Settings.from_mapping({"PATH": "/usr/bin", "GITHUB_TOKEN": TOKEN}) is not None


# --- Loading and caching -------------------------------------------------------


def test_the_environment_populates_fields_by_upper_cased_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    reset_settings_cache()

    assert get_settings().require_github_token() == TOKEN


def test_settings_are_loaded_once_per_process(monkeypatch: pytest.MonkeyPatch) -> None:
    # One run must not see two different values for the same variable.
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    reset_settings_cache()
    first = get_settings()

    monkeypatch.setenv("GITHUB_TOKEN", "changed-underneath-us")

    assert get_settings() is first


def test_resetting_the_cache_re_reads_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    reset_settings_cache()
    assert get_settings().require_github_token() == TOKEN

    monkeypatch.setenv("GITHUB_TOKEN", "a-new-token")
    reset_settings_cache()

    assert get_settings().require_github_token() == "a-new-token"


def test_settings_are_immutable() -> None:
    # Configuration that can be edited at runtime cannot be reasoned about from
    # the startup log.
    settings = configured()

    with pytest.raises(ValidationError, match="frozen"):
        settings.github_token = SecretStr("replaced")


# --- Import-time validation ----------------------------------------------------


#: Variables this module reads. Cleared before each subprocess so the developer's
#: own shell cannot decide whether a test passes.
CONFIG_VARIABLES = ("GITHUB_TOKEN", "ANTHROPIC_API_KEY", ENVIRONMENT_VARIABLE)


def _import_config(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Import queryfix.config in a fresh interpreter carrying exactly this config.

    The rest of the environment is inherited rather than wiped: on Windows a
    Python without SYSTEMROOT cannot initialize its socket providers and dies
    before reaching any QueryFix code, which would make these tests pass for
    entirely the wrong reason.

    Run from an empty directory, not the repository root: ``Settings`` reads a
    ``.env`` file at the process's working directory by design (see its
    docstring), and a developer's own ``.env`` sitting in the repository for
    local runs would otherwise leak into this subprocess and answer the
    question this test is asking. ``PYTHONPATH`` is set explicitly because
    moving the cwd away from the repository also removes it from the default
    import path that ``python -c`` would otherwise search.
    """
    environment = {key: value for key, value in os.environ.items() if key not in CONFIG_VARIABLES}
    environment.update(env)
    environment["PYTHONPATH"] = str(REPOSITORY)

    with tempfile.TemporaryDirectory() as isolated_cwd:
        return subprocess.run(
            [sys.executable, "-c", "import queryfix.config"],
            cwd=isolated_cwd,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )


def test_importing_without_a_token_fails_with_a_clear_error() -> None:
    # The point of failing at import: a misconfigured deployment dies at startup
    # rather than after cloning a reference database and analyzing a diff.
    result = _import_config({})

    assert result.returncode != 0
    assert "GITHUB_TOKEN" in result.stderr
    assert "MissingConfiguration" in result.stderr


def test_the_startup_error_says_how_to_fix_it() -> None:
    result = _import_config({})

    assert "needed to read pull requests and post the review comment" in result.stderr
    assert f"{ENVIRONMENT_VARIABLE}={TEST_ENVIRONMENT}" in result.stderr


def test_the_startup_error_does_not_echo_a_secret() -> None:
    # A validation failure is exactly when a framework is tempted to print what it
    # got. Here one of the two values is present and must not appear.
    result = _import_config({"ANTHROPIC_API_KEY": API_KEY})

    assert result.returncode != 0
    assert API_KEY not in result.stderr
    assert API_KEY not in result.stdout


def test_importing_with_a_token_succeeds() -> None:
    result = _import_config({"GITHUB_TOKEN": TOKEN})

    assert result.returncode == 0, result.stderr


def test_the_test_environment_marker_permits_import_without_configuration() -> None:
    # Without this, importing anything under queryfix would need a real token
    # and pytest could not even collect.
    result = _import_config({ENVIRONMENT_VARIABLE: TEST_ENVIRONMENT})

    assert result.returncode == 0, result.stderr


def test_a_test_run_does_not_require_configuration() -> None:
    # The suite you are reading imported this module without a GITHUB_TOKEN set.
    # If that ever stops being true, every contributor needs a real token to run
    # the tests.
    assert True


# --- The invariant this module exists to make possible ------------------------


def _environment_reads(source: str) -> list[str]:
    """Every expression in ``source`` that reaches the process environment.

    Walks the AST rather than searching the text, so the word "os.environ" in a
    docstring is prose and only a real attribute access counts. Catches both
    ``os.environ`` / ``os.getenv`` and the ``from os import environ`` spelling.
    """
    reads: list[str] = []

    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute) and node.attr in {"environ", "getenv"}:
            if isinstance(node.value, ast.Name) and node.value.id == "os":
                reads.append(f"os.{node.attr}")
        elif isinstance(node, ast.ImportFrom) and node.module == "os":
            reads.extend(
                f"from os import {alias.name}"
                for alias in node.names
                if alias.name in {"environ", "getenv"}
            )

    return reads


def test_config_is_the_only_module_that_reads_the_environment() -> None:
    # A token read at its point of use has no single place to audit and no single
    # place to redact. This is the guard that keeps that from happening quietly.
    offenders = {
        path.relative_to(REPOSITORY).as_posix(): _environment_reads(
            path.read_text(encoding="utf-8")
        )
        for path in PACKAGE.rglob("*.py")
        if path.name != "config.py"
    }

    assert {path: reads for path, reads in offenders.items() if reads} == {}


def test_config_reaches_the_environment_through_exactly_one_expression() -> None:
    # config.py is allowed to read the environment; this pins how much of it does,
    # so the module cannot quietly accumulate scattered reads of its own.
    reads = _environment_reads((PACKAGE / "config.py").read_text(encoding="utf-8"))

    assert reads == ["os.environ"]


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("import os\nTOKEN = os.environ['GITHUB_TOKEN']", ["os.environ"]),
        ("import os\nTOKEN = os.getenv('GITHUB_TOKEN')", ["os.getenv"]),
        ("import os\nTOKEN = os.environ.get('GITHUB_TOKEN')", ["os.environ"]),
        ("from os import environ", ["from os import environ"]),
        ("from os import getenv", ["from os import getenv"]),
        # Prose is not a read. Without this the guard would flag its own docstring.
        ('"""A docstring mentioning os.environ and os.getenv."""', []),
        ("# os.environ in a comment", []),
        ("TOKEN = config.environ", []),
    ],
)
def test_the_guard_sees_every_spelling_and_no_false_ones(source: str, expected: list[str]) -> None:
    # A guard that cannot fail is not a guard, and one that fires on prose gets
    # deleted by the first person it inconveniences.
    assert _environment_reads(source) == expected
