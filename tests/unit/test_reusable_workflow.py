"""Structural guards on the reusable workflow every consumer repository calls.

The workflow is the org-wide blast radius: one bad edit reaches every repository on
its next PR. These tests pin the properties that must not regress silently.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

WORKFLOW = Path(".github/workflows/queryfix-review.yml")


def _load() -> dict[Any, Any]:
    loaded: dict[Any, Any] = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return loaded


def _call() -> dict[str, Any]:
    # YAML 1.1 reads the bare key `on` as the boolean True.
    call: dict[str, Any] = _load()[True]["workflow_call"]
    return call


def _steps() -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = _load()["jobs"]["review"]["steps"]
    return steps


def test_no_run_script_interpolates_an_expression() -> None:
    # `${{ ... }}` inside a `run:` body is spliced into the shell text before it runs,
    # so a crafted input (a branch name, a ref) becomes command injection. Values must
    # reach the script through `env:` instead.
    for step in _steps():
        script = step.get("run")
        if script is not None:
            assert "${{" not in script, f"step {step.get('name')!r} interpolates into run:"


def test_source_repo_and_ref_are_configurable_inputs() -> None:
    inputs = _call()["inputs"]

    assert inputs["queryfix-repo"]["default"] == "pranavv05/QueryFix"
    assert "queryfix-ref" in inputs


def test_private_install_token_is_an_optional_secret() -> None:
    secrets = _call()["secrets"]

    assert secrets["queryfix-token"]["required"] is False
    assert secrets["groq-api-key"]["required"] is False


def test_disable_llm_defaults_off_and_passes_no_llm() -> None:
    inputs = _call()["inputs"]
    review = next(step for step in _steps() if step.get("name") == "Review pull request")

    assert inputs["disable-llm"]["default"] is False
    assert "--no-llm" in review["env"]["NO_LLM_FLAG"]
    assert "$NO_LLM_FLAG" in review["run"]


def test_token_permissions_stay_minimal() -> None:
    assert _load()["permissions"] == {"contents": "read", "pull-requests": "write"}


def test_advisory_input_defaults_off_and_reaches_the_cli_as_an_env_var() -> None:
    inputs = _call()["inputs"]
    review = next(step for step in _steps() if step.get("name") == "Review pull request")

    assert inputs["advisory"]["default"] is False
    assert inputs["advisory"]["type"] == "boolean"
    assert "inputs.advisory" in review["env"]["QUERYFIX_ADVISORY"]


def test_every_third_party_action_is_pinned_to_a_commit_sha() -> None:
    # A tag can be moved after the fact, and this workflow runs in every consumer
    # repository, so a moved tag would be an org-wide supply-chain hole.
    import re

    for workflow in Path(".github/workflows").glob("*.yml"):
        for line in workflow.read_text(encoding="utf-8").splitlines():
            match = re.search(r"uses:\s*(\S+)", line)
            if match is None or match.group(1).startswith("./"):
                continue
            action = match.group(1)
            if ".github/workflows/" in action:  # reusable workflow call (pinned by consumers)
                continue
            assert re.search(r"@[0-9a-f]{40}$", action), f"{workflow.name}: {action} is not pinned"


def test_the_review_job_cannot_hang_for_hours() -> None:
    job = _load()["jobs"]["review"]

    assert 1 <= job["timeout-minutes"] <= 30
