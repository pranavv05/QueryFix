"""The shapes the detector must not report, and the ones it must only hedge.

A performance reviewer stops reading a bot that cries wolf, so these matter more
than the positive cases. Most assert silence. Two deliberately do not: a loop over
a fixed collection and a batch job that means to query per item are both
indistinguishable from an N+1 in source, and the honest behaviour is to report
them as *potential* with a confidence attached rather than to guess. Those two
tests pin the hedging instead of pinning silence, because silently dropping them
would also drop the real N+1s that look identical.
"""

from __future__ import annotations

from collections.abc import Callable

from queryfix.models.finding import Finding
from queryfix.models.nplusone import EvidenceTier, NPlusOneCandidate
from queryfix.models.query import SourceFile

Detect = Callable[..., list[Finding]]
Candidates = Callable[..., list[NPlusOneCandidate]]
Build = Callable[..., SourceFile]

DERIVED = "List<Thing> findByParentId(Long parentId);"


def test_a_repository_call_outside_any_loop_is_silent(
    candidates: Candidates, service: Build, repository: Build
) -> None:
    assert (
        candidates(
            service("        thingRepository.findByParentId(rootId);"),
            repository(DERIVED),
        )
        == []
    )


def test_a_loop_containing_no_repository_call_is_silent(
    candidates: Candidates, service: Build, repository: Build
) -> None:
    assert (
        candidates(
            service(
                """
        for (Parent parent : parents) {
            total += parent.getAmount();
        }
"""
            ),
            repository(DERIVED),
        )
        == []
    )


def test_a_repository_shaped_name_on_a_non_repository_type_is_silent(
    candidates: Candidates, service: Build, repository: Build
) -> None:
    """Resolution is by declared type, never by the identifier's name."""
    assert (
        candidates(
            service(
                """
        for (Parent parent : parents) {
            thingRepository.findByParentId(parent.getId());
        }
""",
                fields="    private final ThingService thingRepository;",
                constructor="",
            ),
            repository(DERIVED),
        )
        == []
    )


def test_a_batched_finder_called_outside_the_loop_is_silent(
    candidates: Candidates, service: Build, repository: Build
) -> None:
    assert (
        candidates(
            service(
                """
        List<Thing> things = thingRepository.findByParentIdIn(parentIds);
        for (Parent parent : parents) {
            total += things.size();
        }
""",
                signature="public void run(List<Parent> parents, List<Long> parentIds)",
            ),
            repository("List<Thing> findByParentIdIn(Collection<Long> parentIds);"),
        )
        == []
    )


def test_a_single_call_before_the_loop_is_silent(
    candidates: Candidates, service: Build, repository: Build
) -> None:
    assert (
        candidates(
            service(
                """
        List<Thing> things = thingRepository.findAll();
        for (Parent parent : parents) {
            total += parent.getAmount();
        }
"""
            ),
            repository(DERIVED),
        )
        == []
    )


def test_a_single_call_after_the_loop_is_silent(
    candidates: Candidates, service: Build, repository: Build
) -> None:
    assert (
        candidates(
            service(
                """
        for (Parent parent : parents) {
            total += parent.getAmount();
        }
        List<Thing> things = thingRepository.findAll();
"""
            ),
            repository(DERIVED),
        )
        == []
    )


def test_an_in_memory_lookup_inside_a_loop_is_silent(
    candidates: Candidates, service: Build, repository: Build
) -> None:
    assert (
        candidates(
            service(
                """
        for (Parent parent : parents) {
            Thing thing = index.get(parent.getId());
        }
"""
            ),
            repository(DERIVED),
        )
        == []
    )


def test_a_cached_result_consumed_inside_a_loop_is_silent(
    candidates: Candidates, service: Build, repository: Build
) -> None:
    """The batched rewrite this detector recommends must not itself be flagged."""
    assert (
        candidates(
            service(
                """
        Map<Long, List<Thing>> byParent = thingRepository.findAll().stream()
                .collect(Collectors.groupingBy(thing -> thing.getParentId()));
        for (Parent parent : parents) {
            List<Thing> things = byParent.getOrDefault(parent.getId(), List.of());
        }
"""
            ),
            repository(DERIVED),
        )
        == []
    )


