"""Stage 1 — reading a pull request's diff into sources.

Two halves. The first is patch parsing, tested against hand-written unified diffs
because the interesting inputs are the malformed ones a real PR will not supply.
The second is :func:`build_sources`, tested against the **recorded** pull request
in ``tests/fixtures/diffs/`` — a real GitHub payload from a real PR, carrying one
file per case this stage has to get right.

Nothing here opens a socket.
"""

from __future__ import annotations

import pytest

from queryfix.models.diff import ChangedFile, ChangeStatus, DiffIngest, SkipReason
from queryfix.models.query import Hunk, SourceFile
from queryfix.models.report import RunContext
from queryfix.pipeline.diff import (
    INGEST_STAGE,
    MalformedPatch,
    ReadHeadFile,
    build_sources,
    parse_hunks,
)
from queryfix.pipeline.extract import ExtractorRegistry, SqlExtractor, extract_source
from tests.conftest import RecordedPullRequest

# --- Fixtures ------------------------------------------------------------------

V1 = "V1__create_tables.sql"
V2 = "V2__reporting_indexes.sql"
ORDER_REPOSITORY = "repository/OrderRepository.java"
ORDER_LINE_REPOSITORY = "repository/OrderLineRepository.java"
DELETED_ENUM = "domain/OrderStatus.java"
README = "queryfix-sandbox/README.md"


@pytest.fixture
def context() -> RunContext:
    return RunContext(run_id="test-run", repo="pranavv05/QueryFix", pr_number=4)


def ingest(
    context: RunContext,
    recorded_pr: RecordedPullRequest,
    files: list[ChangedFile] | None = None,
    reader: ReadHeadFile | None = None,
    registry: ExtractorRegistry | None = None,
) -> DiffIngest:
    """Run :func:`build_sources` over the recording, with optional substitutions."""

    def from_recording(path: str) -> str:
        return recorded_pr.head_text[path]

    return build_sources(
        context,
        files if files is not None else recorded_pr.changed_files(),
        reader if reader is not None else from_recording,
        registry=registry,
    )


# --- Patch parsing -------------------------------------------------------------


def test_a_hunk_header_yields_both_coordinate_systems() -> None:
    hunks = parse_hunks("@@ -57,3 +60,4 @@\n ctx\n ctx\n ctx\n+added\n")

    assert hunks == [
        Hunk(base_start=57, base_lines=3, head_start=60, head_lines=4, added_ranges=[(63, 63)])
    ]


def test_an_omitted_count_means_one_line() -> None:
    # The unified-diff convention, and the classic off-by-one in a hand-rolled
    # parser: `@@ -1 +1 @@` is a one-line hunk, not a zero-line one.
    assert parse_hunks("@@ -1 +1 @@\n-old\n+new\n") == [
        Hunk(base_start=1, base_lines=1, head_start=1, head_lines=1, added_ranges=[(1, 1)])
    ]


def test_a_new_file_starts_the_base_side_at_zero() -> None:
    hunks = parse_hunks("@@ -0,0 +1,2 @@\n+one\n+two\n")

    assert hunks == [
        Hunk(base_start=0, base_lines=0, head_start=1, head_lines=2, added_ranges=[(1, 2)])
    ]


def test_removed_lines_do_not_advance_the_head_side() -> None:
    # The whole point of two coordinate systems: three base lines become one head
    # line, so anything counting raw patch lines would be two off.
    assert parse_hunks("@@ -4,3 +4,1 @@\n-gone\n-gone\n kept\n") == [
        Hunk(base_start=4, base_lines=3, head_start=4, head_lines=1)
    ]


def test_several_hunks_keep_their_own_offsets() -> None:
    patch = "@@ -1,2 +1,2 @@\n a\n b\n@@ -40,1 +40,2 @@\n c\n+d\n"

    assert [(hunk.head_start, hunk.head_end) for hunk in parse_hunks(patch)] == [(1, 2), (40, 41)]


def test_the_no_newline_marker_is_not_a_line() -> None:
    # `\ No newline at end of file` annotates the line above it. Counting it makes
    # every hunk one line too long and fails the length check below.
    assert parse_hunks("@@ -1,1 +1,1 @@\n-old\n+new\n\\ No newline at end of file\n") == [
        Hunk(base_start=1, base_lines=1, head_start=1, head_lines=1, added_ranges=[(1, 1)])
    ]


