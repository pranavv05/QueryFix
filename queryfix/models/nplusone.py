"""N+1 contracts — the deterministic evidence, separate from its prose.

Stage 7 produces two different kinds of thing and they must not be confused. One
is a set of *facts*: a call site at a line, inside a loop, on a type that extends
a Spring Data interface, with the loop variable flowing into its arguments. The
other is an *explanation* a reviewer reads. The facts are established by static
analysis and are checkable; the prose is written to make them land.

:class:`NPlusOneCandidate` is the first of those, and it is the whole input the
LLM stage is ever given. That is what makes CLAUDE.md's rule enforceable rather
than aspirational — a model cannot invent a loop that is not in the candidate,
because deciding what the loops are is not a step it participates in. It can only
describe what is already here, and
:func:`queryfix.integrations.claude.reconcile_nplusone_explanation` checks that
it did.

:class:`EvidenceTier` is the other half of the discipline. Confidence here is not
a number someone tuned; it is a statement of which evidence was actually
obtained, and every tier names a combination a reader can verify.
"""

from __future__ import annotations

from enum import Enum

from pydantic import Field

from queryfix.models.base import Contract
from queryfix.models.finding import Severity
from queryfix.models.java_structure import ArgumentDependency, IterationKind

__all__ = [
    "EvidenceTier",
    "NPlusOneCandidate",
    "NPlusOneExplanation",
    "NPlusOneKind",
    "RuntimeCorroboration",
]


class EvidenceTier(str, Enum):
    """How much is actually known about a candidate, worst evidence last.

    Ordered from proof toward inference. The two runtime tiers are the only ones
    that rest on something having been observed to run; everything below them is
    a claim about source text, however strong. A finding never presents a static
    tier as though it were a measurement — the report's wording changes with the
    tier, not just a number beside it.
    """

    VERY_HIGH = "very-high"
    HIGH = "high"
    MEDIUM_HIGH = "medium-high"
    MEDIUM = "medium"
    LOW = "low"

    @property
    def is_runtime_backed(self) -> bool:
        """Whether this tier rests on observed execution rather than source alone."""
        return self in {EvidenceTier.VERY_HIGH, EvidenceTier.HIGH}

    @property
    def label(self) -> str:
        """The tier as a reviewer sees it."""
        return self.value.replace("-", " ").upper()

    @property
    def severity(self) -> Severity:
        """How loudly this tier is reported.

        Runtime-corroborated and argument-dependent structural patterns are HIGH;
        everything weaker is MEDIUM. Nothing here is CRITICAL: an N+1 is a
        performance defect, not a correctness or data-loss one, and CRITICAL is
        reserved in this codebase for an unqualified write.
        """
        if self in {EvidenceTier.VERY_HIGH, EvidenceTier.HIGH, EvidenceTier.MEDIUM_HIGH}:
            return Severity.HIGH
        return Severity.MEDIUM

    @property
    def confidence(self) -> float | None:
        """The value carried on :attr:`~queryfix.models.finding.Finding.confidence`.

        None for a runtime-backed tier, deliberately. That field means "this could
        not be verified", and the report renders it as such; a pattern corroborated
        by a statement log *was* verified, against real executed SQL, so labelling
        it as inferred would misdescribe the strongest evidence the pipeline has.
        """
        return {
            EvidenceTier.MEDIUM_HIGH: 0.75,
            EvidenceTier.MEDIUM: 0.6,
            EvidenceTier.LOW: 0.4,
        }.get(self)


class NPlusOneKind(str, Enum):
    """Which access pattern a candidate represents."""

    REPOSITORY_CALL_IN_LOOP = "repository-call-in-loop"
    LAZY_ASSOCIATION_IN_LOOP = "lazy-association-in-loop"


class RuntimeCorroboration(Contract):
    """A statement log's account of a shape matching this candidate.

    Attribution is by table name, which is a naming convention rather than a
    mapping (QueryFix has no entity-to-table metadata — see STATUS.md's TD-15),
    so this says *a statement against this table repeated*, not *this call site
    executed it*. The finding says the same, and never more.
    """

    normalized_sql: str
    count: int = Field(ge=1)
    distinct_variants: int = Field(ge=1)
    total_elapsed_ms: int = Field(ge=0)
    matched_on: str = Field(description="Why this group was attributed to the candidate.")


class NPlusOneCandidate(Contract):
    """One deterministic N+1 candidate: what was found, where, and how strongly.

    Everything on it was established by reading source. Nothing on it is a
    judgement about what the author meant, and nothing on it came from a model.
    """

    kind: NPlusOneKind
    tier: EvidenceTier
    file: str = Field(min_length=1)
    line: int = Field(ge=1)
    enclosing_type: str | None = None
    enclosing_method: str | None = None

    repository_type: str | None = Field(
        default=None, description="None for a lazy-association candidate."
    )
    method_name: str = Field(min_length=1)
    query_id: str | None = Field(
        default=None,
        description="The declaration this call resolves to, when extraction saw it. "
        "None when the repository lives outside the pull request and could not be "
        "retrieved, or when the method is inherited from a Spring Data base type.",
    )
    query_ids: tuple[str, ...] = Field(default=())
    declaration_file: str | None = None
    declaration_line: int | None = None

    iteration_kind: IterationKind
    iteration_line: int = Field(ge=1)
    iterable_text: str | None = None
    element_identifier: str | None = None
    loop_depth: int = Field(ge=1)
    dependency: ArgumentDependency = ArgumentDependency.INDEPENDENT
    dependency_detail: str | None = None
    repository_resolved: bool = Field(
        default=True,
        description="False when the repository type was assumed from its name "
        "because its declaration was never seen.",
    )
    runtime: RuntimeCorroboration | None = None

    @property
    def identity(self) -> tuple[str, ...]:
        """Structural identity, for collapsing repeats of one underlying pattern.

        Keyed on the call site rather than on the text, so the same method called
        at two places in one loop stays two findings — they are two edits — while
        one call reached by several analysis paths stays one.
        """
        return (
            self.kind.value,
            self.file,
            str(self.line),
            self.repository_type or "",
            self.method_name,
            str(self.iteration_line),
        )


class NPlusOneExplanation(Contract):
    """Prose an LLM may contribute to a candidate. Text only, by construction.

    There is no field here for a file, a line, a query, or a count. That is the
    enforcement mechanism: the model is not given a channel through which it could
    relocate a finding or assert a number nobody measured, so a malformed response
    fails validation instead of quietly rewriting the evidence.
    """

    summary: str = Field(min_length=1, description="One sentence naming the pattern.")
    reasoning: str = Field(default="", description="Why this shape is likely an N+1.")
    remediation: tuple[str, ...] = Field(
        default=(), description="Suggested fixes, most applicable first."
    )
