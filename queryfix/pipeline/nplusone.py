"""Stage 7 — N+1 detection.

Reasons *across* the diff rather than within one query: a repository call inside a
loop, a lazy association dereferenced per row, the same statement re-issued once
per element. No single-query rule can see any of these, which is why this is a
stage of its own and not a
:class:`~queryfix.pipeline.static_rules.base.Rule` — a rule receives one parsed
query and nothing else, by design, and widening that contract for one check would
cost every other rule its simplicity.

The evidence comes from three places, and keeping them distinct is most of the
value here:

* **Structure**, from :mod:`queryfix.pipeline.extract.java_structure` — which
  identifier holds a repository, which construct repeats, what flows into the
  arguments. Facts about source text.
* **Declarations**, from the extract stage — the JPQL, native SQL, or derived
  method a call site resolves to, so the report can quote the query rather than
  just name a method.
* **Execution**, from :mod:`queryfix.integrations.p6spy` when a run captured a
  statement log. This is the only input that can say something *ran*.

A finding never presents the first two as though they were the third. Source
analysis proves a call sits inside a loop; it cannot prove the loop iterates more
than once, that the collection is not empty, or that a cache does not absorb the
repeat. So the default title is "Potential N+1 query pattern", and the wording
strengthens only where a statement log actually corroborates it — the same
discipline CLAUDE.md applies to unverifiable LLM claims, applied to our own.

Detection is deterministic and does not depend on a model: every candidate,
tier, and rendered :class:`Finding` below is decided before an
:class:`~queryfix.integrations.llm.LLMExplanationProvider` is ever consulted,
and nothing about *whether* a pattern is reported changes if one is absent. An
optional provider — Groq today, via
:class:`~queryfix.integrations.groq.GroqExplanationProvider` — may be supplied
to enrich the rendered findings with prose; when it is, the model describes
evidence that already exists rather than deciding what the evidence is, and its
prose is reconciled against that evidence by
:func:`queryfix.integrations.llm.reconcile_nplusone_explanation` before it can
reach a finding. A caller that supplies no provider gets exactly the report this
module has always produced.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence

import sqlglot
from sqlglot import exp

from queryfix.integrations.llm import (
    ContradictedEvidence,
    LLMExplanationProvider,
    reconcile_nplusone_explanation,
)
from queryfix.integrations.p6spy import StatementGroup
from queryfix.models.finding import Evidence, Finding, Suggestion
from queryfix.models.java_structure import (
    ArgumentDependency,
    IterationKind,
    JavaProgram,
    LazyAssociationAccess,
    RepositoryCallSite,
    RepositoryDeclaration,
    RepositoryMethodDeclaration,
)
from queryfix.models.nplusone import (
    EvidenceTier,
    NPlusOneCandidate,
    NPlusOneKind,
    RuntimeCorroboration,
)
from queryfix.models.query import ExtractedQuery, Provenance, SourceFile
from queryfix.pipeline.extract.derived import entity_table
from queryfix.pipeline.extract.java_structure import ResolveJavaSource, analyze_java_program

__all__ = [
    "LAZY_ASSOCIATION_RULE_ID",
    "REPOSITORY_CALL_RULE_ID",
    "build_candidates",
    "detect_n_plus_one",
    "render_candidate",
]

logger = logging.getLogger(__name__)

REPOSITORY_CALL_RULE_ID = "nplusone-repository-call-in-loop"
LAZY_ASSOCIATION_RULE_ID = "nplusone-lazy-association-in-loop"

#: A shape must repeat at least this often, with at least this many distinct bind
#: sets, before a statement log is read as corroborating an N+1. Two executions of
#: one statement is ordinary; the signature p6spy's own docstring describes is the
#: same shape repeating with only the parameter changing.
_RUNTIME_MIN_COUNT = 3

#: Dialect used to read a normalized statement back. Must stay in step with
#: :func:`~queryfix.integrations.p6spy.normalize_sql`, whose placeholder token is
#: whatever this dialect renders — ``%s`` for Postgres, which no other dialect
#: parses. Postgres, because the reference database is Postgres 16.
_STATEMENT_DIALECT = "postgres"

#: How each repeating construct is described in prose.
_ITERATION_PROSE: dict[IterationKind, str] = {
    IterationKind.FOR: "a `for` loop",
    IterationKind.ENHANCED_FOR: "a for-each loop",
    IterationKind.WHILE: "a `while` loop",
    IterationKind.DO_WHILE: "a `do`/`while` loop",
    IterationKind.LAMBDA_FOR_EACH: "a `forEach` lambda",
    IterationKind.LAMBDA_STREAM: "a per-element stream operation",
}


def detect_n_plus_one(
    sources: Sequence[SourceFile],
    *,
    queries: Sequence[ExtractedQuery] = (),
    repeated_statements: Sequence[StatementGroup] | None = None,
    resolve_source: ResolveJavaSource | None = None,
    explain: LLMExplanationProvider | None = None,
) -> list[Finding]:
    """Identify N+1 access patterns across a run's Java sources.

    ``sources`` is the whole changed set rather than one file, because an N+1 is
    routinely split across files: the loop is in a service and the method it calls
    is declared on a repository interface elsewhere. Non-Java sources are ignored
    rather than rejected, so a caller can pass the same list it gave every other
    stage.

    ``queries`` lets a resolved call site name the query it will issue instead of
    just the method. ``repeated_statements`` carries corroboration from a p6spy
    log when a run captured one, as produced by
    :func:`~queryfix.integrations.p6spy.find_repeated_statements`.
    ``resolve_source`` fetches a repository interface the pull request did not
    change; without it, such a type resolves by naming convention and its findings
    are reported one tier weaker.

    ``explain`` is an optional :class:`~queryfix.integrations.llm.LLMExplanationProvider`
    consulted only when there is at least one candidate to explain — a run with no
    N+1 patterns never calls it, and a caller that passes none gets exactly the
    deterministic findings this function has always produced. A provider that
    fails entirely, or whose prose contradicts a candidate's own evidence, costs
    that candidate its enrichment and nothing else; the deterministic finding is
    always what falls back.

    The previous signature took a ``diff: str`` this stage never had a use for.
    Structural analysis needs the files, not a patch, and passing typed sources
    rather than smuggling them through a string is what lets every branch below be
    tested without constructing a diff.
    """
    program = analyze_java_program(sources, queries=queries, resolve_source=resolve_source)
    candidates = build_candidates(program, repeated_statements=repeated_statements)
    findings = [render_candidate(candidate) for candidate in candidates]

    if explain is None or not candidates:
        return findings
    return _apply_explanations(candidates, findings, explain)


def _apply_explanations(
    candidates: Sequence[NPlusOneCandidate],
    findings: Sequence[Finding],
    explain: LLMExplanationProvider,
) -> list[Finding]:
    """Enrich each finding with provider prose, never at the cost of correctness.

    Two independent ways this can fall back to the deterministic finding, and
    both are ordinary operation rather than error paths: the provider itself may
    answer for only some (or none) of the candidates, and a returned explanation
    may be rejected by :func:`~queryfix.integrations.llm.reconcile_nplusone_explanation`
    for asserting something the evidence does not support. A provider failing
    outright — an exception escaping ``explain_nplusone`` itself, which a
    well-behaved provider should never do — is caught here too, as a last resort:
    an implementation bug in a provider must not be able to cost the run its
    deterministic N+1 findings.
    """
    try:
        explanations = explain.explain_nplusone(list(zip(candidates, findings, strict=True)))
    except Exception:
        logger.exception(
            "n+1 explanation provider failed; keeping deterministic findings",
            extra={"number_of_candidates": len(candidates)},
        )
        return list(findings)

    if not explanations:
        return list(findings)

    enriched: list[Finding] = []
    for candidate, finding in zip(candidates, findings, strict=True):
        explanation = explanations.get(candidate.identity)
        if explanation is None:
            enriched.append(finding)
            continue
        try:
            enriched.append(reconcile_nplusone_explanation(finding, candidate, explanation))
        except ContradictedEvidence:
            logger.warning(
                "n+1 explanation for %s:%d contradicted its own evidence; discarding",
                candidate.file,
                candidate.line,
            )
            enriched.append(finding)
    return enriched


def build_candidates(
    program: JavaProgram,
    *,
    repeated_statements: Sequence[StatementGroup] | None = None,
) -> list[NPlusOneCandidate]:
    """Turn structural facts into ranked, deduplicated candidates.

    Separated from rendering so the deterministic half is testable without going
    through Markdown, and so the LLM stage can be handed candidates rather than
    findings it might be tempted to rewrite.
    """
    groups = _usable_statement_groups(repeated_statements)
    candidates: list[NPlusOneCandidate] = []

    for structure in program.files.values():
        for site in structure.call_sites:
            if not site.iteration:
                # The overwhelmingly common, entirely healthy case: a repository
                # call that runs once. Reporting these is how a detector becomes
                # something reviewers mute.
                continue
            candidates.append(_repository_candidate(site, program, groups))

        for access in structure.lazy_accesses:
            candidates.append(_lazy_candidate(access, groups, program))

    return _deduplicate(candidates)


def _deduplicate(candidates: Sequence[NPlusOneCandidate]) -> list[NPlusOneCandidate]:
    """Collapse repeats of one underlying pattern, keeping the best-evidenced.

    A nested loop yields one call site, not one per level, so duplicates are rare
    by construction — but a file reachable twice (a repository fetched by import
    *and* present in the diff) would otherwise report the same call twice.
    Ordering is by tier, so the strongest evidence for a pattern is the copy kept
    and the report leads with the most defensible findings.
    """
    order = list(EvidenceTier)
    best: dict[tuple[str, ...], NPlusOneCandidate] = {}

    for candidate in candidates:
        existing = best.get(candidate.identity)
        if existing is None or order.index(candidate.tier) < order.index(existing.tier):
            best[candidate.identity] = candidate

    return sorted(
        best.values(),
        key=lambda candidate: (
            order.index(candidate.tier),
            candidate.file,
            candidate.line,
        ),
    )


def _repository_candidate(
    site: RepositoryCallSite,
    program: JavaProgram,
    groups: Sequence[StatementGroup],
) -> NPlusOneCandidate:
    """Build a candidate for a repository call inside a repeating construct."""
    declaration = program.repositories.get(site.repository_type)
    method = _declared_method(declaration, site.method_name)
    query_id = method.query_id if method is not None else None

    runtime = _correlate_runtime(
        groups, _candidate_tables(program, site.repository_type, declaration)
    )
    tier = _repository_tier(site, runtime)
    innermost = site.innermost_iteration
    assert innermost is not None  # guarded by the caller: `site.iteration` is non-empty

    return NPlusOneCandidate(
        kind=NPlusOneKind.REPOSITORY_CALL_IN_LOOP,
        tier=tier,
        file=site.file,
        line=site.line,
        enclosing_type=site.enclosing_type,
        enclosing_method=site.enclosing_method,
        repository_type=site.repository_type,
        method_name=site.method_name,
        query_id=query_id,
        query_ids=(query_id,) if query_id is not None else (),
        declaration_file=method.file if method is not None else None,
        declaration_line=method.line if method is not None else None,
        iteration_kind=innermost.kind,
        iteration_line=innermost.span.start_line,
        iterable_text=innermost.iterable_text,
        element_identifier=innermost.element_identifier,
        loop_depth=site.loop_depth,
        dependency=site.dependency,
        dependency_detail=site.dependency_detail,
        repository_resolved=declaration is not None,
        runtime=runtime,
    )


def _lazy_candidate(
    access: LazyAssociationAccess,
    groups: Sequence[StatementGroup],
    program: JavaProgram,
) -> NPlusOneCandidate:
    """Build a candidate for a lazy association dereferenced per element.

    Capped below the structural repository tiers however good the shape looks. A
    getter is an ordinary method call; whether it issues SQL depends on the
    persistence context — a fetch join, an entity graph, or an earlier touch in
    the same transaction all make it free. That is not knowable from source, so
    the tier says so.
    """
    runtime = _correlate_runtime(groups, _candidate_tables(program, access.entity_type, None))
    tier = EvidenceTier.HIGH if runtime is not None else EvidenceTier.MEDIUM
    innermost = access.iteration[-1]

    return NPlusOneCandidate(
        kind=NPlusOneKind.LAZY_ASSOCIATION_IN_LOOP,
        tier=tier,
        file=access.file,
        line=access.line,
        enclosing_type=access.enclosing_type,
        enclosing_method=access.enclosing_method,
        repository_type=None,
        method_name=access.accessor_name,
        iteration_kind=innermost.kind,
        iteration_line=innermost.span.start_line,
        iterable_text=innermost.iterable_text,
        element_identifier=innermost.element_identifier,
        loop_depth=access.loop_depth,
        dependency=ArgumentDependency.NO_ARGUMENTS,
        dependency_detail=(
            f"`{access.receiver_identifier}.{access.accessor_name}()` dereferences the "
            f"lazy `@{access.relationship.association}` association "
            f"`{access.entity_type}.{access.relationship.field_name}`"
        ),
        runtime=runtime,
    )


def _repository_tier(
    site: RepositoryCallSite, runtime: RuntimeCorroboration | None
) -> EvidenceTier:
    """Grade a repository call site by the evidence actually obtained.

    The ladder is deliberately made of conditions a reader can check, not of
    weights. Runtime corroboration plus a matching structure is the only thing
    that earns the top tier; a type resolved by name alone can never rise above
    the bottom one, because "it ends in Repository" is a convention, and a
    convention is not a fact about what the code does.
    """
    if runtime is not None:
        return EvidenceTier.VERY_HIGH if site.iteration else EvidenceTier.HIGH
    if site.resolution.value == "name-convention":
        return EvidenceTier.LOW
    if site.dependency is ArgumentDependency.LOOP_ELEMENT_ARGUMENT:
        return EvidenceTier.MEDIUM_HIGH
    return EvidenceTier.MEDIUM


def _declared_method(
    declaration: RepositoryDeclaration | None, method_name: str
) -> RepositoryMethodDeclaration | None:
    """The declared method a call resolves to, matched by name.

    Name-only, because Java dispatches on argument types and this analyzer does
    not model them. Two overloads collapse to whichever is declared first, which
    can attribute a call to the wrong sibling; both are the same method on the
    same repository, so the query quoted may be the wrong overload's. Recorded as
    a limitation rather than papered over.
    """
    if declaration is None:
        return None
    return next((method for method in declaration.methods if method.name == method_name), None)


def _usable_statement_groups(
    repeated_statements: Sequence[StatementGroup] | None,
) -> list[StatementGroup]:
    """Statement groups strong enough to corroborate anything.

    A shape whose bind values never change is a caching problem, not an N+1 — the
    distinction p6spy's own grouping was built to expose — so those are dropped
    here rather than allowed to inflate a structural finding's tier.
    """
    if not repeated_statements:
        return []
    return [
        group
        for group in repeated_statements
        if group.count >= _RUNTIME_MIN_COUNT and group.distinct_variants > 1
    ]


def _candidate_tables(
    program: JavaProgram | None,
    type_name: str,
    declaration: RepositoryDeclaration | None,
) -> dict[str, str]:
    """Table names a candidate might read, mapped to how each was arrived at.

    Three sources, strongest first. A repository's entity type argument plus that
    entity's ``@Table(name = …)`` is a real mapping and is preferred. Failing
    that, the entity or repository name is converted by the same convention the
    derived-method renderer uses — which is a guess, and is labelled as one in the
    evidence, because an entity called ``Order`` maps to ``orders`` far more often
    than to ``order``.
    """
    candidates: dict[str, str] = {}
    entity = declaration.entity_type if declaration is not None else None

    if entity is not None and program is not None:
        mapped = program.tables.get(entity)
        if mapped is not None:
            candidates[mapped.lower()] = f"`{entity}` maps to `{mapped}` via `@Table`"

    if entity is not None:
        derived = entity_table(entity)
        if derived and derived != "unknown_entity":
            candidates.setdefault(derived, f"`{entity}` by naming convention")

    derived_from_repository = entity_table(type_name)
    if derived_from_repository and derived_from_repository != "unknown_entity":
        candidates.setdefault(derived_from_repository, f"`{type_name}` by naming convention")

    return candidates


def _correlate_runtime(
    groups: Sequence[StatementGroup], tables: Mapping[str, str]
) -> RuntimeCorroboration | None:
    """Attribute a repeated statement shape to this candidate, conservatively.

    Matching is on table identity. Where an entity declares ``@Table(name = …)``
    that is a real mapping; otherwise it is a naming convention, since QueryFix
    has no entity-to-table metadata of its own (STATUS.md, TD-15). Either way the
    resulting evidence says "a statement against this table repeated", never "this
    call site ran it" — the log records what the application did, not which line
    asked for it.

    No match is not a downgrade. A log captured from a suite that never exercised
    this path proves nothing either way, and treating silence as exoneration would
    hide precisely the untested paths most likely to be wrong.
    """
    if not tables:
        return None

    for group in groups:
        read = _statement_tables(group.normalized_sql)
        matched = next((table for table in tables if table in read), None)
        if matched is None:
            continue
        return RuntimeCorroboration(
            normalized_sql=group.normalized_sql,
            count=group.count,
            distinct_variants=group.distinct_variants,
            total_elapsed_ms=group.total_elapsed_ms,
            matched_on=f"statement reads `{matched}`; {tables[matched]}",
        )
    return None


def _statement_tables(sql: str, dialect: str = _STATEMENT_DIALECT) -> set[str]:
    """Table names a statement reads, lowercased, or an empty set if unreadable.

    Parsed rather than pattern-matched, for the reason CLAUDE.md gives for every
    other SQL reading in this codebase: a regex cannot tell a table name from the
    same characters inside a quoted identifier.

    The dialect must match the one
    :func:`~queryfix.integrations.p6spy.normalize_sql` rendered with, because
    normalization replaces every literal with that dialect's placeholder. Reading
    a Postgres-normalized statement as generic SQL fails on the first ``%s``,
    which silently yields no tables and disables runtime correlation entirely
    rather than reporting an error — the failure mode this default exists to
    close.
    """
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except sqlglot.ParseError:
        return set()
    if tree is None or isinstance(tree, exp.Command):
        return set()
    return {table.name.lower() for table in tree.find_all(exp.Table) if table.name}


def render_candidate(candidate: NPlusOneCandidate) -> Finding:
    """Turn one candidate into the Finding a reviewer reads.

    Everything user-facing is decided here so the deterministic half stays free of
    prose, and so the wording that distinguishes an observed repeat from an
    inferred one lives in exactly one place.
    """
    return Finding(
        rule_id=(
            REPOSITORY_CALL_RULE_ID
            if candidate.kind is NPlusOneKind.REPOSITORY_CALL_IN_LOOP
            else LAZY_ASSOCIATION_RULE_ID
        ),
        severity=candidate.tier.severity,
        title=_title(candidate),
        explanation=_explanation(candidate),
        impact=_impact(candidate),
        provenance=Provenance(
            file=candidate.file,
            line=candidate.line,
            symbol=candidate.enclosing_method,
            span_start=min(candidate.iteration_line, candidate.line),
            span_end=max(candidate.iteration_line, candidate.line),
        ),
        query_id=candidate.query_id,
        query_ids=list(candidate.query_ids),
        evidence=_evidence(candidate),
        suggestions=_suggestions(candidate),
        confidence=candidate.tier.confidence,
        confidence_basis=_confidence_basis(candidate),
    )


def _title(candidate: NPlusOneCandidate) -> str:
    """The headline, which states how strongly the pattern is actually known.

    "Potential" is the default and stays until a statement log corroborates the
    shape. A structural match is a strong reason to look; it is not a measurement,
    and a title that claims one would be the pipeline making exactly the
    unverifiable assertion it labels an LLM for making.
    """
    if candidate.kind is NPlusOneKind.LAZY_ASSOCIATION_IN_LOOP:
        return "Potential N+1 from a lazy association loaded inside a loop"
    if candidate.tier.is_runtime_backed:
        return "N+1 query pattern, corroborated by a captured statement log"
    return "Potential N+1 query pattern"


def _call_description(candidate: NPlusOneCandidate) -> str:
    """``Type.method(...)`` as a reviewer would search for it."""
    if candidate.repository_type is None:
        return f"`{candidate.method_name}()`"
    return f"`{candidate.repository_type}.{candidate.method_name}(…)`"


def _explanation(candidate: NPlusOneCandidate) -> str:
    """What was found, in terms the reviewer can check against their own file."""
    construct = _ITERATION_PROSE.get(candidate.iteration_kind, "a repeating construct")
    where = f"line {candidate.iteration_line}"
    over = f" over `{candidate.iterable_text}`" if candidate.iterable_text else ""

    if candidate.kind is NPlusOneKind.LAZY_ASSOCIATION_IN_LOOP:
        opening = (
            f"{_call_description(candidate)} dereferences a lazily-fetched association "
            f"inside {construct}{over} that opens at {where}."
        )
    else:
        opening = (
            f"{_call_description(candidate)} is called inside {construct}{over} that "
            f"opens at {where}."
        )

    parts = [opening]
    if candidate.loop_depth > 1:
        parts.append(
            f"It sits {candidate.loop_depth} levels of iteration deep, so the repeat "
            f"count is the product of the enclosing collections rather than the size "
            f"of any one of them."
        )
    if candidate.dependency is ArgumentDependency.LOOP_ELEMENT_ARGUMENT:
        parts.append(
            "The query is parameterised by the current element, which is the "
            "parent-key/child-query shape of a classic N+1 rather than a repeated "
            "identical lookup."
        )
    elif candidate.kind is NPlusOneKind.REPOSITORY_CALL_IN_LOOP:
        parts.append(
            "Its arguments do not appear to depend on the current element, so this is "
            "repeated work rather than the classic per-parent lookup — it may be "
            "hoistable out of the loop entirely."
        )
    if not candidate.repository_resolved:
        parts.append(
            f"`{candidate.repository_type}`'s declaration was not available in this "
            f"change, so it was treated as a repository on the strength of its name."
        )
    return " ".join(parts)


def _impact(candidate: NPlusOneCandidate) -> str:
    """The consequence at scale, which is the whole reason to act.

    The observed numbers are reported exactly as counted, and nothing is added to
    them. A captured log records one run against whatever data that run had — six
    executions in a test fixture, or six thousand in production — so extrapolating
    from it would be inventing the very measurement this branch exists to report
    honestly. What the log establishes is that the count tracks the collection;
    saying that is enough, and it is true at either size.
    """
    if candidate.runtime is not None:
        return (
            f"A statement of this shape ran {candidate.runtime.count:,} times in the "
            f"captured log with {candidate.runtime.distinct_variants:,} distinct "
            f"parameter sets, totalling {candidate.runtime.total_elapsed_ms:,} ms. "
            f"One execution per parameter set is the N+1 signature: the count tracks "
            f"the size of the collection, so it grows with production data even where "
            f"each individual execution stays fast."
        )
    return (
        "Each iteration costs a database round trip, so total latency grows linearly "
        "with the collection — a set that is small in development and large in "
        "production degrades without the query itself ever looking slow, because no "
        "individual execution is."
    )


def _confidence_basis(candidate: NPlusOneCandidate) -> str | None:
    """Why this candidate sits in its evidence tier, in terms a reviewer can check.

    A tier is an ordinal, and the same tier will recur on most findings in a pull
    request; printing its number told a reader nothing about *this* one. The reason
    does: it names the specific conditions the tier ladder tested.
    """
    if candidate.tier.is_runtime_backed:
        return None  # observed, not inferred: the evidence section carries the proof
    if candidate.kind is NPlusOneKind.LAZY_ASSOCIATION_IN_LOOP:
        return (
            "a lazy association is read once per element, but whether it is actually "
            "loaded lazily at runtime (fetch plan, open session) was not checked"
        )
    if not candidate.repository_resolved:
        return (
            f"the type of `{candidate.repository_type}` was inferred from its name; "
            "its declaration was not seen"
        )
    if candidate.dependency is ArgumentDependency.LOOP_ELEMENT_ARGUMENT:
        element = f"`{candidate.element_identifier}`" if candidate.element_identifier else "it"
        return (
            f"the loop variable {element} feeds the call's arguments and the repository "
            "declaration was resolved, which is the shape of a per-row lookup"
        )
    return (
        "the call repeats inside a loop but its arguments do not depend on the loop "
        "element, so it may be repeated work that can be hoisted rather than a per-row lookup"
    )


def _evidence(candidate: NPlusOneCandidate) -> list[Evidence]:
    """Every fact the finding rests on, itemized so a reviewer can check each.

    Structural and runtime evidence are separate entries and are labelled as such.
    A reader must be able to tell at a glance which parts were read out of the
    source and which were observed running.
    """
    construct = _ITERATION_PROSE.get(candidate.iteration_kind, "a repeating construct")
    items = [
        Evidence(
            label="Repetition (static)",
            detail=(
                f"{_call_description(candidate)} appears inside {construct} opening at "
                f"`{candidate.file}:{candidate.iteration_line}`."
            ),
        )
    ]

    if candidate.loop_depth > 1:
        items.append(
            Evidence(
                label="Nesting (static)",
                detail=f"{candidate.loop_depth} enclosing levels of iteration.",
            )
        )

    if candidate.dependency_detail is not None:
        items.append(
            Evidence(
                label=(
                    "Element dependency (static)"
                    if candidate.dependency is ArgumentDependency.LOOP_ELEMENT_ARGUMENT
                    else "Association (static)"
                ),
                detail=candidate.dependency_detail,
            )
        )

    if candidate.declaration_file is not None:
        items.append(
            Evidence(
                label="Query declaration (static)",
                detail=(
                    f"`{candidate.method_name}` is declared at "
                    f"`{candidate.declaration_file}:{candidate.declaration_line}`."
                ),
            )
        )

    if candidate.runtime is not None:
        items.append(
            Evidence(
                label="Runtime (observed)",
                detail=(
                    f"`{candidate.runtime.normalized_sql}` executed "
                    f"{candidate.runtime.count:,} times with "
                    f"{candidate.runtime.distinct_variants:,} distinct parameter sets "
                    f"({candidate.runtime.matched_on})."
                ),
            )
        )

    items.append(
        Evidence(
            label="Confidence tier",
            detail=(
                f"{candidate.tier.label} — "
                f"{'corroborated by observed execution' if candidate.tier.is_runtime_backed else 'static source analysis only; no query was executed'}."
            ),
        )
    )
    return items


def _suggestions(candidate: NPlusOneCandidate) -> list[Suggestion]:
    """Remediations, ordered by how directly they address this shape."""
    if candidate.kind is NPlusOneKind.LAZY_ASSOCIATION_IN_LOOP:
        return [
            Suggestion(
                description=(
                    "Load the association with the parents in one query — a "
                    "`JOIN FETCH`, or an `@EntityGraph` on the finder that produced "
                    "the collection — so the loop walks data already in memory. If "
                    "the association is genuinely needed per element, Hibernate's "
                    "`@BatchSize` turns N round trips into N/size."
                )
            )
        ]

    batched = (
        f"`{candidate.method_name}In(…)`"
        if candidate.method_name.startswith(("find", "get", "read", "query"))
        else "a batched finder"
    )
    return [
        Suggestion(
            description=(
                f"Fetch the children in one round trip before the loop — a derived "
                f"finder taking a collection, such as {batched}, or a single query "
                f"joining parent to child — then index the result in memory and look "
                f"it up per element."
            )
        ),
        Suggestion(
            description=(
                "If the parents come from a query you control, a `JOIN FETCH` or an "
                "`@EntityGraph` can bring the children back with them, removing the "
                "second query rather than batching it."
            )
        ),
    ]