def test_a_blank_context_line_counts_even_without_its_leading_space() -> None:
    # A well-formed diff writes " " for a blank context line, but trailing
    # whitespace does not survive every editor or mail gateway. Reading it as what
    # it plainly is costs nothing; rejecting it would fail patches that are fine.
    assert parse_hunks("@@ -1,3 +1,3 @@\n a\n\n b\n") == [
        Hunk(base_start=1, base_lines=3, head_start=1, head_lines=3)
    ]


def test_a_file_preamble_before_the_first_hunk_is_ignored() -> None:
    patch = (
        "diff --git a/x.sql b/x.sql\nindex 111..222 100644\n--- a/x.sql\n+++ b/x.sql\n"
        "@@ -1,1 +1,1 @@\n+one\n"
    )

    assert parse_hunks(patch) == [
        Hunk(base_start=1, base_lines=1, head_start=1, head_lines=1, added_ranges=[(1, 1)])
    ]


def test_an_empty_patch_has_no_hunks() -> None:
    assert parse_hunks("") == []


def test_a_hunk_shorter_than_it_declares_is_malformed() -> None:
    # The check that stops a mis-read patch from producing plausible-looking hunks.
    # A wrong hunk anchors a finding to a line the author never wrote, which is
    # worse than admitting the file could not be read.
    with pytest.raises(MalformedPatch, match="declares 4 line"):
        parse_hunks("@@ -1,4 +1,4 @@\n a\n b\n")


def test_a_hunk_longer_than_it_declares_is_malformed() -> None:
    with pytest.raises(MalformedPatch, match="contains 3"):
        parse_hunks("@@ -1,2 +1,2 @@\n a\n b\n c\n")


def test_the_length_check_covers_the_last_hunk_too() -> None:
    # The easy bug: check on seeing the *next* header, and never check the final
    # hunk because no header follows it.
    with pytest.raises(MalformedPatch):
        parse_hunks("@@ -1,1 +1,1 @@\n a\n@@ -9,1 +9,3 @@\n b\n")


def test_content_with_no_hunk_header_is_malformed() -> None:
    with pytest.raises(MalformedPatch, match="no hunk header"):
        parse_hunks("this is not a diff\n")


def test_an_unrecognized_line_inside_a_hunk_is_malformed() -> None:
    with pytest.raises(MalformedPatch, match="unrecognized"):
        parse_hunks("@@ -1,2 +1,2 @@\n a\n?what\n")


# --- Hunk and SourceFile geometry ----------------------------------------------


@pytest.mark.parametrize(
    ("line", "expected"),
    [(9, False), (10, True), (12, True), (14, True), (15, False)],
    ids=["before", "first", "middle", "last", "after"],
)
def test_a_hunk_covers_its_endpoints_inclusively(line: int, expected: bool) -> None:
    assert Hunk(base_start=1, base_lines=5, head_start=10, head_lines=5).covers(line) is expected


def test_a_pure_deletion_covers_no_head_line() -> None:
    # git writes `@@ -5,3 +4,0 @@` for a hunk that only removes lines: head_start
    # is the line *before* the gap. Nothing was added, so nothing can be anchored,
    # and an off-by-one here would claim the untouched line above was changed.
    hunk = Hunk(base_start=5, base_lines=3, head_start=4, head_lines=0)

    assert hunk.head_end == 3
    assert not any(hunk.covers(line) for line in range(0, 10))


def test_a_source_with_no_hunks_treats_every_line_as_in_scope() -> None:
    # Sources handed to POST /analyze directly did not come from a diff. Answering
    # "unchanged" for them would silently drop every finding the API ever produced.
    source = SourceFile(path="inline.sql", content="SELECT 1")

    assert source.is_changed(1)
    assert source.is_changed(9999)


def test_a_diff_source_separates_changed_lines_from_the_rest() -> None:
    source = SourceFile(
        path="m.sql",
        content="a\nb\nc\nd\n",
        hunks=[Hunk(base_start=2, base_lines=1, head_start=2, head_lines=2)],
    )

    assert [line for line in range(1, 5) if source.is_changed(line)] == [2, 3]


# --- The recorded pull request: routing ----------------------------------------


def test_the_recording_still_carries_every_case_this_stage_must_handle(
    recorded_pr: RecordedPullRequest,
) -> None:
    # A guard on the fixture, not on the code. If someone re-records the PR against
    # a different branch and loses the rename or the deletion, the tests below
    # would still pass while testing nothing — this is what fails instead.
    statuses = {str(entry["status"]) for entry in recorded_pr.entries}

    assert {"added", "modified", "renamed", "removed"} <= statuses
    assert recorded_pr.entry(ORDER_REPOSITORY)["patch"].count("@@ -") == 2
    assert recorded_pr.entry(DELETED_ENUM)["status"] == "removed"
    assert recorded_pr.entry(README)["status"] == "modified"


