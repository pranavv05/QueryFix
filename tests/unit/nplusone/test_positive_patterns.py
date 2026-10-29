"""The shapes the detector must find.

One test per construct, each asserting not just *that* something was found but
what the finding says about it — the loop kind, the nesting depth, whether the
element flows into the arguments. A test that only counts findings passes just as
happily when the detector is right for the wrong reason.
"""

from __future__ import annotations

from collections.abc import Callable

from queryfix.models.finding import Finding, Severity
from queryfix.models.java_structure import ArgumentDependency, IterationKind
from queryfix.models.nplusone import EvidenceTier, NPlusOneCandidate, NPlusOneKind
from queryfix.models.query import SourceFile
from queryfix.pipeline.nplusone import REPOSITORY_CALL_RULE_ID

Detect = Callable[..., list[Finding]]
Candidates = Callable[..., list[NPlusOneCandidate]]
Build = Callable[..., SourceFile]


def test_enhanced_for_with_a_derived_repository_method(
    candidates: Candidates, service: Build, repository: Build
) -> None:
    sources = (
        service(
            """
        for (Parent parent : parents) {
            thingRepository.findByParentId(parent.getId());
        }
"""
        ),
        repository("List<Thing> findByParentId(Long parentId);"),
    )

    (candidate,) = candidates(*sources)

    assert candidate.kind is NPlusOneKind.REPOSITORY_CALL_IN_LOOP
    assert candidate.iteration_kind is IterationKind.ENHANCED_FOR
    assert candidate.repository_type == "ThingRepository"
    assert candidate.method_name == "findByParentId"
    assert candidate.dependency is ArgumentDependency.LOOP_ELEMENT_ARGUMENT
    assert candidate.loop_depth == 1
    # Resolved against the interface, not assumed from the name.
    assert candidate.repository_resolved is True
    assert candidate.tier is EvidenceTier.MEDIUM_HIGH


def test_classic_for_loop(candidates: Candidates, service: Build, repository: Build) -> None:
    (candidate,) = candidates(
        service(
            """
        for (int i = 0; i < parents.size(); i++) {
            thingRepository.findByParentId(parents.get(i).getId());
        }
"""
        ),
        repository("List<Thing> findByParentId(Long parentId);"),
    )

    assert candidate.iteration_kind is IterationKind.FOR
    # A classic `for` binds no element, so there is nothing for an argument to
    # depend on — the pattern is real but one notch weaker than a for-each.
    assert candidate.dependency is ArgumentDependency.INDEPENDENT
    assert candidate.tier is EvidenceTier.MEDIUM


def test_while_loop(candidates: Candidates, service: Build, repository: Build) -> None:
    (candidate,) = candidates(
        service(
            """
        while (cursor.hasNext()) {
            thingRepository.findByParentId(cursor.next());
        }
"""
        ),
        repository("List<Thing> findByParentId(Long parentId);"),
    )

    assert candidate.iteration_kind is IterationKind.WHILE


def test_do_while_loop(candidates: Candidates, service: Build, repository: Build) -> None:
    (candidate,) = candidates(
        service(
            """
        do {
            thingRepository.findByParentId(cursor.next());
        } while (cursor.hasNext());
"""
        ),
        repository("List<Thing> findByParentId(Long parentId);"),
    )

    assert candidate.iteration_kind is IterationKind.DO_WHILE


def test_for_each_lambda(candidates: Candidates, service: Build, repository: Build) -> None:
    (candidate,) = candidates(
        service(
            """
        parents.forEach(parent -> thingRepository.findByParentId(parent.getId()));
"""
        ),
        repository("List<Thing> findByParentId(Long parentId);"),
    )

    assert candidate.iteration_kind is IterationKind.LAMBDA_FOR_EACH
    assert candidate.element_identifier == "parent"
    assert candidate.dependency is ArgumentDependency.LOOP_ELEMENT_ARGUMENT


def test_for_each_lambda_with_a_block_body(
    candidates: Candidates, service: Build, repository: Build
) -> None:
    (candidate,) = candidates(
        service(
            """
        parents.forEach(parent -> {
            List<Thing> things = thingRepository.findByParentId(parent.getId());
            total += things.size();
        });
"""
        ),
        repository("List<Thing> findByParentId(Long parentId);"),
    )

    assert candidate.iteration_kind is IterationKind.LAMBDA_FOR_EACH


