"""Stage 8 — Report rendering.

Merges static findings, plan findings, index suggestions, and N+1 findings into a
single ranked Markdown report: severity, evidence (plan excerpts, cost deltas),
and a suggested fix per finding.

Rendering is deliberately a pure function of the :class:`Report` model — the
comment body is user-facing, so it is snapshot-tested, and that only works if the
same report always renders the same Markdown. Nothing here reads a clock, a
counter, or the environment, and the run ID is deliberately absent from the body:
the comment is rewritten in place on every run, so a body that changed when the
findings had not would show a reviewer a diff that means nothing.

Posting the comment belongs to :mod:`queryfix.integrations.github`.

The layout puts coverage gaps *above* the findings. A report over a partially
read change that leads with its findings reads as a complete review, and a
reviewer who trusts it stops looking — which is the failure mode the fail-soft
design exists to prevent (CLAUDE.md invariant 5). Caveats are sections, not
footnotes.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Sequence

from queryfix.integrations.github import COMMENT_MARKER, REVIEW_MARKER
from queryfix.models.finding import Evidence, Finding, Severity, Suggestion
from queryfix.models.query import ExtractedQuery, QueryKind
from queryfix.models.report import Report
from queryfix.policy import EnforcementStatus, ReviewResult

__all__ = ["DEFAULT_MAX_FINDINGS", "cap_findings", "rank_findings", "render_markdown"]

#: Severity sections, worst first. Iterated in this order rather than sorted, so a
#: severity added to the enum without a decision about where it belongs shows up
#: here as a missing section rather than silently sorting itself somewhere.
_SEVERITY_DISPLAY: tuple[tuple[Severity, str, str], ...] = (
    (Severity.CRITICAL, "🔴", "Critical"),
    (Severity.HIGH, "🟠", "High"),
    (Severity.MEDIUM, "🟡", "Medium"),
    (Severity.LOW, "🔵", "Low"),
    (Severity.INFO, "⚪", "Info"),
)

#: Derived from `_SEVERITY_DISPLAY` rather than declared again, so the sort order
#: `rank_findings` applies and the section order the report renders in cannot drift
#: apart into two answers for "which severity outranks which".
_SEVERITY_RANK: dict[Severity, int] = {
    severity: index for index, (severity, _, _) in enumerate(_SEVERITY_DISPLAY)
}

#: Default ceiling on findings shown in one report. A comment listing every last
#: `SELECT *` teaches its author to stop reading before the tenth one; capping
#: keeps the ones `rank_findings` already put first. Not a hidden constant a caller
#: has to know about — `AnalysisRunner` passes it explicitly, and any caller that
#: wants a different number overrides it at the same call site rather than editing
#: this file.
DEFAULT_MAX_FINDINGS = 20

#: GitHub rejects a review or comment body over 65,536 characters (422), which would
#: lose the whole review. Rendering stays well inside it so the posted body is never the
#: thing that fails: 60,000 leaves room for the marker, the status line, and any
#: surrounding markup GitHub adds.
MAX_BODY_CHARS = 60_000

#: Longest list of unreadable files or queries printed. A pilot repository had 55 SQL
#: files that could not be parsed; naming each is useful at 5 and noise at 55.
_LIST_CAP = 25

#: Fence language for quoted query text. JPQL and derived-method renderings are not
#: SQL, but GitHub has no lexer for them and `sql` highlights them acceptably; the
#: kind is stated in prose beside the block rather than implied by the fence.
_FENCE_LANGUAGE = "sql"

_BACKTICK_RUN = re.compile(r"`+")
_WHITESPACE = re.compile(r"\s+")


def rank_findings(findings: list[Finding]) -> list[Finding]:
    """Deduplicate and order findings for presentation, most important first.

    Deduplication collapses only exact repeats: the same rule, raised against the
    same query, anchored to the same location, saying the same thing. That shape is
    a real artifact of the pipeline rather than two problems that happen to look
    alike — the same source submitted twice in one request (a caller mistake
    ``AnalysisRunner`` tolerates rather than rejects) yields two ``ExtractedQuery``
    objects sharing one id, and the rule engine then raises the identical finding
    against each.

    Two genuinely different queries that happen to render the same text — two
    migrations with the same copy-pasted mistake, two derived methods with the
    same predicate — are deliberately NOT collapsed: each is wrong in its own
    place, and fixing one has not fixed the other. Identity is keyed on
    ``query_id``, not on query text, precisely to keep that distinction (see
    ``test_pipeline_contracts.py::test_identical_statements_are_reported_once_each_not_merged``).

    Ordering is severity, stable beyond that. A single stage's findings already
    arrive severity-sorted, so this re-sort is a no-op today; it becomes
    load-bearing the moment a second stage (plan analysis, N+1) contributes
    findings to the same report and the merged list needs one order, not two
    stages' orders concatenated.
    """
    seen: set[tuple[str, str | None, str, int | None, str]] = set()
    deduplicated: list[Finding] = []
    for finding in findings:
        key = (
            finding.rule_id,
            finding.query_id,
            finding.provenance.file,
            finding.provenance.line,
            finding.title,
        )
        if key in seen:
            continue
        seen.add(key)
        deduplicated.append(finding)

    deduplicated.sort(key=lambda finding: _SEVERITY_RANK[finding.severity])
    return deduplicated


def cap_findings(
    findings: list[Finding], *, max_findings: int = DEFAULT_MAX_FINDINGS
) -> tuple[list[Finding], int]:
    """Truncate to the ``max_findings`` highest-priority findings.

    Takes ranked input — call after :func:`rank_findings`, not instead of it — and
    truncates rather than filters, so what survives is always a prefix: whatever
    this drops is exactly what ranking already put last. Returns the number
    dropped alongside the kept findings, so a caller can say a report was capped
    rather than let a shortened list imply nothing more was found.
    """
    if max_findings < 0:
        raise ValueError("max_findings must not be negative")
    if len(findings) <= max_findings:
        return findings, 0
    return findings[:max_findings], len(findings) - max_findings


def render_markdown(
    report: Report,
    *,
    enforcement: ReviewResult | None = None,
    max_chars: int = MAX_BODY_CHARS,
) -> str:
    """Render a report as the Markdown body of the PR comment or Review.

    The body carries a hidden marker as its first line so a re-run finds and
    edits the existing one instead of adding another —
    :data:`queryfix.integrations.github.COMMENT_MARKER` for the plain
    issue-comment path, :data:`queryfix.integrations.github.REVIEW_MARKER`
    for the Review path. The two must never collide: a review search that
    happened to match on the comment marker (or vice versa) would let
    :func:`queryfix.integrations.github.upsert_review` "find" a plain
    comment and try to call review-only methods on it.

    ``enforcement`` is ``None`` for the plain issue-comment path
    (:func:`queryfix.integrations.github.upsert_report_comment`), which keeps
    rendering byte-identical to every existing snapshot — this function's
    long-standing contract. When supplied (the Pull Request Review path,
    :func:`queryfix.integrations.github.upsert_review`), the body leads with
    the deterministic PASS/BLOCKED/DEGRADED/FAILED status and separates
    blocking findings from everything else, matching the task's required
    review body shape.

    Pure: the same report and enforcement result always render byte-identical
    Markdown.

    The body is bounded by ``max_chars`` (GitHub refuses anything over 65,536, and a
    refused review is a lost review). When it would not fit, the lowest-priority
    findings are dropped first, repeatedly, counted in the summary exactly as the
    findings cap already is; as a last resort the text is cut at a block boundary with
    any open code fence closed, and says so.
    """
    body = _render(report, enforcement)
    shown = len(report.findings)
    while len(body) > max_chars and shown > 0:
        shown //= 2
        trimmed = report.model_copy(
            update={
                "findings": report.findings[:shown],
                "omitted_findings": report.omitted_findings + len(report.findings) - shown,
            }
        )
        body = _render(trimmed, enforcement)
    return body if len(body) <= max_chars else _hard_truncate(body, max_chars)


_TRUNCATION_NOTICE = (
    "\n\n---\n"
    "*This review was cut short because it exceeded GitHub's size limit for a "
    "review body. The findings above are the highest-priority ones.*\n"
)


def _hard_truncate(body: str, max_chars: int) -> str:
    """Cut at the last block boundary that fits, closing any code fence left open."""
    budget = max_chars - len(_TRUNCATION_NOTICE) - 8
    cut = body.rfind("\n\n", 0, max(budget, 0))
    head = body[: cut if cut > 0 else max(budget, 0)]
    if head.count("```") % 2 == 1:
        head += "\n```"
    return head + _TRUNCATION_NOTICE


def _render(report: Report, enforcement: ReviewResult | None) -> str:
    """The unbounded rendering; :func:`render_markdown` decides whether it fits."""
    queries = _queries_by_id(report.queries)
    unanalyzable = [query for query in report.queries if query.parse_error is not None]

    marker = REVIEW_MARKER if enforcement is not None else COMMENT_MARKER
    blocks: list[str] = [marker, "## QueryFix"]
    if enforcement is not None:
        blocks.append(_status_line(enforcement))
    blocks.append(_summary(report, unanalyzable))
    blocks.extend(_analysis_basis_note(report, unanalyzable))
    blocks.extend(_degraded_section(report.degraded_stages))
    blocks.extend(_unanalyzable_section(unanalyzable))
    blocks.extend(_findings_sections(report.findings, queries, enforcement))

    return "\n\n".join(blocks) + "\n"


def _status_line(result: ReviewResult) -> str:
    """State the merge decision before any informational report detail.

    A reviewer opening the review must not have to read findings to learn the
    outcome — the task's own example review body leads with ``Status:``.
    """
    line = f"Status: {result.status.value}"
    if result.status is EnforcementStatus.BLOCKED:
        line += f"\n\nBlocking findings: {len(result.blocking_findings)}"
    return line


def _summary(report: Report, unanalyzable: Sequence[ExtractedQuery]) -> str:
    """One line saying what was reviewed and what came of it.

    Reports the number of queries actually analyzed, not the number extracted —
    an unanalyzable candidate was read but not reviewed, and counting it would
    overstate the coverage this report represents.
    """
    analyzed = len(report.queries) - len(unanalyzable)

    if not report.queries:
        # Two different reasons a report can have no queries in it, and they must
        # not read the same: a change with nothing to review is a clean result, a
        # PR that could not even be fetched is not — see it degraded below, and a
        # reviewer must not be able to mistake the second for the first from the
        # headline alone.
        found = (
            "QueryFix could not analyze this pull request."
            if report.degraded_stages
            else "No queries were found in this change."
        )
    elif analyzed == 0:
        # Queries were found and none could be read. "Found no problems" would be
        # a clean bill of health for a review that never happened.
        found = (
            f"Found {_count(len(report.queries), 'query', 'queries')}, "
            f"none of which could be reviewed."
        )
    elif not report.findings and not report.omitted_findings:
        found = f"Reviewed {_count(analyzed, 'query', 'queries')} and found no problems."
    else:
        total_found = len(report.findings) + report.omitted_findings
        found = (
            f"Reviewed {_count(analyzed, 'query', 'queries')} and found "
            f"{_count(total_found, 'problem', 'problems')}."
        )
        if report.omitted_findings:
            # A capped report that only says "found 27 problems" while showing 20
            # would read as complete. Say what was cut, the same way a degraded
            # stage is named rather than left for the reader to notice by counting.
            noun = "finding" if report.omitted_findings == 1 else "findings"
            verb = "is" if report.omitted_findings == 1 else "are"
            found += (
                f" Showing the {len(report.findings)} highest-priority; "
                f"{report.omitted_findings} lower-priority {noun} {verb} omitted."
            )

    if report.unchanged_findings:
        noun = "finding" if report.unchanged_findings == 1 else "findings"
        found += (
            f" {report.unchanged_findings} more {noun} in code this change did not touch "
            f"{'is' if report.unchanged_findings == 1 else 'are'} not shown."
        )

    if report.degraded_stages or unanalyzable:
        return f"{found} **Part of this change could not be reviewed — see below.**"
    return found


def _analysis_basis_note(report: Report, unanalyzable: Sequence[ExtractedQuery]) -> list[str]:
    """State plainly that this review is static, once, rather than per finding.

    Every finding today comes from the query's source text — no database was
    provisioned, nothing ran, no plan was measured. A reviewer who reads "found no
    problems" without this could take it as "this query performs fine", which is a
    claim static analysis cannot make. Said once at the top rather than repeated on
    every finding: with a full report that is noise, and CLAUDE.md's own report
    layout already puts coverage caveats above the findings for the same reason —
    a claim about what the report can and cannot see belongs where it is read
    first.

    Silent when nothing was actually reviewed (an empty diff, or every candidate
    unanalyzable) — there is no analysis basis to state anything about.
    """
    if len(report.queries) - len(unanalyzable) <= 0:
        return []
    return [
        "*Static analysis only: every finding below comes from the query's source "
        "text, not a measured execution plan — nothing in this change was run "
        "against a database.*"
    ]


def _degraded_section(degraded_stages: Sequence[str]) -> list[str]:
    """The stages that failed soft, as a section a reviewer cannot skim past."""
    if not degraded_stages:
        return []

    return [
        "### ⚠️ Not fully analyzed",
        (
            "These stages failed and were skipped, so this report covers less than "
            "the change does. Everything below is still accurate — it is just not "
            "everything."
        ),
        _capped_list([_degraded_item(stage) for stage in degraded_stages]),
    ]


def _clip(item: str, limit: int = 300) -> str:
    """Bound one list entry: an exception message can run to kilobytes."""
    return item if len(item) <= limit else item[: limit - 1] + "…"


def _capped_list(items: Sequence[str]) -> str:
    """A bullet list that stops at :data:`_LIST_CAP` and says how many it left out."""
    lines = [f"- {_clip(item)}" for item in items[:_LIST_CAP]]
    if len(items) > _LIST_CAP:
        lines.append(f"- …and {len(items) - _LIST_CAP} more")
    return "\n".join(lines)


def _degraded_item(marker: str) -> str:
    """Render one ``degraded_stages`` marker.

    Markers are either a bare stage name or ``<stage>:<detail>`` — the runner
    qualifies extraction per source, because losing one file out of ten is a
    materially different report from losing all ten. Split generically rather than
    matching known stage names, so a stage added later renders without an edit
    here.
    """
    stage, separator, detail = marker.partition(":")
    if separator and detail:
        return f"**{stage}** — `{detail}`"
    return f"**{marker}**"


def _unanalyzable_section(unanalyzable: Sequence[ExtractedQuery]) -> list[str]:
    """Queries that were found but could not be parsed.

    Named rather than omitted: a query QueryFix could not read is not a query
    with no problems, and the difference is the whole point of reporting it.
    """
    if not unanalyzable:
        return []

    return [
        "### ⚠️ Queries that could not be parsed",
        ("These were found in the change but could not be read, so no rule ran against them."),
        _capped_list(
            [f"{_anchor(query)} — {_one_line(query.parse_error)}" for query in unanalyzable]
        ),
    ]


def _findings_sections(
    findings: Sequence[Finding],
    queries: dict[str, ExtractedQuery],
    enforcement: ReviewResult | None,
) -> list[str]:
    """One section per severity present, worst first.

    Without ``enforcement`` (the issue-comment path), unchanged from every
    existing snapshot: one flat run of severity sections. With it (the Review
    path), findings split into a "Blocking findings" group and a
    "Non-blocking findings" group first, each internally still ordered worst
    severity first — a reviewer must be able to tell at a glance which
    findings are the reason for ``REQUEST_CHANGES`` and which are along for
    the ride.
    """
    if enforcement is None:
        return _severity_sections(findings, queries, heading_level="###")

    blocks: list[str] = []
    if enforcement.blocking_findings:
        blocks.append("### 🚫 Blocking findings")
        blocks.extend(
            _severity_sections(enforcement.blocking_findings, queries, heading_level="####")
        )
    if enforcement.warning_findings:
        blocks.append("### Non-blocking findings")
        blocks.extend(
            _severity_sections(enforcement.warning_findings, queries, heading_level="####")
        )
    return blocks


def _severity_sections(
    findings: Sequence[Finding], queries: dict[str, ExtractedQuery], *, heading_level: str
) -> list[str]:
    """Render the severity groups within one findings section."""
    blocks: list[str] = []

    for severity, badge, label in _SEVERITY_DISPLAY:
        # Filtered in the order the findings arrived, so the engine's ranking
        # within a severity survives into the report.
        at_severity = [finding for finding in findings if finding.severity is severity]
        if not at_severity:
            continue

        blocks.append(f"{heading_level} {badge} {label}")
        for finding in at_severity:
            blocks.extend(_finding_blocks(finding, queries))

    return blocks


def _finding_blocks(finding: Finding, queries: dict[str, ExtractedQuery]) -> list[str]:
    """One finding: what, where, the query itself, why it matters, and the fix."""
    blocks = [f"#### {finding.title}", f"**Where:** {_finding_anchor(finding, queries)}"]

    query = queries.get(finding.query_id) if finding.query_id is not None else None
    if query is not None:
        if query.kind is QueryKind.SPRING_DATA_DERIVED:
            # Not SQL the application will run — see the confidence caveat below —
            # so it is not shown as if it were. `findByCustomerId` on a `@ManyToOne`
            # compiles to a JOIN this rendering cannot know about (CLAUDE.md).
            blocks.append(
                "*Inferred query shape, from the method name — not the literal SQL "
                "Spring Data will issue:*"
            )
        blocks.append(_code_block(query.text))

    blocks.append(finding.explanation)
    blocks.append(f"**Impact:** {finding.impact}")

    blocks.extend(_evidence_blocks(finding.evidence))
    blocks.extend(_suggestion_blocks(finding.suggestions))

    if finding.confidence is not None:
        blocks.append(_evidence_strength(finding))

    blocks.append(f"<sub>`{finding.rule_id}`</sub>")
    return blocks


def _evidence_strength(finding: Finding) -> str:
    """How far to trust a finding that could not be verified, in words and with reasons.

    Not a percentage. The underlying value is an ordinal per evidence tier, and nearly
    every finding in a review shares a tier, so "75%" on each one looked like a
    calibrated probability and said nothing about why. A band plus the specific
    conditions behind it is both honest about what the number is and useful.
    """
    value = finding.confidence if finding.confidence is not None else 0.0
    band = "strong" if value >= 0.7 else "moderate" if value >= 0.5 else "weak"
    reason = f" — {finding.confidence_basis}" if finding.confidence_basis else ""
    return (
        f"*Evidence strength: {band}, inferred{reason}. "
        f"This was not confirmed against a running system or a query plan.*"
    )


def _finding_anchor(finding: Finding, queries: dict[str, ExtractedQuery]) -> str:
    """Where a finding points, including the other queries a cross-query one spans."""
    anchor = _provenance_anchor(
        finding.provenance.file, finding.provenance.line, finding.provenance.symbol
    )

    related = [query_id for query_id in finding.query_ids if query_id != finding.query_id]
    if not related:
        return anchor

    # An N+1 spans a set of queries; naming only the first would hide what the
    # finding is actually about.
    others = ", ".join(f"`{_anchor_for_id(query_id, queries)}`" for query_id in related)
    return f"{anchor}, with {others}"


def _anchor_for_id(query_id: str, queries: dict[str, ExtractedQuery]) -> str:
    """A related query's location, falling back to its ID when it is not in scope."""
    query = queries.get(query_id)
    if query is None:
        return query_id
    return _location(query.provenance.file, query.provenance.line)