def test_a_loop_over_a_fixed_collection_is_hedged_rather_than_asserted(
    candidates: Candidates, service: Build, repository: Build
) -> None:
    """Cardinality is not knowable from source, so this is reported, not claimed.

    A loop over an enum's values runs a bounded number of times and is usually
    fine. Nothing in the source distinguishes it from a loop over a million rows,
    and suppressing it by guessing at collection size would suppress real N+1s
    with it — so it is reported at a tier that says the evidence is structural.
    """
    (candidate,) = candidates(
        service(
            """
        for (Status status : Status.values()) {
            thingRepository.findByStatus(status);
        }
"""
        ),
        repository("List<Thing> findByStatus(Status status);"),
    )

    assert candidate.tier is not EvidenceTier.VERY_HIGH
    assert candidate.tier.confidence is not None


def test_a_deliberate_per_item_batch_job_is_hedged_rather_than_asserted(
    detect: Detect, service: Build, repository: Build
) -> None:
    """Intent is not visible in source; the title stays "Potential"."""
    findings = detect(
        service(
            """
        for (Parent parent : parents) {
            thingRepository.findByParentId(parent.getId());
        }
"""
        ),
        repository(DERIVED),
    )

    (finding,) = findings
    assert finding.title.startswith("Potential")
    assert finding.confidence is not None
    assert "no query was executed" in " ".join(item.detail for item in finding.evidence)


def test_unrelated_repositories_do_not_cross_contaminate(
    candidates: Candidates, service: Build, repository: Build
) -> None:
    found = candidates(
        service(
            """
        List<Other> others = otherRepository.findAll();
        for (Parent parent : parents) {
            thingRepository.findByParentId(parent.getId());
        }
""",
            fields=(
                "    private final ThingRepository thingRepository;\n"
                "    private final OtherRepository otherRepository;"
            ),
            constructor="",
        ),
        repository(DERIVED),
        repository("List<Other> findAll();", name="OtherRepository"),
    )

    assert [candidate.repository_type for candidate in found] == ["ThingRepository"]


def test_the_same_method_name_on_another_repository_is_attributed_correctly(
    candidates: Candidates, service: Build, repository: Build
) -> None:
    (candidate,) = candidates(
        service(
            """
        for (Parent parent : parents) {
            otherRepository.findByParentId(parent.getId());
        }
""",
            fields=(
                "    private final ThingRepository thingRepository;\n"
                "    private final OtherRepository otherRepository;"
            ),
            constructor="",
        ),
        repository(DERIVED),
        repository(DERIVED, name="OtherRepository"),
    )

    assert candidate.repository_type == "OtherRepository"
    assert candidate.declaration_file is not None
    assert candidate.declaration_file.endswith("OtherRepository.java")


def test_a_nested_classes_field_does_not_resolve_an_outer_call(
    candidates: Candidates, service: Build, repository: Build
) -> None:
    """Scope is innermost-first; a nested member must not leak outward."""
    assert (
        candidates(
            service(
                """
        for (Parent parent : parents) {
            thingRepository.findByParentId(parent.getId());
        }
""",
                fields="",
                constructor="",
                extra="""
    private static final class Helper {
        private final ThingRepository thingRepository;
    }
""",
            ),
            repository(DERIVED),
        )
        == []
    )


def test_an_anonymous_class_outside_a_loop_is_silent(
    candidates: Candidates, service: Build, repository: Build
) -> None:
    assert (
        candidates(
            service(
                """
        Runnable task = new Runnable() {
            @Override
            public void run() {
                thingRepository.findByParentId(rootId);
            }
        };
"""
            ),
            repository(DERIVED),
        )
        == []
    )


