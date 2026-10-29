"""Deterministic merge-enforcement policy for QueryFix findings.

This module is the sole authority for turning a completed :class:`Report` into
one of four outcomes — :class:`EnforcementStatus` — and, for ``BLOCKED``,
which findings are the reason. It deliberately has no dependency on report
rendering or LLM providers: prose is useful to a reviewer, but cannot change
whether a pull request is blocked (CLAUDE.md: "Findings from Claude [or Groq]
are claims, not facts" — and, more directly, an LLM-authored explanation has
no field in :class:`~queryfix.models.finding.Finding` it could use to talk
its way out of a severity it was given).

Status semantics, matching the task's PASS/BLOCKED/DEGRADED/FAILED contract:

- **BLOCKED** — checked first, if a blocking finding exists. Takes priority
  over every other status, including FAILED: a real-world run
  surfaced the case this ordering exists for — the changed files were Java
  services with no ``@Query``/derived methods of their own, so extraction's
  ``queries`` list was empty, while N+1 detection (which reads control flow,
  not query declarations) still found a genuine blocking finding, and posting
  the review then failed. Zero queries plus a degraded stage is
  indistinguishable from a total ingestion failure by shape alone — but a
  finding existing at all is proof some analysis was reliable, so BLOCKED
  must not be buried under FAILED.
- **FAILED** — otherwise, if no reliable analysis was possible at all. Reuses
  the same signal :func:`queryfix.pipeline.report.render_markdown`'s own
  summary already keys its "QueryFix could not analyze this pull request"
  wording on: zero queries, zero findings, *and* at least one degraded stage.
  Zero queries with zero degraded stages is a clean, ordinary result (a
  README-only pull request), not a failure — the two must not read the same.
- **DEGRADED** — otherwise, if any stage failed soft (``report.degraded_stages``
  non-empty). A Groq failure never reaches this: a missing LLM explanation is
  invisible at the ``Finding``/``Report`` level (CLAUDE.md invariant 5's Groq
  clause is independent of this status by construction, not by a special
  case here).
- **PASS** — otherwise.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum

from queryfix.models.finding import Finding, Severity
from queryfix.models.report import Report

__all__ = [
    "DEFAULT_BLOCKING_SEVERITIES",
    "EnforcementPolicy",
    "EnforcementStatus",
    "InvalidEnforcementPolicy",
    "ReviewResult",
    "parse_rule_ids",
    "parse_severities",
]

#: CRITICAL and HIGH block by default — matches the real-world example this
#: task was written against (a HIGH N+1 finding). MEDIUM/LOW/INFO are
#: non-blocking unless a caller narrows or widens this via configuration.
DEFAULT_BLOCKING_SEVERITIES = frozenset({Severity.CRITICAL, Severity.HIGH})


class InvalidEnforcementPolicy(ValueError):
    """Raised when an enforcement setting cannot be interpreted safely."""


class EnforcementStatus(str, Enum):
    """The deterministic outcome of one completed review."""

    PASS = "PASS"
    BLOCKED = "BLOCKED"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"


def parse_severities(value: str | None, *, setting_name: str) -> frozenset[Severity]:
    """Parse a comma-separated severity setting with clear validation errors.

    ``None`` means "use the caller's default" and is therefore not accepted
    here; callers choose their own default before parsing. An empty or
    whitespace-only string deliberately means an empty set, which lets an
    operator disable a particular policy dimension explicitly rather than
    relying on an ambiguous missing-variable behavior.
    """
    if value is None:
        msg = f"{setting_name} must be set before it can be parsed"
        raise InvalidEnforcementPolicy(msg)

    raw_names = [name.strip() for name in value.split(",")]
    if not any(raw_names):
        return frozenset()
    if any(not name for name in raw_names):
        msg = f"{setting_name} contains an empty severity name"
        raise InvalidEnforcementPolicy(msg)

    parsed: set[Severity] = set()
    for name in raw_names:
        try:
            parsed.add(Severity(name.lower()))
        except ValueError as error:
            choices = ", ".join(severity.name for severity in Severity)
            msg = f"{setting_name} has invalid severity {name!r}; expected one of: {choices}"
            raise InvalidEnforcementPolicy(msg) from error
    return frozenset(parsed)


def parse_rule_ids(value: str | None, *, setting_name: str) -> frozenset[str]:
    """Parse optional comma-separated rule IDs, rejecting accidental blanks."""
    if value is None or not value.strip():
        return frozenset()
    rule_ids = [rule_id.strip() for rule_id in value.split(",")]
    if any(not rule_id for rule_id in rule_ids):
        msg = f"{setting_name} contains an empty rule ID"
        raise InvalidEnforcementPolicy(msg)
    return frozenset(rule_ids)


@dataclass(frozen=True, slots=True)
class EnforcementPolicy:
    """One deterministic policy for classifying QueryFix findings.

    Rule-level fields are supported even though severity-based policy is the
    normal configuration, so a narrowly-scoped exemption (a noisy rule an
    operator wants to keep visible without it blocking) never needs a second
    policy implementation. Precedence, most specific first: an explicit
    ignored or warning rule always wins; an explicit blocking rule then wins
    over severity; severity is the fallback.
    """

    blocking_severities: frozenset[Severity] = field(
        default_factory=lambda: DEFAULT_BLOCKING_SEVERITIES
    )
    ignored_severities: frozenset[Severity] = field(default_factory=frozenset)
    blocking_rule_ids: frozenset[str] = field(default_factory=frozenset)
    warning_rule_ids: frozenset[str] = field(default_factory=frozenset)
    ignored_rule_ids: frozenset[str] = field(default_factory=frozenset)
    #: Report everything, block nothing. Checked before any other rule, so neither a
    #: ``block-rules`` entry in a repository's own config nor a blocking severity can
    #: re-enable a gate the organisation turned off. This is the pilot setting: a team
    #: sees real findings on real PRs before anything is allowed to stop a merge.
    advisory: bool = False

    def should_block(self, finding: Finding) -> bool:
        """Whether this structured finding blocks under this policy.

        Only ``Finding.rule_id`` and ``Finding.severity`` participate. Title,
        confidence, and any LLM-authored explanation are deliberately absent
        from this decision — see the module docstring.
        """
        if self.advisory:
            return False
        if finding.rule_id in self.ignored_rule_ids | self.warning_rule_ids:
            return False
        if finding.rule_id in self.blocking_rule_ids:
            return True
        if finding.severity in self.ignored_severities:
            return False
        return finding.severity in self.blocking_severities

    def blocking_findings(self, findings: Sequence[Finding]) -> list[Finding]:
        """Every blocking finding, preserving deterministic rank order."""
        return [finding for finding in findings if self.should_block(finding)]

    def evaluate(
        self, report: Report, *, findings: Sequence[Finding] | None = None
    ) -> ReviewResult:
        """Produce the complete enforcement result for a completed report.

        ``findings`` exists for a caller that ranked/capped a longer list
        before building ``report`` (:class:`~queryfix.pipeline.runner.AnalysisRunner`
        caps a report's displayed findings, but enforcement must still see
        every finding it found — a blocking finding must not slip through
        because it was the 21st one and the comment only shows 20).
        """
        authoritative_findings = list(report.findings if findings is None else findings)
        blocking = self.blocking_findings(authoritative_findings)
        warnings = [finding for finding in authoritative_findings if finding not in blocking]

        if blocking:
            # Takes priority over every other check, including the FAILED
            # signal below: a real-world run surfaced exactly this
            # case — extraction found zero queries in the changed Java files
            # (they were services, not repository interfaces), N+1 found a
            # genuine blocking finding by reading control flow, and posting
            # the review then failed (a permissions gap). Zero queries plus a
            # degraded stage looked identical to a total ingestion failure
            # until this check moved first — but a finding existing at all is
            # proof some analysis was reliable, so it must not be buried
            # under FAILED.
            status = EnforcementStatus.BLOCKED
        elif not report.queries and not authoritative_findings and report.degraded_stages:
            # No reliable analysis was possible at all — the same signal
            # pipeline/report.py's own summary already keys "QueryFix could
            # not analyze this pull request" on. Zero queries with zero
            # degraded stages is an ordinary clean result (nothing to
            # review), not a failure, so both conditions are required; zero
            # findings is required too, per the case above.
            status = EnforcementStatus.FAILED
        elif report.degraded_stages:
            status = EnforcementStatus.DEGRADED
        else:
            status = EnforcementStatus.PASS

        return ReviewResult(
            report=report,
            findings=authoritative_findings,
            blocking_findings=blocking,
            warning_findings=warnings,
            status=status,
        )


@dataclass(frozen=True, slots=True)
class ReviewResult:
    """A report plus the independent, deterministic enforcement decision."""

    report: Report
    findings: list[Finding]
    blocking_findings: list[Finding]
    warning_findings: list[Finding]
    status: EnforcementStatus