def _anchor(query: ExtractedQuery) -> str:
    """A query's ``file:line``, code-spanned."""
    return _provenance_anchor(query.provenance.file, query.provenance.line, query.provenance.symbol)


def _provenance_anchor(file: str, line: int | None, symbol: str | None) -> str:
    """``file:line``, with the enclosing symbol when the source named one.

    Every part is optional except the file, and an absent part is left out rather
    than rendered — a comment reading `Repo.java:None` is worse than one reading
    `Repo.java`.
    """
    anchor = f"`{_location(file, line)}`"
    if symbol is not None:
        return f"{anchor} (`{symbol}`)"
    return anchor


def _location(file: str, line: int | None) -> str:
    """``file:line``, or just the file when no line was resolved."""
    return f"{file}:{line}" if line is not None else file


def _evidence_blocks(evidence: Sequence[Evidence]) -> list[str]:
    """Supporting detail — a plan excerpt, a cost delta, a snippet."""
    if not evidence:
        return []

    return [
        "<details><summary>Evidence</summary>\n\n"
        + "\n".join(f"- **{item.label}:** {item.detail}" for item in evidence)
        + "\n\n</details>"
    ]


def _suggestion_blocks(suggestions: Sequence[Suggestion]) -> list[str]:
    """Every proposed fix, with its measured impact where one exists."""
    blocks: list[str] = []

    for suggestion in suggestions:
        blocks.append(f"**Suggested fix:** {suggestion.description}")
        if suggestion.sql is not None:
            blocks.append(_code_block(suggestion.sql))
        impact = _cost_delta(suggestion.cost_before, suggestion.cost_after)
        if impact is not None:
            blocks.append(impact)

    return blocks