def test_a_real_pull_request_becomes_one_source_per_analyzable_file(
    context: RunContext, recorded_pr: RecordedPullRequest
) -> None:
    result = ingest(context, recorded_pr)

    assert [source.path for source in result.sources] == [
        recorded_pr.path(ORDER_LINE_REPOSITORY),
        recorded_pr.path(ORDER_REPOSITORY),
        recorded_pr.path(V1),
        recorded_pr.path(V2),
    ]
    assert result.degraded_stages == []


def test_a_deleted_file_produces_no_source(
    context: RunContext, recorded_pr: RecordedPullRequest
) -> None:
    result = ingest(context, recorded_pr)
    deleted = recorded_pr.path(DELETED_ENUM)

    assert deleted not in [source.path for source in result.sources]
    assert any(
        skip.path == deleted and skip.reason is SkipReason.DELETED for skip in result.skipped
    )


def test_a_deletion_is_a_skip_and_never_a_degradation(
    context: RunContext, recorded_pr: RecordedPullRequest
) -> None:
    # A `.java` file was deleted, so the extension check cannot be what excludes
    # it — the status has to. Reporting a deletion as a degradation would tell a
    # reviewer QueryFix choked on a file it deliberately ignored.
    result = ingest(context, recorded_pr)

    assert result.degraded_stages == []


def test_a_language_no_extractor_claims_is_skipped_rather_than_degraded(
    context: RunContext, recorded_pr: RecordedPullRequest
) -> None:
    result = ingest(context, recorded_pr)
    readme = recorded_pr.path(README)

    assert any(
        skip.path == readme and skip.reason is SkipReason.UNSUPPORTED_LANGUAGE
        for skip in result.skipped
    )
    assert f"{INGEST_STAGE}:{readme}" not in result.degraded_stages


def test_the_head_text_of_an_unsupported_file_is_never_even_read(
    context: RunContext, recorded_pr: RecordedPullRequest
) -> None:
    # The skip has to happen before the fetch, or a pull request full of images
    # and lock files costs one request each to learn nothing.
    read: list[str] = []

    def recording_reader(path: str) -> str:
        read.append(path)
        return recorded_pr.head_text[path]

    ingest(context, recorded_pr, reader=recording_reader)

    assert recorded_pr.path(README) not in read
    assert recorded_pr.path(DELETED_ENUM) not in read


def test_a_renamed_file_is_anchored_to_its_new_path(
    context: RunContext, recorded_pr: RecordedPullRequest
) -> None:
    # The rename in the recording is OrderItemRepository -> OrderLineRepository.
    # Anchoring to the old path would point every finding at a file that does not
    # exist on the head commit, and GitHub would refuse to place the comment.
    result = ingest(context, recorded_pr)
    entry = recorded_pr.entry(ORDER_LINE_REPOSITORY)
    paths = [source.path for source in result.sources]

    assert entry["previous_filename"] not in paths
    assert entry["filename"] in paths


def test_every_changed_file_is_accounted_for_exactly_once(
    context: RunContext, recorded_pr: RecordedPullRequest
) -> None:
    # The contract of DiffIngest. A file in none of the three lists is a file
    # nobody can tell was never looked at, which is the silence invariant 5 exists
    # to prevent; a file in two is a report that contradicts itself.
    result = ingest(context, recorded_pr)

    accounted = (
        [source.path for source in result.sources]
        + [skip.path for skip in result.skipped]
        + [marker.split(":", 1)[1] for marker in result.degraded_stages]
    )

    assert sorted(accounted) == sorted(str(entry["filename"]) for entry in recorded_pr.entries)
    assert len(accounted) == len(set(accounted))


def test_the_language_set_is_injectable(
    context: RunContext, recorded_pr: RecordedPullRequest
) -> None:
    # The same seam the extract dispatcher offers. With only SQL registered, the
    # Java files become skips rather than sources — and still not degradations.
    sql_only = ExtractorRegistry()
    sql_only.register(".sql", SqlExtractor())

    result = ingest(context, recorded_pr, registry=sql_only)

    assert [source.path.rsplit("/", 1)[-1] for source in result.sources] == [V1, V2]
    assert result.degraded_stages == []


# --- The recorded pull request: fail-soft --------------------------------------