def test_stream_map_runs_per_element(
    candidates: Candidates, service: Build, repository: Build
) -> None:
    (candidate,) = candidates(
        service(
            """
        List<List<Thing>> all = parents.stream()
                .map(parent -> thingRepository.findByParentId(parent.getId()))
                .toList();
"""
        ),
        repository("List<Thing> findByParentId(Long parentId);"),
    )

    assert candidate.iteration_kind is IterationKind.LAMBDA_STREAM
    assert candidate.dependency is ArgumentDependency.LOOP_ELEMENT_ARGUMENT


def test_nested_loops_record_their_depth(
    candidates: Candidates, service: Build, repository: Build
) -> None:
    (candidate,) = candidates(
        service(
            """
        for (Parent parent : parents) {
            for (Child child : parent.getChildren()) {
                thingRepository.findByChildId(child.getId());
            }
        }
"""
        ),
        repository("List<Thing> findByChildId(Long childId);"),
    )

    # One call site, one finding — not one per enclosing level.
    assert candidate.loop_depth == 2
    assert candidate.iteration_kind is IterationKind.ENHANCED_FOR


def test_nested_lambdas_produce_one_finding(
    candidates: Candidates, service: Build, repository: Build
) -> None:
    found = candidates(
        service(
            """
        parents.forEach(parent -> {
            parent.getChildren().forEach(child -> {
                thingRepository.findByChildId(child.getId());
            });
        });
"""
        ),
        repository("List<Thing> findByChildId(Long childId);"),
    )

    assert len(found) == 1
    assert found[0].loop_depth == 2


def test_field_injected_repository_resolves(
    candidates: Candidates, service: Build, repository: Build
) -> None:
    (candidate,) = candidates(
        service(
            """
        for (Parent parent : parents) {
            thingRepository.findByParentId(parent.getId());
        }
""",
            fields="    @Autowired\n    private ThingRepository thingRepository;",
            constructor="",
        ),
        repository("List<Thing> findByParentId(Long parentId);"),
    )

    assert candidate.repository_type == "ThingRepository"


def test_repository_used_through_this_resolves(
    candidates: Candidates, service: Build, repository: Build
) -> None:
    (candidate,) = candidates(
        service(
            """
        for (Parent parent : parents) {
            this.thingRepository.findByParentId(parent.getId());
        }
"""
        ),
        repository("List<Thing> findByParentId(Long parentId);"),
    )

    assert candidate.repository_type == "ThingRepository"


def test_repository_reached_through_a_local_alias(
    candidates: Candidates, service: Build, repository: Build
) -> None:
    (candidate,) = candidates(
        service(
            """
        ThingRepository local = thingRepository;
        for (Parent parent : parents) {
            local.findByParentId(parent.getId());
        }
"""
        ),
        repository("List<Thing> findByParentId(Long parentId);"),
    )

    assert candidate.repository_type == "ThingRepository"
    assert candidate.method_name == "findByParentId"


def test_jpql_query_method_links_to_its_query(
    candidates: Candidates, service: Build, repository: Build
) -> None:
    sources = (
        service(
            """
        for (Parent parent : parents) {
            thingRepository.findThingsFor(parent.getId());
        }
"""
        ),
        repository(
            '@Query("SELECT t FROM Thing t WHERE t.parent.id = :id")\n'
            "    List<Thing> findThingsFor(Long id);"
        ),
    )

    (candidate,) = candidates(*sources)

    assert candidate.query_id is not None
    assert candidate.declaration_file == sources[1].path
    assert candidate.query_ids == (candidate.query_id,)


def test_native_query_method_links_to_its_query(
    candidates: Candidates, service: Build, repository: Build
) -> None:
    sources = (
        service(
            """
        for (Parent parent : parents) {
            thingRepository.findRawFor(parent.getId());
        }
"""
        ),
        repository(
            '@Query(value = "SELECT * FROM things WHERE parent_id = :id", nativeQuery = true)\n'
            "    List<Object[]> findRawFor(Long id);"
        ),
    )

    (candidate,) = candidates(*sources)

    assert candidate.query_id is not None
    assert candidate.declaration_line is not None


def test_the_repository_declaration_need_not_be_in_the_change(
    candidates: Candidates, service: Build, repository: Build
) -> None:
    """A pull request that only edits a call site is the common real case."""
    declaration = repository("List<Thing> findByParentId(Long parentId);")
    fetched: list[str] = []

    def resolve(path: str) -> str | None:
        fetched.append(path)
        return declaration.content if path == declaration.path else None

    (candidate,) = candidates(
        service(
            """
        for (Parent parent : parents) {
            thingRepository.findByParentId(parent.getId());
        }
"""
        ),
        resolve_source=resolve,
    )

    # Resolved by deriving the path from the file's own package and imports.
    assert declaration.path in fetched
    assert candidate.repository_resolved is True
    assert candidate.tier is EvidenceTier.MEDIUM_HIGH


