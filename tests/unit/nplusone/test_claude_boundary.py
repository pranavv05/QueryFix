"""The boundary that keeps an LLM from deciding what is true.

Nothing here calls Claude — no client is constructed anywhere in the codebase yet.
These test the two pure functions that will wrap the eventual call: what the model
is shown, and what it is allowed to change. The design intent is that deterministic
evidence wins, and these are the tests that make that a property rather than a
promise.
"""

from __future__ import annotations

import pytest

from queryfix.integrations.claude import (
    ContradictedEvidence,
    build_nplusone_request,
    reconcile_nplusone_explanation,
    request_findings,
)
from queryfix.models.java_structure import ArgumentDependency, IterationKind
from queryfix.models.nplusone import (
    EvidenceTier,
    NPlusOneCandidate,
    NPlusOneExplanation,
    NPlusOneKind,
    RuntimeCorroboration,
)
from queryfix.pipeline.nplusone import render_candidate


def candidate(**overrides: object) -> NPlusOneCandidate:
    base: dict[str, object] = {
        "kind": NPlusOneKind.REPOSITORY_CALL_IN_LOOP,
        "tier": EvidenceTier.MEDIUM_HIGH,
        "file": "src/main/java/com/example/service/ExampleService.java",
        "line": 20,
        "enclosing_type": "ExampleService",
        "enclosing_method": "run",
        "repository_type": "ThingRepository",
        "method_name": "findByParentId",
        "query_id": "src/main/java/com/example/data/ThingRepository.java:findByParentId",
        "query_ids": ("src/main/java/com/example/data/ThingRepository.java:findByParentId",),
        "declaration_file": "src/main/java/com/example/data/ThingRepository.java",
        "declaration_line": 7,
        "iteration_kind": IterationKind.ENHANCED_FOR,
        "iteration_line": 19,
        "element_identifier": "parent",
        "loop_depth": 1,
        "dependency": ArgumentDependency.LOOP_ELEMENT_ARGUMENT,
        "dependency_detail": "`parent` flows into the argument list",
    }
    return NPlusOneCandidate(**{**base, **overrides})  # type: ignore[arg-type]


def test_the_request_carries_only_established_facts() -> None:
    payload = build_nplusone_request(candidate())

    assert payload["evidence_tier"] == "medium-high"
    assert payload["evidence_is_runtime_backed"] is False
    # No runtime section at all, rather than an empty one a model could fill in.
    assert "runtime" not in payload
    assert "instructions" in payload


def test_the_request_includes_runtime_facts_when_they_exist() -> None:
    payload = build_nplusone_request(
        candidate(
            tier=EvidenceTier.VERY_HIGH,
            runtime=RuntimeCorroboration(
                normalized_sql="SELECT id FROM thing WHERE parent_id = %s",
                count=5000,
                distinct_variants=5000,
                total_elapsed_ms=9000,
                matched_on="statement reads `thing`",
            ),
        )
    )

    runtime = payload["runtime"]
    assert isinstance(runtime, dict)
    assert runtime["execution_count"] == 5000


def test_an_explanation_cannot_move_the_finding() -> None:
    original = render_candidate(candidate())
    merged = reconcile_nplusone_explanation(
        original,
        candidate(),
        NPlusOneExplanation(
            summary="This loop queries once per parent.",
            reasoning="Each iteration issues its own statement.",
            remediation=("Batch the lookup.",),
        ),
    )

    # Every deterministic field is byte-identical to what the detector produced.
    assert merged.rule_id == original.rule_id
    assert merged.severity is original.severity
    assert merged.provenance == original.provenance
    assert merged.query_id == original.query_id
    assert merged.query_ids == original.query_ids
    assert merged.confidence == original.confidence
    # The detector's own checkable sentence survives alongside the prose.
    assert original.explanation in merged.explanation
    assert "This loop queries once per parent." in merged.explanation
    assert merged.suggestions[-1].description == "Batch the lookup."


def test_the_merged_finding_says_who_wrote_the_prose() -> None:
    merged = reconcile_nplusone_explanation(
        render_candidate(candidate()),
        candidate(),
        NPlusOneExplanation(summary="A per-parent lookup."),
    )

    assert any(item.label == "Explanation source" for item in merged.evidence)


def test_an_invented_execution_count_is_rejected() -> None:
    with pytest.raises(ContradictedEvidence, match="measured execution count"):
        reconcile_nplusone_explanation(
            render_candidate(candidate()),
            candidate(),
            NPlusOneExplanation(
                summary="This executed 5,000 times against the database.",
            ),
        )


def test_an_execution_count_is_allowed_when_a_log_backs_it() -> None:
    backed = candidate(
        tier=EvidenceTier.VERY_HIGH,
        runtime=RuntimeCorroboration(
            normalized_sql="SELECT id FROM thing WHERE parent_id = %s",
            count=5000,
            distinct_variants=5000,
            total_elapsed_ms=9000,
            matched_on="statement reads `thing`",
        ),
    )

    merged = reconcile_nplusone_explanation(
        render_candidate(backed),
        backed,
        NPlusOneExplanation(summary="This ran 5,000 times in the captured log."),
    )

    assert "5,000 times" in merged.explanation


def test_an_invented_source_file_is_rejected() -> None:
    with pytest.raises(ContradictedEvidence, match="not this candidate's source file"):
        reconcile_nplusone_explanation(
            render_candidate(candidate()),
            candidate(),
            NPlusOneExplanation(summary="The loop in PaymentService.java is the problem."),
        )


def test_an_invented_line_number_is_rejected() -> None:
    with pytest.raises(ContradictedEvidence, match="not a line this candidate spans"):
        reconcile_nplusone_explanation(
            render_candidate(candidate()),
            candidate(),
            NPlusOneExplanation(summary="See ExampleService.java:999 for the loop."),
        )


def test_naming_the_candidates_own_files_is_permitted() -> None:
    merged = reconcile_nplusone_explanation(
        render_candidate(candidate()),
        candidate(),
        NPlusOneExplanation(
            summary="ExampleService.java:20 calls a method declared in ThingRepository.java.",
        ),
    )

    assert "ExampleService.java:20" in merged.explanation


def test_an_empty_explanation_leaves_the_finding_untouched() -> None:
    original = render_candidate(candidate())
    merged = reconcile_nplusone_explanation(original, candidate(), NPlusOneExplanation(summary=" "))

    assert merged.explanation == original.explanation


def test_the_explanation_model_has_no_location_fields() -> None:
    """The structural reason a model cannot relocate a finding."""
    forbidden = {"file", "line", "query", "query_id", "count", "provenance", "severity"}

    assert forbidden.isdisjoint(NPlusOneExplanation.model_fields)


def test_claude_is_still_not_wired() -> None:
    """Detection is deterministic; no model call exists in this phase."""
    with pytest.raises(NotImplementedError):
        request_findings("system", "user")
