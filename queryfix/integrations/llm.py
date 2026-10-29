"""The provider-neutral LLM boundary: what any explanation provider may see and say.

This is where CLAUDE.md's rule about LLM findings being *claims, not facts* becomes
enforceable code rather than a convention every provider has to remember on its own.
Two things live here, and every provider — Groq today, Claude or anything else
later — is built on top of them rather than reimplementing them:

* :func:`build_nplusone_request` — the structured payload a provider is shown.
  Only established facts, each labelled with how it was established.
* :func:`reconcile_nplusone_explanation` — the merge that lets prose *describe* a
  finding without ever letting it *move* one. Every deterministic field on the
  :class:`~queryfix.models.finding.Finding` survives untouched; the model may
  only add a sentence ahead of the detector's own explanation and suggestions
  after the ones the detector already derived.

:class:`LLMExplanationProvider` is the seam a caller drives against.  It is a
:class:`typing.Protocol`, not a base class, so nothing has to inherit from it —
:class:`queryfix.integrations.groq.GroqExplanationProvider` satisfies it today
by having the right method, and a future ``ClaudeExplanationProvider`` built on
:mod:`queryfix.integrations.claude` would satisfy it the same way. The stage that
drives one (:func:`queryfix.pipeline.nplusone.detect_n_plus_one`) is written
against this Protocol and against nothing else, which is what makes swapping the
provider a change to *one* call site rather than to the detector.

Nothing in this module makes a network call. A provider decides how to obtain an
:class:`~queryfix.models.nplusone.NPlusOneExplanation`; this module only decides
what it is allowed to do once it has one.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Protocol

from queryfix.models.finding import Evidence, Finding, Suggestion
from queryfix.models.nplusone import NPlusOneCandidate, NPlusOneExplanation

__all__ = [
    "NPLUSONE_SYSTEM_PROMPT",
    "ContradictedEvidence",
    "LLMExplanationProvider",
    "build_nplusone_request",
    "reconcile_nplusone_explanation",
]

#: Phrases that assert an observed execution count. Permitted only when a
#: statement log actually backed the candidate — otherwise the model has invented
#: a measurement, which is the single most damaging thing it could contribute to a
#: performance review, because it is unfalsifiable by the reader.
_RUNTIME_CLAIM = re.compile(
    r"\b(?:executed|ran|issued|fired|performed)\b[^.]{0,40}\b\d[\d,]*\s*(?:times|queries)\b"
    r"|\b\d[\d,]*\s*(?:database\s+)?(?:round[- ]trips?|queries)\s+(?:were|was)\s+"
    r"(?:executed|issued|observed|measured)\b"
    r"|\bmeasured\b[^.]{0,30}\b\d[\d,]*\b",
    re.IGNORECASE,
)

#: The strict system prompt every N+1 explanation provider is expected to send.
#: Shared rather than duplicated per provider, so the enforcement stance stated
#: here and the enforcement code in :func:`reconcile_nplusone_explanation` cannot
#: drift into two different descriptions of the same boundary.
NPLUSONE_SYSTEM_PROMPT = """\
You are an explanation layer for QueryFix, an automated database-query reviewer.

The deterministic analysis supplied by the application is authoritative. You are \
given structured evidence about one candidate N+1 query pattern that a static \
analyzer already found: a call site, a loop, a repository method, and whatever a \
runtime statement log corroborated.

You MUST NOT invent facts. You MUST ONLY use facts present in the supplied \
evidence. You MUST NOT change or introduce:
- severity
- confidence
- file
- line
- query identity
- execution count
- runtime evidence
- detection status (whether this is or is not an N+1 — that has already been decided)

If the supplied evidence contains no runtime section, no query was observed \
running: describe the pattern as a *potential* N+1 pattern, never as a measured \
one. Do not say a query "executed", "ran", or "fired" any number of times. If a \
runtime section is present, you may describe what it recorded, using only the \
numbers supplied.

