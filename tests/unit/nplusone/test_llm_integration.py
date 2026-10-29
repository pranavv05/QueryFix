"""End-to-end: detector -> finding -> explanation provider -> reconciliation -> report.

The provider here is a small in-memory fake implementing
:class:`~queryfix.integrations.llm.LLMExplanationProvider`, not Groq — the point
of this file is the *pipeline wiring* (whether ``detect_n_plus_one`` calls a
provider at the right time, with the right pairs, and reconciles what comes back
correctly), which is independent of which provider is plugged in. Groq's own
request/response handling — auth failures, timeouts, malformed JSON — is covered
in ``tests/unit/test_groq_integration.py`` against a fake Groq client instead.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

import pytest

from queryfix.integrations.p6spy import StatementGroup
from queryfix.models.finding import Finding, Severity
from queryfix.models.nplusone import NPlusOneCandidate, NPlusOneExplanation
from queryfix.models.query import SourceFile
from queryfix.pipeline.nplusone import (
    LAZY_ASSOCIATION_RULE_ID,
    REPOSITORY_CALL_RULE_ID,
    detect_n_plus_one,
)
from queryfix.pipeline.report import render_markdown
from queryfix.pipeline.runner import AnalysisRunner

Build = Callable[..., SourceFile]

LOOP = """
        for (Parent parent : parents) {
            thingRepository.findByParentId(parent.getId());
        }
"""
DERIVED = "List<Thing> findByParentId(Long parentId);"


def statement_group(
    *, count: int, variants: int, sql: str = "SELECT id FROM thing WHERE parent_id = ?"
) -> StatementGroup:
    return StatementGroup(
        normalized_sql=sql,
        count=count,
        distinct_variants=variants,
        total_elapsed_ms=count * 2,
        example_sql=sql.replace("?", "1"),
    )


class FakeExplanationProvider:
    """A minimal, in-memory :class:`LLMExplanationProvider`.

    Records every call it receives (``calls``) so a test can assert whether it
    was invoked at all — the thing CLAUDE.md's "don't call Groq for every query"
    requirement actually needs proven. ``answer`` decides what comes back: a
    fixed mapping, a callable computing one per call, or an exception to raise,
    which exercises the "provider fails outright" fallback path.
    """

    def __init__(
        self,
        answer: (
            Mapping[tuple[str, ...], NPlusOneExplanation]
            | Callable[
                [Sequence[tuple[NPlusOneCandidate, Finding]]],
                Mapping[tuple[str, ...], NPlusOneExplanation],
            ]
            | BaseException
            | None
        ) = None,
    ) -> None:
        self._answer = answer if answer is not None else {}
        self.calls: list[list[tuple[NPlusOneCandidate, Finding]]] = []

    def explain_nplusone(
        self, pairs: Sequence[tuple[NPlusOneCandidate, Finding]]
    ) -> Mapping[tuple[str, ...], NPlusOneExplanation]:
        self.calls.append(list(pairs))
        if isinstance(self._answer, BaseException):
            raise self._answer
        if callable(self._answer):
            return self._answer(pairs)
        return self._answer


def nplusone_findings(findings: list[Finding]) -> list[Finding]:
    return [
        finding
        for finding in findings
        if finding.rule_id in {REPOSITORY_CALL_RULE_ID, LAZY_ASSOCIATION_RULE_ID}
    ]


# --- The happy path --------------------------------------------------------------


def test_detector_finding_provider_reconciliation_end_to_end(
    service: Build, repository: Build
) -> None:
    provider = FakeExplanationProvider(
        answer=lambda pairs: {
            candidate.identity: NPlusOneExplanation(
                summary="This loop issues one query per parent.",
                reasoning="Each iteration depends on the current element.",
                remediation=("Batch the lookup with a single IN query.",),
            )
            for candidate, _ in pairs
        }
    )

    findings = detect_n_plus_one(
        [service(LOOP), repository(DERIVED)],
        explain=provider,
    )

    (finding,) = nplusone_findings(findings)
    assert "This loop issues one query per parent." in finding.explanation
    assert any(item.label == "Explanation source" for item in finding.evidence)
    assert finding.suggestions[-1].description == "Batch the lookup with a single IN query."
    assert len(provider.calls) == 1
    assert len(provider.calls[0]) == 1

    # The report renderer needs no change to handle an enriched finding — it is
    # still just a Finding with a longer explanation and one more suggestion.
    from queryfix.models.report import Report, RunContext

    report = Report(
        context=RunContext(run_id="r1", repo="acme/example", pr_number=1),
        findings=findings,
    )
    markdown = render_markdown(report)
    assert "This loop issues one query per parent." in markdown
    assert "Batch the lookup with a single IN query." in markdown


def test_multiple_n_plus_one_findings_each_get_their_own_explanation(
    service: Build, repository: Build
) -> None:
    second_repo = repository("List<Other> findByOtherId(Long id);", name="OtherRepository")
    second_loop = """
        for (Parent parent : parents) {
            otherRepository.findByOtherId(parent.getId());
        }