def test_an_unresolvable_repository_still_reports_but_one_tier_weaker(
    candidates: Candidates, service: Build
) -> None:
    (candidate,) = candidates(
        service(
            """
        for (Parent parent : parents) {
            thingRepository.findByParentId(parent.getId());
        }
"""
        )
    )

    assert candidate.repository_resolved is False
    assert candidate.tier is EvidenceTier.LOW
    assert candidate.query_id is None


def test_multiple_independent_patterns_are_all_reported(
    candidates: Candidates, service: Build, repository: Build
) -> None:
    sources = (
        service(
            """
        for (Parent parent : parents) {
            thingRepository.findByParentId(parent.getId());
        }
        for (Other other : others) {
            otherRepository.findByOtherId(other.getId());
        }
""",
            fields=(
                "    private final ThingRepository thingRepository;\n"
                "    private final OtherRepository otherRepository;"
            ),
            constructor="",
            imports=("com.example.data.ThingRepository", "com.example.data.OtherRepository"),
        ),
        repository("List<Thing> findByParentId(Long parentId);"),
        repository("List<Other> findByOtherId(Long otherId);", name="OtherRepository"),
    )

    found = candidates(*sources)

    assert {candidate.repository_type for candidate in found} == {
        "ThingRepository",
        "OtherRepository",
    }


def test_a_pattern_spanning_two_changed_files(
    detect: Detect, service: Build, repository: Build
) -> None:
    findings = detect(
        service(
            """
        for (Parent parent : parents) {
            thingRepository.findByParentId(parent.getId());
        }
"""
        ),
        repository("List<Thing> findByParentId(Long parentId);"),
    )

    (finding,) = [f for f in findings if f.rule_id == REPOSITORY_CALL_RULE_ID]
    assert finding.severity is Severity.HIGH
    assert finding.provenance.symbol == "run"
    # The cross-file link the report renders beside the primary anchor.
    assert finding.query_ids


def test_a_lazy_association_walked_in_a_loop(
    candidates: Candidates, service: Build, entity: Build
) -> None:
    (candidate,) = candidates(
        service(
            """
        for (Parent parent : parents) {
            parent.getChildren().size();
        }
""",
            fields="",
            constructor="",
            imports=("com.example.domain.Parent",),
        ),
        entity(
            '@OneToMany(mappedBy = "parent", fetch = FetchType.LAZY)\n'
            "    private List<Child> children;"
        ),
    )

    assert candidate.kind is NPlusOneKind.LAZY_ASSOCIATION_IN_LOOP
    assert candidate.method_name == "getChildren"
    # Capped below the repository tiers: a getter may already be initialized.
    assert candidate.tier is EvidenceTier.MEDIUM


def test_a_lazy_many_to_one_marked_lazy_is_detected(
    candidates: Candidates, service: Build, entity: Build
) -> None:
    (candidate,) = candidates(
        service(
            """
        for (Parent parent : parents) {
            parent.getAccount().getName();
        }
""",
            fields="",
            constructor="",
            imports=("com.example.domain.Parent",),
        ),
        entity("@ManyToOne(fetch = FetchType.LAZY)\n    private Account account;"),
    )

    assert candidate.method_name == "getAccount"


def test_the_finding_names_the_loop_the_call_and_the_query(
    detect: Detect, service: Build, repository: Build
) -> None:
    """Phase 14's acceptance shape: a reviewer can act without opening the file."""
    findings = detect(
        service(
            """
        for (Parent parent : parents) {
            thingRepository.findByParentId(parent.getId());
        }
"""
        ),
        repository("List<Thing> findByParentId(Long parentId);"),
    )

    (finding,) = [f for f in findings if f.rule_id == REPOSITORY_CALL_RULE_ID]
    labels = {item.label for item in finding.evidence}
    blob = " ".join(item.detail for item in finding.evidence)

    assert finding.title == "Potential N+1 query pattern"
    assert "Repetition (static)" in labels
    assert "Element dependency (static)" in labels
    assert "Query declaration (static)" in labels
    assert "Confidence tier" in labels
    assert "ThingRepository.findByParentId" in blob
    assert "parent" in blob
    assert finding.suggestions
    # Static-only findings must carry a confidence, so the report labels them.
    assert finding.confidence is not None