Respond with a JSON object with exactly these fields: `summary` (one sentence \
naming the pattern), `why_it_matters` (the performance consequence, grounded in \
the supplied evidence), `evidence_explanation` (what the supplied evidence shows \
and how it supports the finding), and `recommendation` (a concrete remediation). \
Every sentence must be traceable to a fact you were given — do not add anything \
that is not in the supplied evidence.\
"""


class LLMExplanationProvider(Protocol):
    """Anything that can turn N+1 candidates into prose, without deciding facts.

    A structural Protocol rather than an abstract base class: a provider satisfies
    this by implementing the method, with no inheritance required. That is what
    lets :class:`queryfix.integrations.groq.GroqExplanationProvider` be swapped
    for a future Claude-backed provider without either one depending on the other,
    and without the detector depending on either.
    """

    def explain_nplusone(
        self, pairs: Sequence[tuple[NPlusOneCandidate, Finding]]
    ) -> Mapping[tuple[str, ...], NPlusOneExplanation]:
        """Best-effort explanations for ``pairs``, keyed by ``candidate.identity``.

        ``pairs`` carries each candidate alongside the :class:`Finding` the
        detector already rendered from it — the same deterministic prose,
        evidence, and suggestions a reviewer would see with no provider
        configured at all — so a provider can write richer text without being
        handed anything the detector did not already establish.

        Must never raise for a provider-side failure: authentication, rate
        limiting, a network error, or a malformed response are all "no
        explanation for this one", not an exception. A candidate absent from the
        returned mapping simply keeps its deterministic finding. Implementations
        are free to omit some or all pairs; the empty mapping is a legitimate
        answer to "the provider is unavailable", not an error.
        """
        ...


class ContradictedEvidence(ValueError):
    """An explanation asserted something the deterministic evidence does not support.

    Raised rather than silently dropped so a prompt regression is loud in tests
    instead of quietly degrading every report in production.
    """


def build_nplusone_request(
    candidate: NPlusOneCandidate, *, finding: Finding | None = None
) -> dict[str, object]:
    """The structured payload describing one candidate to a provider.

    Only established facts, and each one labelled with how it was established.
    ``runtime`` is absent rather than null-with-a-note when no log corroborated the
    candidate: a field that is present and empty invites a model to fill it in,
    and a field that is absent cannot be reasoned about at all.

    ``finding`` is the :class:`Finding` the detector already rendered from
    ``candidate`` — optional, and additive only. When supplied, its rule ID,
    title, severity, confidence, evidence, and suggestions are included under a
    ``finding`` key so a provider can write prose that is consistent with what the
    report already says, rather than reasoning about the candidate in isolation.
    Every one of those values is itself deterministic — :func:`Finding` is a pure
    rendering of ``candidate`` — so including it opens no new channel a model
    could use to assert something unverified.
    """
    payload: dict[str, object] = {
        "pattern": candidate.kind.value,
        "evidence_tier": candidate.tier.value,
        "evidence_is_runtime_backed": candidate.tier.is_runtime_backed,
        "location": {
            "file": candidate.file,
            "line": candidate.line,
            "enclosing_type": candidate.enclosing_type,
            "enclosing_method": candidate.enclosing_method,
        },
        "call": {
            "repository_type": candidate.repository_type,
            "method_name": candidate.method_name,
            "repository_declaration_resolved": candidate.repository_resolved,
        },
        "iteration": {
            "kind": candidate.iteration_kind.value,
            "opens_at_line": candidate.iteration_line,
            "iterates_over": candidate.iterable_text,
            "element_identifier": candidate.element_identifier,
            "nesting_depth": candidate.loop_depth,
        },
        "argument_dependency": {
            "kind": candidate.dependency.value,
            "detail": candidate.dependency_detail,
        },
    }

    if candidate.query_id is not None:
        payload["query"] = {
            "query_id": candidate.query_id,
            "declared_in": candidate.declaration_file,
            "declared_at_line": candidate.declaration_line,
        }

    if candidate.runtime is not None:
        payload["runtime"] = {
            "normalized_sql": candidate.runtime.normalized_sql,
            "execution_count": candidate.runtime.count,
            "distinct_parameter_sets": candidate.runtime.distinct_variants,
            "total_elapsed_ms": candidate.runtime.total_elapsed_ms,
            "attribution": candidate.runtime.matched_on,
        }

    if finding is not None:
        payload["finding"] = {
            "rule_id": finding.rule_id,
            "title": finding.title,
            "severity": finding.severity.value,
            "confidence": finding.confidence,
            "evidence": [{"label": item.label, "detail": item.detail} for item in finding.evidence],
            "suggestions": [item.description for item in finding.suggestions],
        }

    payload["instructions"] = (
        "Explain the listed evidence for a reviewer. Do not introduce files, line "
        "numbers, queries, repository methods, or execution counts that do not "
        "appear above. If no runtime section is present, no query was observed "
        "running: describe the pattern as potential, never as measured."
    )
    return payload


def reconcile_nplusone_explanation(
    finding: Finding,
    candidate: NPlusOneCandidate,
    explanation: NPlusOneExplanation,
) -> Finding:
    """Merge model prose into a finding without letting it move the evidence.

    Every deterministic field survives untouched — ``rule_id``, ``severity``,
    ``provenance``, ``query_id``, ``query_ids``, ``confidence``, and the evidence
    list the detector built. The explanation may only add: a sentence ahead of the
    established explanation, and suggestions after the ones already derived from
    the pattern.

    Two checks are enforced, and both fail loudly:

    * Prose asserting an execution count is rejected unless a statement log
      actually backed this candidate.
    * Prose naming a file or line that is not this candidate's is rejected, which
      is the concrete form "do not invent locations" takes once a model is writing
      free text.
    """
    _reject_runtime_claims(candidate, explanation)
    _reject_foreign_locations(candidate, explanation)

    summary = explanation.summary.strip()
    reasoning = explanation.reasoning.strip()
    narrative = " ".join(part for part in (summary, reasoning) if part)

    return finding.model_copy(
        update={
            # Prepended, not substituted. The checkable sentence the detector wrote
            # stays in the report, so a reviewer can always compare the prose
            # against the fact it claims to describe.
            "explanation": (
                f"{narrative}\n\n{finding.explanation}" if narrative else finding.explanation
            ),
            "evidence": [
                *finding.evidence,
                Evidence(
                    label="Explanation source",
                    detail=(
                        "Narrative written from the deterministic evidence above by an "
                        "LLM explanation provider. The evidence, location, and confidence "
                        "tier were established by static analysis, not by the model."
                    ),
                ),
            ],
            "suggestions": [
                *finding.suggestions,
                *(
                    Suggestion(description=item.strip())
                    for item in explanation.remediation
                    if item.strip()
                ),
            ],
        }
    )


def _reject_runtime_claims(candidate: NPlusOneCandidate, explanation: NPlusOneExplanation) -> None:
    """Refuse an asserted execution count that no statement log supports."""
    if candidate.runtime is not None:
        return

    for field, text in (
        ("summary", explanation.summary),
        ("reasoning", explanation.reasoning),
        *((f"remediation[{index}]", item) for index, item in enumerate(explanation.remediation)),
    ):
        match = _RUNTIME_CLAIM.search(text)
        if match is not None:
            msg = (
                f"explanation {field} asserts a measured execution count "
                f"({match.group().strip()!r}) for a candidate with no runtime evidence"
            )
            raise ContradictedEvidence(msg)


def _reject_foreign_locations(
    candidate: NPlusOneCandidate, explanation: NPlusOneExplanation
) -> None:
    """Refuse prose naming a source location that is not this candidate's.

    Only ``name.java``-shaped tokens and ``file:line`` anchors are checked. A model
    discussing "the repository" in the abstract is fine; one that names
    ``OrderService.java`` when the finding is in ``PaymentService.java`` has
    relocated the finding, and a reviewer following it looks at the wrong file.
    """
    permitted_files = {
        value.rsplit("/", 1)[-1].lower()
        for value in (candidate.file, candidate.declaration_file)
        if value is not None
    }
    permitted_lines = {
        str(value)
        for value in (candidate.line, candidate.iteration_line, candidate.declaration_line)
        if value is not None
    }

    text = " ".join((explanation.summary, explanation.reasoning, *explanation.remediation))

    for named in re.findall(r"\b[\w$]+\.java\b", text, re.IGNORECASE):
        if named.lower() not in permitted_files:
            msg = f"explanation names {named!r}, which is not this candidate's source file"
            raise ContradictedEvidence(msg)

    for _, line in re.findall(r"\b([\w$]+\.java):(\d+)\b", text, re.IGNORECASE):
        if line not in permitted_lines:
            msg = f"explanation cites line {line}, which is not a line this candidate spans"
            raise ContradictedEvidence(msg)