def test_a_file_whose_head_cannot_be_read_degrades_that_file_only(
    context: RunContext, recorded_pr: RecordedPullRequest
) -> None:
    broken = recorded_pr.path(ORDER_REPOSITORY)

    def failing_reader(path: str) -> str:
        if path == broken:
            msg = "boom"
            raise OSError(msg)
        return recorded_pr.head_text[path]

    result = ingest(context, recorded_pr, reader=failing_reader)

    assert result.degraded_stages == [f"{INGEST_STAGE}:{broken}"]
    assert broken not in [source.path for source in result.sources]
    assert len(result.sources) == 3


def test_a_malformed_patch_degrades_that_file_only(
    context: RunContext, recorded_pr: RecordedPullRequest
) -> None:
    files = recorded_pr.changed_files()
    target = next(f for f in files if f.path.endswith(V2))
    corrupted = [
        f.model_copy(update={"patch": "@@ -1,2 +1,9 @@\n+one\n"}) if f is target else f
        for f in files
    ]

    result = ingest(context, recorded_pr, files=corrupted)

    assert result.degraded_stages == [f"{INGEST_STAGE}:{target.path}"]
    assert len(result.sources) == 3


def test_a_pure_rename_is_a_skip_because_the_bytes_did_not_change(
    context: RunContext, recorded_pr: RecordedPullRequest
) -> None:
    # GitHub sends no patch for a file that only moved. Re-reporting its existing
    # queries under the new name would be noise on a PR that moved a file.
    moved = ChangedFile(
        path="db/moved.sql",
        status=ChangeStatus.RENAMED,
        previous_path="db/old.sql",
        patch=None,
    )

    result = ingest(context, recorded_pr, files=[moved])

    assert result.sources == []
    assert result.skipped[0].reason is SkipReason.NO_TEXT_CHANGE
    assert result.degraded_stages == []


def test_a_withheld_patch_on_a_file_that_did_change_degrades(
    context: RunContext, recorded_pr: RecordedPullRequest
) -> None:
    # GitHub omits the patch for binary files and for diffs past its size ceiling.
    # The changed region is then unknowable, and analyzing the whole file would
    # report on lines nobody touched — so this is a degradation, not a skip.
    oversized = ChangedFile(
        path="db/huge.sql", status=ChangeStatus.MODIFIED, patch=None, additions=90_000
    )

    result = ingest(context, recorded_pr, files=[oversized])

    assert result.sources == []
    assert result.skipped == []
    assert result.degraded_stages == [f"{INGEST_STAGE}:db/huge.sql"]


def test_a_status_this_release_does_not_know_is_analyzed_rather_than_dropped() -> None:
    # Strict parsing would let a status GitHub adds later take down a whole run.
    # Reading it as a modification errs toward analyzing the file, which is the
    # safe direction for a tool whose silence is meant to mean "looked, found
    # nothing".
    assert ChangeStatus.parse("teleported") is ChangeStatus.MODIFIED
    assert ChangeStatus.parse("renamed") is ChangeStatus.RENAMED


def test_github_spells_a_deletion_removed() -> None:
    # Pinned because "deleted" is the natural thing to write and would silently
    # turn every deletion into a modification of a file that is no longer there.
    assert ChangeStatus.REMOVED.value == "removed"


# --- Line anchoring: the head file, not the base -------------------------------


def test_a_finding_in_the_second_hunk_anchors_to_its_head_line(
    context: RunContext, recorded_pr: RecordedPullRequest
) -> None:
    # The case the whole design turns on. OrderRepository.java has two hunks; the
    # native @Query added by the second one sits at head line 55, thirty lines
    # below where a patch-relative count would put it. The assertion is not the
    # number itself but that the number *resolves* — line 55 of the head file is
    # the annotation.
    result = ingest(context, recorded_pr)
    source = next(s for s in result.sources if s.path.endswith(ORDER_REPOSITORY))

    refurb = next(q for q in extract_source(source) if "REFURB" in q.text)
    line = refurb.provenance.line
    assert line is not None

    assert source.content.split("\n")[line - 1].lstrip().startswith("@Query(")
    assert "REFURB" in source.content.split("\n")[line - 1]
    assert source.is_changed(line)