def test_a_repository_call_written_in_a_comment_is_silent(
    candidates: Candidates, service: Build, repository: Build
) -> None:
    assert (
        candidates(
            service(
                """
        for (Parent parent : parents) {
            // thingRepository.findByParentId(parent.getId());
            /* thingRepository.findByParentId(parent.getId()); */
            total += parent.getAmount();
        }
"""
            ),
            repository(DERIVED),
        )
        == []
    )


def test_java_inside_a_string_literal_is_silent(
    candidates: Candidates, service: Build, repository: Build
) -> None:
    assert (
        candidates(
            service(
                """
        String snippet = "for (Parent p : parents) { thingRepository.findByParentId(p.getId()); }";
        log.info(snippet);
"""
            ),
            repository(DERIVED),
        )
        == []
    )


def test_a_commented_out_repository_interface_is_not_a_declaration(
    candidates: Candidates, service: Build
) -> None:
    """A declaration that exists only in a comment must not resolve anything."""
    ghost = SourceFile(
        path="src/main/java/com/example/data/GhostRepository.java",
        content=(
            "package com.example.data;\n"
            "// public interface GhostRepository extends JpaRepository<Ghost, Long> {\n"
            "//     List<Ghost> findByParentId(Long parentId);\n"
            "// }\n"
        ),
    )

    found = candidates(
        service(
            """
        for (Parent parent : parents) {
            ghostRepository.findByParentId(parent.getId());
        }
""",
            fields="    private final GhostRepository ghostRepository;",
            constructor="",
        ),
        ghost,
    )

    # The call still reports — the field's type is repository-shaped — but only at
    # the tier that says so, because no declaration was ever seen.
    assert [candidate.repository_resolved for candidate in found] == [False]
    assert found[0].tier is EvidenceTier.LOW


def test_a_lambda_that_is_not_per_element_is_silent(
    candidates: Candidates, service: Build, repository: Build
) -> None:
    """`Optional.map` runs at most once; reading it as a loop invents an N+1."""
    assert (
        candidates(
            service(
                """
        Optional<Parent> maybe = parents.stream().findFirst();
        maybe.map(parent -> thingRepository.findByParentId(parent.getId()));
        maybe.ifPresent(parent -> thingRepository.findByParentId(parent.getId()));
        transactionTemplate.execute(status -> thingRepository.findByParentId(rootId));
"""
            ),
            repository(DERIVED),
        )
        == []
    )


def test_a_method_reference_is_not_read_as_a_call(
    candidates: Candidates, service: Build, repository: Build
) -> None:
    """Whether a method reference runs per element depends on what consumes it."""
    assert (
        candidates(
            service("        parentIds.forEach(thingRepository::findByParentId);"),
            repository(DERIVED),
        )
        == []
    )


def test_identical_declarations_on_unrelated_repositories_stay_separate(
    candidates: Candidates, service: Build, repository: Build
) -> None:
    """Two repositories declaring the same JPQL are two queries, not one."""
    jpql = (
        '@Query("SELECT t FROM Thing t WHERE t.parentId = :id")\n    List<Thing> lookup(Long id);'
    )

    found = candidates(
        service(
            """
        for (Parent parent : parents) {
            thingRepository.lookup(parent.getId());
        }
""",
            fields=(
                "    private final ThingRepository thingRepository;\n"
                "    private final OtherRepository otherRepository;"
            ),
            constructor="",
        ),
        repository(jpql),
        repository(jpql, name="OtherRepository"),
    )

    (candidate,) = found
    assert candidate.repository_type == "ThingRepository"
    assert candidate.declaration_file is not None
    assert candidate.declaration_file.endswith("ThingRepository.java")


def test_a_call_in_a_loop_header_runs_once_and_is_silent(
    candidates: Candidates, service: Build, repository: Build
) -> None:
    """The iterable expression is evaluated once, not once per element."""
    assert (
        candidates(
            service(
                """
        for (Thing thing : thingRepository.findAll()) {
            total += thing.getAmount();
        }
"""
            ),
            repository(DERIVED),
        )
        == []
    )