"""
    provider = FakeExplanationProvider(
        answer=lambda pairs: {
            candidate.identity: NPlusOneExplanation(summary=f"Explained {candidate.method_name}.")
            for candidate, _ in pairs
        }
    )

    findings = detect_n_plus_one(
        [
            service(
                LOOP + second_loop,
                fields=(
                    "    private final ThingRepository thingRepository;\n"
                    "    private final OtherRepository otherRepository;"
                ),
                constructor=(
                    "    public ExampleService(ThingRepository thingRepository, "
                    "OtherRepository otherRepository) {\n"
                    "        this.thingRepository = thingRepository;\n"
                    "        this.otherRepository = otherRepository;\n"
                    "    }"
                ),
                imports=(
                    "com.example.data.ThingRepository",
                    "com.example.data.OtherRepository",
                ),
            ),
            repository(DERIVED),
            second_repo,
        ],
        explain=provider,
    )

    matches = nplusone_findings(findings)
    assert len(matches) == 2
    assert {"Explained findByParentId.", "Explained findByOtherId."} == {
        finding.explanation.splitlines()[0] for finding in matches
    }
    # One provider call carrying both candidates, not one call per candidate.
    assert len(provider.calls) == 1
    assert len(provider.calls[0]) == 2


# --- "Do not call Groq for every query" -------------------------------------------


def test_no_n_plus_one_candidates_means_no_provider_call() -> None:
    provider = FakeExplanationProvider()

    findings = detect_n_plus_one([], explain=provider)

    assert findings == []
    assert provider.calls == []


def test_a_run_with_only_static_findings_never_calls_the_provider(service: Build) -> None:
    provider = FakeExplanationProvider()
    healthy = """
        thingRepository.findByParentId(1L);
