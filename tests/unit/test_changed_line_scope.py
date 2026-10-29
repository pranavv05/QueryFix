"""Findings are scoped to the code a pull request touched.

Found on a real pilot: a PR that changed 225 lines was blocked by 16 findings, none of
them on a line it changed. Legacy code in a file the PR merely edited is not the PR's
to fix, and holding every edit to the file's history is how a gate gets switched off.
"""

from __future__ import annotations

from queryfix.models.query import Hunk, SourceFile
from queryfix.pipeline.report import render_markdown
from queryfix.pipeline.runner import AnalysisRunner
from queryfix.policy import EnforcementStatus

# line 1: a bad statement the PR did NOT touch; line 3: a bad statement it added.
SQL = "SELECT * FROM orders;\n\nSELECT * FROM customers;\n"


def _hunk(start: int, lines: int = 1) -> Hunk:
    return Hunk(base_start=start, base_lines=0, head_start=start, head_lines=lines)


def _run(hunks: list[Hunk], *, scope: bool = True):  # type: ignore[no-untyped-def]
    source = SourceFile(path="db/001.sql", content=SQL, hunks=hunks)
    return AnalysisRunner(scope_to_changes=scope).run_review(
        repo="acme/x", pr_number=1, sources=[source]
    )


def _lines(result) -> set[int | None]:  # type: ignore[no-untyped-def]
    return {finding.provenance.line for finding in result.findings}


def test_only_findings_on_changed_lines_are_kept() -> None:
    result = _run([_hunk(3)])

    assert _lines(result) == {3}
    assert result.report.unchanged_findings == 2  # no-limit + select-star on line 1


def test_an_unchanged_blocking_finding_does_not_block() -> None:
    # Line 1 holds a HIGH no-limit finding the PR never touched.
    result = _run([_hunk(2)])  # only the blank line changed

    assert result.findings == []
    assert result.status is EnforcementStatus.PASS
    assert result.report.unchanged_findings == 4


def test_a_changed_blocking_finding_still_blocks() -> None:
    result = _run([_hunk(1)])

    assert result.status is EnforcementStatus.BLOCKED
    assert {f.provenance.line for f in result.blocking_findings} == {1}


def test_a_source_with_no_hunks_is_entirely_in_scope() -> None:
    # SQL handed in whole (the HTTP API) did not come from a diff.
    result = _run([])

    assert _lines(result) == {1, 3}
    assert result.report.unchanged_findings == 0


def test_scoping_can_be_turned_off() -> None:
    result = _run([_hunk(3)], scope=False)

    assert _lines(result) == {1, 3}
    assert result.report.unchanged_findings == 0


def test_a_multi_line_statement_is_in_scope_when_any_of_its_lines_changed() -> None:
    content = "SELECT *\nFROM orders\nORDER BY id;\n"
    source = SourceFile(path="a.sql", content=content, hunks=[_hunk(3)])

    result = AnalysisRunner().run_review(repo="a/b", pr_number=1, sources=[source])

    assert result.findings  # the statement starts on line 1; line 3 changed


def test_the_summary_says_what_was_left_out() -> None:
    markdown = render_markdown(_run([_hunk(3)]).report)

    assert "2 more findings in code this change did not touch are not shown." in markdown


def test_a_clean_change_with_hidden_legacy_findings_says_so() -> None:
    markdown = render_markdown(_run([_hunk(2)]).report)

    assert "found no problems." in markdown
    assert "4 more findings in code this change did not touch are not shown." in markdown
