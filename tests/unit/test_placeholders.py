"""Placeholder-state tests.

These assert that every pipeline module imports cleanly and that its entry points
are still unimplemented. As each stage lands, replace the corresponding case with
real behavioural tests.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from queryfix.db import provision, session
from queryfix.integrations import claude, github
from queryfix.models import ExtractedQuery, Provenance, QueryKind
from queryfix.pipeline import (
    explain,
    hypopg,
    static_rules,
)

UNIMPLEMENTED: list[tuple[Callable[..., Any], tuple[Any, ...]]] = [
    (explain.explain_analyze, (None, None)),
    (explain.analyze_plan, (None, {})),
    (hypopg.simulate_indexes, (None, None, {})),
    (claude.request_findings, ("sys", "user")),
]


@pytest.mark.parametrize(
    ("func", "args"),
    UNIMPLEMENTED,
    ids=[f"{f.__module__}.{f.__name__}" for f, _ in UNIMPLEMENTED],
)
def test_entry_point_is_not_implemented_yet(
    func: Callable[..., Any], args: tuple[Any, ...]
) -> None:
    with pytest.raises(NotImplementedError):
        func(*args)


@pytest.mark.parametrize(
    "context_manager",
    [provision.provision_reference_db, session.rollback_transaction],
    ids=["provision_reference_db", "rollback_transaction"],
)
def test_context_manager_stage_is_not_implemented_yet(
    context_manager: Callable[..., Any],
) -> None:
    # These are @contextmanager generators, so the body does not run until the
    # `with` is entered — calling them alone raises nothing.
    with pytest.raises(NotImplementedError), context_manager(None):
        pass  # pragma: no cover


def test_rule_registry_is_populated_by_importing_the_package() -> None:
    # Registration happens as an import side effect, so a rule module that nobody
    # imports is a rule that silently never runs. This is the guard for that.
    assert sorted(rule.rule_id for rule in static_rules.RULES) == [
        "missing-where",
        "no-limit",
        "non-sargable",
        "select-star",
        "unindexed-filter",
    ]


def test_comment_marker_is_stable() -> None:
    # The marker is what makes the PR comment idempotent — changing it orphans
    # every comment QueryFix has already posted.
    assert github.COMMENT_MARKER == "<!-- queryfix:report -->"


def test_default_model_is_the_documented_one() -> None:
    assert claude.MODEL == "claude-opus-5"


def test_extracted_query_records_parse_failures() -> None:
    query = ExtractedQuery(
        id="q1",
        kind=QueryKind.RAW_SQL,
        text="SELECT FROM WHERE",
        provenance=Provenance(file="migrations/001.sql", line=3),
        parse_error="unexpected token",
    )

    assert query.normalized is None
    assert query.parse_error == "unexpected token"


def test_models_are_re_exported_at_package_level() -> None:
    # Callers import from `queryfix.models`; the split across query/finding/
    # report modules is an authoring detail they should not have to track.
    from queryfix.models import Finding, Report, Severity, Suggestion

    assert Finding.__name__ == "Finding"
    assert Report.__name__ == "Report"
    assert Severity.CRITICAL.value == "critical"
    assert Suggestion.__name__ == "Suggestion"