"""

    findings = detect_n_plus_one([service(healthy)], explain=provider)

    assert nplusone_findings(findings) == []
    assert provider.calls == []


def test_no_provider_configured_is_the_same_as_no_provider_called(
    service: Build, repository: Build
) -> None:
    """The default: an ``AnalysisRunner`` built with no explicit provider."""
    report = AnalysisRunner().run(
        repo="acme/example", pr_number=1, sources=[service(LOOP), repository(DERIVED)]
    )

    (finding,) = nplusone_findings(report.findings)
    assert finding.evidence[-1].label == "Confidence tier"  # unchanged, no LLM enrichment


# --- Deterministic finding survives complete provider failure ---------------------


def test_deterministic_finding_survives_a_provider_that_raises(
    service: Build, repository: Build
) -> None:
    provider = FakeExplanationProvider(answer=RuntimeError("groq is down"))

    findings = detect_n_plus_one([service(LOOP), repository(DERIVED)], explain=provider)

    (finding,) = nplusone_findings(findings)
    assert finding.severity is Severity.HIGH
    assert "Explanation source" not in [item.label for item in finding.evidence]


def test_deterministic_finding_survives_an_empty_answer(service: Build, repository: Build) -> None:
    provider = FakeExplanationProvider(answer={})

    findings = detect_n_plus_one([service(LOOP), repository(DERIVED)], explain=provider)

    (finding,) = nplusone_findings(findings)
    assert "Explanation source" not in [item.label for item in finding.evidence]


# --- Reconciliation rejects contradictions, even from a "successful" call ---------


def test_an_explanation_inventing_a_foreign_file_is_discarded(
    service: Build, repository: Build
) -> None:
    provider = FakeExplanationProvider(
        answer=lambda pairs: {
            candidate.identity: NPlusOneExplanation(
                summary="The real bug is in PaymentService.java."
            )
            for candidate, _ in pairs
        }
    )

    findings = detect_n_plus_one([service(LOOP), repository(DERIVED)], explain=provider)

    (finding,) = nplusone_findings(findings)
    assert "PaymentService.java" not in finding.explanation
    assert "Explanation source" not in [item.label for item in finding.evidence]


def test_an_explanation_inventing_a_foreign_line_is_discarded(
    service: Build, repository: Build
) -> None:
    provider = FakeExplanationProvider(
        answer=lambda pairs: {
            candidate.identity: NPlusOneExplanation(
                summary=f"See {candidate.file.rsplit('/', 1)[-1]}:99999 for the loop."
            )
            for candidate, _ in pairs
        }
    )

    findings = detect_n_plus_one([service(LOOP), repository(DERIVED)], explain=provider)

    (finding,) = nplusone_findings(findings)
    assert "99999" not in finding.explanation


def test_an_invented_execution_count_without_runtime_evidence_is_discarded(
    service: Build, repository: Build
) -> None:
    provider = FakeExplanationProvider(
        answer=lambda pairs: {
            candidate.identity: NPlusOneExplanation(
                summary="This executed 5,000 times against the database."
            )
            for candidate, _ in pairs
        }
    )

    findings = detect_n_plus_one([service(LOOP), repository(DERIVED)], explain=provider)

    (finding,) = nplusone_findings(findings)
    assert "5,000 times" not in finding.explanation
    assert "Explanation source" not in [item.label for item in finding.evidence]


def test_runtime_backed_evidence_lets_the_explanation_describe_the_observed_count(
    service: Build, repository: Build
) -> None:
    provider = FakeExplanationProvider(
        answer=lambda pairs: {
            candidate.identity: NPlusOneExplanation(
                summary=f"This ran {candidate.runtime.count:,} times in the captured log."
            )
            for candidate, _ in pairs
            if candidate.runtime is not None
        }
    )

    findings = detect_n_plus_one(
        [service(LOOP), repository(DERIVED)],
        repeated_statements=[statement_group(count=5000, variants=5000)],
        explain=provider,
    )

    (finding,) = nplusone_findings(findings)
    assert "5,000 times in the captured log" in finding.explanation
    assert any(item.label == "Explanation source" for item in finding.evidence)


# --- Deterministic fields are never touched ---------------------------------------


def test_severity_confidence_and_provenance_survive_reconciliation(
    service: Build, repository: Build
) -> None:
    unenriched = nplusone_findings(detect_n_plus_one([service(LOOP), repository(DERIVED)]))[0]

    provider = FakeExplanationProvider(
        answer=lambda pairs: {
            candidate.identity: NPlusOneExplanation(
                summary="A per-parent lookup.",
                reasoning="Each iteration issues its own statement.",
                remediation=("Batch it.",),
            )
            for candidate, _ in pairs
        }
    )
    enriched = nplusone_findings(
        detect_n_plus_one([service(LOOP), repository(DERIVED)], explain=provider)
    )[0]

    assert enriched.severity is unenriched.severity
    assert enriched.confidence == unenriched.confidence
    assert enriched.rule_id == unenriched.rule_id
    assert enriched.provenance == unenriched.provenance
    assert enriched.query_id == unenriched.query_id
    assert enriched.query_ids == unenriched.query_ids


@pytest.mark.parametrize("field", ["severity", "confidence"])
def test_reconciliation_gives_the_model_no_channel_to_change_severity_or_confidence(
    field: str,
) -> None:
    assert field not in NPlusOneExplanation.model_fields