def test_the_hunks_separate_queries_the_pull_request_added_from_the_rest(
    context: RunContext, recorded_pr: RecordedPullRequest
) -> None:
    # Reading the whole head file means pre-existing queries are extracted too.
    # That is intended — they are context — and the hunks are what let a later
    # stage tell them apart. `exportAllOrders` is a planted bug that predates this
    # PR; `findRefurbishedItems` is what the PR added.
    result = ingest(context, recorded_pr)
    source = next(s for s in result.sources if s.path.endswith(ORDER_REPOSITORY))

    changed = {
        query.text: source.is_changed(query.provenance.line)
        for query in extract_source(source)
        if query.provenance.line is not None
    }

    assert changed["SELECT * FROM order_items WHERE sku LIKE '%-REFURB'"] is True
    assert changed["SELECT * FROM orders"] is False


def test_a_query_added_outside_its_own_hunk_context_is_still_found(
    context: RunContext, recorded_pr: RecordedPullRequest
) -> None:
    # Why sources carry the whole head file rather than a reconstructed patch.
    # `findByStatusAndCustomerId` is a derived method the PR adds, and the Java
    # extractor only finds derived methods inside a repository interface — whose
    # declaration is thirty lines above the hunk and therefore absent from the
    # patch. Reconstructing would have reported nothing about a query the pull
    # request had just introduced.
    result = ingest(context, recorded_pr)
    source = next(s for s in result.sources if s.path.endswith(ORDER_REPOSITORY))

    added = next(
        q for q in extract_source(source) if q.provenance.symbol == "findByStatusAndCustomerId"
    )

    assert added.provenance.line is not None
    assert source.is_changed(added.provenance.line)


def test_a_line_appended_far_down_a_file_anchors_where_it_really_is(
    context: RunContext, recorded_pr: RecordedPullRequest
) -> None:
    # V1__create_tables.sql gains one statement at its end. Its single hunk starts
    # at head line 57, so anything that counted from the top of the patch would be
    # fifty-six lines out.
    result = ingest(context, recorded_pr)
    source = next(s for s in result.sources if s.path.endswith(V1))

    appended = next(q for q in extract_source(source) if q.text.startswith("SELECT"))
    line = appended.provenance.line
    assert line is not None

    assert line > 55
    assert source.content.split("\n")[line - 1] == "SELECT * FROM order_items;"
    assert source.is_changed(line)


def test_an_added_file_anchors_from_its_own_first_line(
    context: RunContext, recorded_pr: RecordedPullRequest
) -> None:
    result = ingest(context, recorded_pr)
    source = next(s for s in result.sources if s.path.endswith(V2))

    lines = source.content.split("\n")
    for query in extract_source(source):
        assert query.provenance.line is not None
        assert source.is_changed(query.provenance.line)
        # Every statement of a wholly new file is inside the one hunk, and every
        # recorded line resolves to text that starts that statement.
        assert lines[query.provenance.line - 1].startswith(query.text.split("\n")[0][:20])


def test_a_renamed_file_anchors_its_new_query_under_the_new_path(
    context: RunContext, recorded_pr: RecordedPullRequest
) -> None:
    result = ingest(context, recorded_pr)
    source = next(s for s in result.sources if s.path.endswith(ORDER_LINE_REPOSITORY))

    added = next(q for q in extract_source(source) if q.provenance.symbol == "findBySku")

    assert added.provenance.file == source.path
    assert added.provenance.line is not None
    assert source.is_changed(added.provenance.line)


# --- the exact lines a change wrote, as opposed to the context around them ----------


def test_added_ranges_exclude_the_context_github_pads_a_hunk_with() -> None:
    patch = "@@ -10,6 +10,7 @@\n c1\n c2\n c3\n+new\n c4\n c5\n c6\n"

    (hunk,) = parse_hunks(patch)

    assert hunk.added_ranges == [(13, 13)]
    assert hunk.covers(11)  # context: inside the hunk...
    assert not hunk.touches(11)  # ...but not written by the change
    assert hunk.touches(13)
    assert not hunk.touches(15)


def test_adjacent_added_lines_form_one_range_and_a_gap_starts_another() -> None:
    patch = "@@ -1,3 +1,5 @@\n+a\n+b\n ctx\n-gone\n+c\n ctx\n"

    (hunk,) = parse_hunks(patch)

    assert hunk.added_ranges == [(1, 2), (4, 4)]


def test_a_pure_deletion_adds_no_line() -> None:
    (hunk,) = parse_hunks("@@ -4,3 +4,1 @@\n-gone\n-gone\n kept\n")

    assert hunk.added_ranges == []


def test_a_hunk_with_no_known_ranges_falls_back_to_its_span() -> None:
    hunk = Hunk(base_start=1, base_lines=0, head_start=5, head_lines=3)

    assert hunk.touches(6)
    assert not hunk.touches(9)
