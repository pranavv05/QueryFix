"""Pipeline orchestration — drives the stages that are implemented.

This is not a stage. Every module beside it owns one step of the run and knows
nothing about the others; something has to put them in order, and if that lived in
the FastAPI route then the CLI and the eventual webhook handler would each grow
their own copy of it. So the ordering, the fail-soft boundaries, and the run's log
record live here, and the API layer is left with request parsing and response
shaping.

Wired today, in run order:

1. **Extract** — :func:`queryfix.pipeline.extract.extract_source` per source.
2. **Static analysis** — :class:`~queryfix.pipeline.static_rules.RuleEngine` over
   everything extract produced.
3. **N+1 detection** — :func:`~queryfix.pipeline.nplusone.detect_n_plus_one` over
   the *sources*, not just the queries. It is the one stage that needs the files
   themselves: a repository call inside a loop is a fact about control flow, and
   control flow is exactly what an ``ExtractedQuery`` does not carry. Its findings
   merge into the same list as the static ones, so ranking orders them together.
4. **Rank + cap** — :func:`~queryfix.pipeline.report.rank_findings` deduplicates
   and orders the merged findings; :func:`~queryfix.pipeline.report.cap_findings`
   bounds how many reach the comment. Stage 8 in CLAUDE.md's numbering, done here
   rather than only in the renderer because a capped-but-unlabelled report would
   otherwise report a false total in ``AnalyzeResponse`` as well as in Markdown.

Stages 4–6 (provision, EXPLAIN, HypoPG) are not wired because they are not
implemented. The runner does not pretend otherwise: it returns the
:class:`~queryfix.models.report.Report` those stages would have enriched,
carrying only what the static and structural paths could establish.

Fail-soft is the point of the structure (CLAUDE.md invariant 5). A source that
cannot be extracted loses that source and nothing else; a rule engine that raises
loses the static findings and still returns the queries. Either way the caller gets
a report naming what degraded, never an exception.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Sequence
from typing import TYPE_CHECKING

from queryfix.integrations.llm import LLMExplanationProvider
from queryfix.integrations.p6spy import StatementGroup
from queryfix.models.finding import Finding
from queryfix.models.query import ExtractedQuery, QueryKind, SourceFile, SqlSource
from queryfix.models.report import Report, RunContext
from queryfix.pipeline.extract import DEFAULT_DIALECT, extract_source
from queryfix.pipeline.extract.java_structure import ResolveJavaSource
from queryfix.pipeline.report import DEFAULT_MAX_FINDINGS, cap_findings, rank_findings
from queryfix.pipeline.static_rules import RuleEngine
from queryfix.policy import EnforcementPolicy, ReviewResult

if TYPE_CHECKING:
    from github import Github

__all__ = ["EXTRACT_STAGE", "NPLUS_ONE_STAGE", "STATIC_RULES_STAGE", "AnalysisRunner"]

logger = logging.getLogger(__name__)

#: Name recorded in ``Report.degraded_stages`` when the static stage itself fails.
STATIC_RULES_STAGE = "static_rules"

#: Recorded when N+1 detection fails. A bare stage name rather than a per-file
#: marker: the stage reasons across the whole source set at once, so there is no
#: single file whose loss it could name honestly.
NPLUS_ONE_STAGE = "nplusone"

#: Extraction degrades per source, as ``extract:<path>``, rather than as a bare
#: stage name: losing one file out of ten is a materially different report from
#: losing all ten, and a single ``"extract"`` marker cannot say which happened.
EXTRACT_STAGE = "extract"


class AnalysisRunner:
    """Runs the implemented pipeline over a set of SQL sources.

    Holds no per-run state, and must not grow any: the ``/analyze`` route is a plain
    ``def``, so FastAPI runs it in a threadpool and one shared instance serves
    concurrent requests. A run's identity and results live in the
    :class:`~queryfix.models.report.Report` built per call, never on ``self``.

    The rule engine is injected rather than constructed inline so a caller can supply
    a narrower rule set or a real schema provider without reaching into this module.

    ``llm_provider`` is injected the same way, and for the same reason: this class
    must stay constructible with no configuration at all, so every existing caller
    — including the whole test suite — keeps producing the purely deterministic
    report it always has. It defaults to ``None`` rather than reading process
    configuration itself; a caller that wants Groq (or any other
    :class:`~queryfix.integrations.llm.LLMExplanationProvider`) wired in by
    default constructs one from settings and passes it explicitly — see
    :func:`queryfix.integrations.groq.create_default_llm_provider`, used by
    :mod:`queryfix.cli`.
    """

    def __init__(
        self,
        engine: RuleEngine | None = None,
        *,
        llm_provider: LLMExplanationProvider | None = None,
        dialect: str | None = None,
        scope_to_changes: bool = True,
    ) -> None:
        # Constructed eagerly, not per run: RuleEngine reads the live rule registry
        # through a property, so a shared instance still sees rules registered after
        # it was built.
        self._engine = engine if engine is not None else RuleEngine()
        self._llm_provider = llm_provider
        # The repository's SQL dialect, when known. None keeps every source on the
        # default, which is what a caller that never heard of dialects gets.
        self._dialect = dialect
        # Report and enforce only what the pull request touched. A source with no
        # hunks (SQL handed in whole, the HTTP API) is entirely in scope either way.
        self._scope_to_changes = scope_to_changes

    def run(
        self,
        *,
        repo: str,
        pr_number: int,
        sources: Sequence[SourceFile],
        run_id: str | None = None,
        context: RunContext | None = None,
        initial_degraded_stages: Sequence[str] = (),
        post_comment: bool = False,
        client: Github | None = None,
        max_findings: int = DEFAULT_MAX_FINDINGS,
        repeated_statements: Sequence[StatementGroup] | None = None,
        resolve_source: ResolveJavaSource | None = None,
    ) -> Report:
        """Extract queries from ``sources``, apply the static rules, and report.

        A thin wrapper over :meth:`run_review` for callers that only want the
        :class:`~queryfix.models.report.Report` — the FastAPI ``/analyze``
        route and the existing test suite, both of which predate the
        deterministic enforcement decision and have no reason to change
        return type just because :mod:`queryfix.cli` gained one. See
        :meth:`run_review` for every parameter's meaning.
        """
        return self.run_review(
            repo=repo,
            pr_number=pr_number,
            sources=sources,
            run_id=run_id,
            context=context,
            initial_degraded_stages=initial_degraded_stages,
            post_comment=post_comment,
            client=client,
            max_findings=max_findings,
            repeated_statements=repeated_statements,
            resolve_source=resolve_source,
        ).report

    def run_review(
        self,
        *,
        repo: str,
        pr_number: int,
        sources: Sequence[SourceFile],
        run_id: str | None = None,
        context: RunContext | None = None,
        initial_degraded_stages: Sequence[str] = (),
        post_comment: bool = False,
        client: Github | None = None,
        max_findings: int = DEFAULT_MAX_FINDINGS,
        repeated_statements: Sequence[StatementGroup] | None = None,
        resolve_source: ResolveJavaSource | None = None,
        enforcement_policy: EnforcementPolicy | None = None,
    ) -> ReviewResult:
        """Extract queries from ``sources``, apply the static rules, and report,
        plus the deterministic PASS/BLOCKED/DEGRADED/FAILED decision.

        ``run_id`` is generated unless supplied; callers pass one when the identifier
        comes from elsewhere (a webhook delivery ID, a test fixture). ``max_findings``
        bounds the comment's length; findings beyond it are still found and ranked,
        just not shown — see :func:`~queryfix.pipeline.report.cap_findings`.

        ``repeated_statements`` and ``resolve_source`` are both optional inputs to
        the N+1 stage: a captured p6spy log to corroborate a structural pattern
        against, and a way to read a repository interface the pull request did not
        change. Omitting either narrows what that stage can conclude — never what
        the other stages do.

        ``enforcement_policy`` defaults to :class:`~queryfix.policy.EnforcementPolicy`'s
        own default (CRITICAL/HIGH block) rather than reading process
        configuration — this class stays constructible with no configuration
        at all, matching ``llm_provider``'s existing convention. The policy
        is evaluated against every ranked finding, before ``max_findings``
        truncates what the comment shows: a blocking finding ranked 21st in a
        20-item comment must still block (:mod:`queryfix.policy`'s own
        docstring explains why).

        ``post_comment`` still posts a plain issue comment via
        :func:`~queryfix.integrations.github.upsert_report_comment`,
        unrelated to the enforcement decision — this is the FastAPI
        ``/analyze`` route's existing behavior, predating
        :func:`~queryfix.integrations.github.upsert_review`, which
        :mod:`queryfix.cli` calls itself once it has this method's
        :class:`~queryfix.policy.ReviewResult`.
        """
        started = time.perf_counter()
        context = (
            context
            if context is not None
            else RunContext(
                run_id=run_id if run_id is not None else str(uuid.uuid4()),
                repo=repo,
                pr_number=pr_number,
            )
        )

        queries, extract_degraded = self._extract(context, sources)
        findings, static_degraded = self._apply_static_rules(context, queries)
        cross_query, nplusone_degraded = self._detect_n_plus_one(
            context,
            sources,
            queries,
            repeated_statements=repeated_statements,
            resolve_source=resolve_source,
        )
        findings = [*findings, *cross_query]
        degraded_stages = [
            *initial_degraded_stages,
            *extract_degraded,
            *static_degraded,
            *nplusone_degraded,
        ]

        all_findings = rank_findings(findings)
        unchanged_findings = 0
        if self._scope_to_changes:
            all_findings, unchanged_findings = self._scope(all_findings, queries, sources)
        findings, omitted_findings = cap_findings(all_findings, max_findings=max_findings)
        policy = enforcement_policy if enforcement_policy is not None else EnforcementPolicy()

        comment_id = None
        if post_comment:
            from queryfix.integrations.github import GitHubUnavailable, upsert_report_comment

            temp_report = Report(
                context=context,
                queries=queries,
                findings=findings,
                degraded_stages=degraded_stages,
                omitted_findings=omitted_findings,
                unchanged_findings=unchanged_findings,
            )
            try:
                comment_id = upsert_report_comment(context, temp_report, client=client)
            except GitHubUnavailable:
                degraded_stages.append("post_comment")

        elapsed_ms = (time.perf_counter() - started) * 1000
        # The message carries the same fields as `extra` because the two are read by
        # different tools: `extra` by whatever ships structured logs, the message by
        # whoever is tailing them. Neither should have to reconstruct the other.
        logger.info(
            "analysis complete: run_id=%s number_of_queries=%d number_of_findings=%d "
            "number_of_findings_omitted=%d processing_time_ms=%.3f degraded=%d",
            context.run_id,
            len(queries),
            len(findings),
            omitted_findings,
            elapsed_ms,
            len(degraded_stages),
            extra={
                "run_id": context.run_id,
                "repo": context.repo,
                "pr_number": context.pr_number,
                "number_of_queries": len(queries),
                "number_of_findings": len(findings),
                "number_of_findings_omitted": omitted_findings,
                "processing_time_ms": elapsed_ms,
                "degraded_stages": degraded_stages,
            },
        )

        report = Report(
            context=context,
            queries=queries,
            findings=findings,
            degraded_stages=degraded_stages,
            comment_id=comment_id,
            omitted_findings=omitted_findings,
            unchanged_findings=unchanged_findings,
        )
        return policy.evaluate(report, findings=all_findings)

    @staticmethod
    def _scope(
        findings: list[Finding], queries: list[ExtractedQuery], sources: Sequence[SourceFile]
    ) -> tuple[list[Finding], int]:
        """Keep the findings whose code this pull request touched; count the rest.

        Found on a real pilot: a PR that changed 225 lines was blocked by 16 findings,
        none of them on a line it changed — legacy code in files it happened to edit.
        Holding a PR to the whole file's history makes every edit a debt collection,
        which is how a gate gets switched off. A finding is in scope when the extent
        it concerns was changed: any line of the statement for a query, the loop line
        or the call line for an N+1. A source with no hunks did not
        come from a diff, so everything in it is in scope.
        """
        by_path = {source.path: source for source in sources}
        by_id = {query.id: query for query in queries}
        kept: list[Finding] = []
        for finding in findings:
            source = by_path.get(finding.provenance.file)
            if source is None or not source.hunks or finding.provenance.line is None:
                kept.append(finding)
                continue
            start = finding.provenance.span_start
            end = finding.provenance.span_end
            if start is not None and end is not None:
                # A span names two lines that matter, the loop that repeats and the
                # call it repeats, with arbitrary code between them. The PR owns the
                # pattern if it wrote either end; an unrelated edit somewhere inside a
                # seventy-line loop body does not make it the author of the loop.
                touched = any(hunk.touches(start) or hunk.touches(end) for hunk in source.hunks)
            else:
                start = finding.provenance.line
                end = start
                query = by_id.get(finding.query_id) if finding.query_id else None
                # A derived method's text is rendered by QueryFix, so its newlines
                # say nothing about how many source lines the declaration spans.
                if query is not None and query.kind is not QueryKind.SPRING_DATA_DERIVED:
                    end = start + query.text.count("\n")
                touched = any(
                    hunk.touches(line) for hunk in source.hunks for line in range(start, end + 1)
                )
            if touched:
                kept.append(finding)
        return kept, len(findings) - len(kept)

    def _with_dialect(self, source: SourceFile) -> SourceFile:
        """Hand a plain ``.sql`` source to the SQL extractor with this run's dialect.

        A bare :class:`SourceFile` cannot carry a dialect (that would put SQL's
        concerns on every language's input model), so it is promoted to the
        :class:`SqlSource` that can. A source the caller already typed, and any
        non-SQL file, is left alone.
        """
        if (
            self._dialect is None
            or type(source) is not SourceFile
            or not source.path.lower().endswith(".sql")
        ):
            return source
        return SqlSource(
            path=source.path, content=source.content, hunks=source.hunks, dialect=self._dialect
        )

    def _apply_dialect(self, queries: list[ExtractedQuery]) -> list[ExtractedQuery]:
        """Re-label queries that were read as the default so rules parse them as this run's.

        Java extraction has no dialect of its own: a native query or a derived
        method's rendering is parsed by the rule engine from ``query.dialect``, so
        it has to carry the repository's.
        """
        if self._dialect is None:
            return queries
        return [
            query.model_copy(update={"dialect": self._dialect})
            if query.dialect == DEFAULT_DIALECT
            else query
            for query in queries
        ]

    def _extract(
        self, context: RunContext, sources: Sequence[SourceFile]
    ) -> tuple[list[ExtractedQuery], list[str]]:
        """Stage 2, one source at a time so a bad source costs only itself.

        A source degrades in two ways, and both produce the same marker because they
        are the same caveat to whoever reads the report — *this file was not fully
        analyzed*. Either the extractor returned a candidate it could not parse, or
        it raised outright.
        """
        queries: list[ExtractedQuery] = []
        degraded: list[str] = []

        for source in sources:
            # Built once so the two ways a source degrades cannot drift into two
            # spellings of the same marker.
            marker = f"{EXTRACT_STAGE}:{source.path}"
            try:
                # The whole model, not `(path, content)`: a source may carry
                # options its extractor needs — `SqlSource.dialect` is one — and
                # unpacking it here would silently drop them.
                extracted = self._apply_dialect(extract_source(self._with_dialect(source)))
            except Exception:
                # Stage boundary. The extractor already turns malformed SQL into an
                # unanalyzable candidate, so reaching here means something genuinely
                # unexpected — which is exactly what must not take the run down.
                logger.exception(
                    "extract failed for %s; continuing with the remaining sources",
                    source.path,
                    extra={"run_id": context.run_id, "source_path": source.path},
                )
                degraded.append(marker)
                continue

            queries.extend(extracted)

            # Counted rather than collected: the candidates themselves are already in
            # `queries`, and a second list of the same objects earns nothing.
            unparseable = sum(1 for query in extracted if query.parse_error is not None)
            if unparseable:
                logger.warning(
                    "extract: %d candidate(s) in %s could not be parsed",
                    unparseable,
                    source.path,
                    extra={"run_id": context.run_id, "source_path": source.path},
                )
                degraded.append(marker)

        return queries, degraded

    def _apply_static_rules(
        self, context: RunContext, queries: list[ExtractedQuery]
    ) -> tuple[list[Finding], list[str]]:
        """Stage 3, in one call so each query is parsed once and ranked against all.

        One call rather than one per source: the engine parses each query exactly
        once, and a single call is also what lets it rank a CRITICAL finding from the
        last file above a MEDIUM one from the first.

        The engine already confines a failing *rule* to that rule. This boundary
        catches what it cannot: a failure in its own parse-and-dispatch loop, which
        would otherwise discard the extract stage's work along with the findings.
        """
        try:
            return self._engine.analyze(queries), []
        except Exception:
            logger.exception(
                "static rule stage failed; reporting without static findings",
                extra={"run_id": context.run_id, "number_of_queries": len(queries)},
            )
            return [], [STATIC_RULES_STAGE]

    def _detect_n_plus_one(
        self,
        context: RunContext,
        sources: Sequence[SourceFile],
        queries: list[ExtractedQuery],
        *,
        repeated_statements: Sequence[StatementGroup] | None,
        resolve_source: ResolveJavaSource | None,
    ) -> tuple[list[Finding], list[str]]:
        """Stage 7, over the sources rather than the extracted queries.

        The only stage handed the raw sources. It has to be: the loop that makes a
        query repeat is not in the query, and nothing between here and the file
        text carries control flow.

        Fail-soft at the stage boundary, like the rule engine above it. Structural
        analysis reads whatever a pull request contains — including Java that is
        mid-edit, generated, or written against a language version this analyzer
        does not fully model — so a malformed file must cost this stage's findings
        and nothing else. A crashed N+1 stage still leaves every static finding
        intact, which is CLAUDE.md's invariant 5 applied at the one place a new
        stage could most easily break it.
        """
        try:
            # Imported here rather than at module scope: the stage pulls in sqlglot
            # and the structural analyzer, and a runner constructed for a SQL-only
            # run should not pay for them.
            from queryfix.pipeline.nplusone import detect_n_plus_one

            return (
                detect_n_plus_one(
                    sources,
                    queries=queries,
                    repeated_statements=repeated_statements,
                    resolve_source=resolve_source,
                    explain=self._llm_provider,
                ),
                [],
            )
        except Exception:
            logger.exception(
                "n+1 stage failed; reporting without cross-query findings",
                extra={"run_id": context.run_id, "number_of_sources": len(sources)},
            )
            return [], [NPLUS_ONE_STAGE]