def _cost_delta(before: float | None, after: float | None) -> str | None:
    """The simulated cost change, when both ends were measured.

    Both or neither: a "before" with no "after" is not a measurement, and printing
    half of one invites the reader to supply the other half themselves.
    """
    if before is None or after is None:
        return None

    line = f"**Simulated cost:** {before:,.2f} → {after:,.2f}"
    if before <= 0:
        return line

    # Rounded away from flattering the suggestion, never toward it. A 99.96%
    # reduction printed as "100% lower" reads as "this becomes free", which is a
    # claim about a *simulated* plan that nobody measured. Floor the improvement,
    # ceil the regression, so the number is never better than the truth.
    change = (before - after) / before
    if change >= 0:
        return f"{line} ({math.floor(change * 100)}% lower)"
    return f"{line} ({math.ceil(-change * 100)}% higher)"


def _code_block(text: str) -> str:
    """Fence ``text``, choosing a fence the content cannot break out of."""
    return f"{_fence(text)}{_FENCE_LANGUAGE}\n{text.strip()}\n{_fence(text)}"


def _fence(text: str) -> str:
    """A backtick fence longer than any backtick run inside ``text``.

    A quoted query containing a fenced block of its own — a query built in a
    Markdown-formatted string, say — would otherwise close the block early and
    spill the rest of the report into the page as prose.
    """
    longest = max((len(run) for run in _BACKTICK_RUN.findall(text)), default=0)
    return "`" * max(3, longest + 1)


def _one_line(text: str | None) -> str:
    """Collapse a message to one line so it can sit inside a list item."""
    if text is None:
        return "could not be parsed"
    collapsed = _WHITESPACE.sub(" ", text).strip()
    return collapsed if collapsed else "could not be parsed"


def _queries_by_id(queries: Iterable[ExtractedQuery]) -> dict[str, ExtractedQuery]:
    """Index queries by ID for finding-to-query lookup.

    First occurrence wins. IDs are unique within a file but not within a run — the
    same path submitted twice yields the same IDs (STATUS.md, TD-3) — so this is
    the point that would need a run-qualified ID. Until then the duplicate is a
    byte-identical copy of the same source, so which one is quoted does not change
    the rendered output.
    """
    index: dict[str, ExtractedQuery] = {}
    for query in queries:
        index.setdefault(query.id, query)
    return index


def _count(number: int, singular: str, plural: str) -> str:
    """``"1 query"`` / ``"3 queries"``."""
    return f"{number} {singular if number == 1 else plural}"
