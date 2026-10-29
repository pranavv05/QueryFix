"""p6spy corroboration, and the line between observed and inferred.

A statement log is the only input that can say something *ran*. These tests pin
both directions of that: it must strengthen a matching structural pattern, and it
must never be allowed to manufacture certainty where the log does not actually
support it — including the case p6spy's own grouping was built to expose, where a
shape repeats with identical bind values and is a caching problem rather than an
N+1.
"""

from __future__ import annotations

from collections.abc import Callable

from queryfix.integrations.p6spy import (
    StatementGroup,
    find_repeated_statements,
    parse_statement_log,
)
from queryfix.models.finding import Finding
from queryfix.models.nplusone import EvidenceTier, NPlusOneCandidate
from queryfix.models.query import SourceFile

Detect = Callable[..., list[Finding]]
Candidates = Callable[..., list[NPlusOneCandidate]]
Build = Callable[..., SourceFile]

LOOP = """
        for (Parent parent : parents) {
            thingRepository.findByParentId(parent.getId());
        }
"""
DERIVED = "List<Thing> findByParentId(Long parentId);"


def group(
    *, count: int, variants: int, sql: str = "SELECT id FROM thing WHERE parent_id = ?"
) -> StatementGroup:
    return StatementGroup(
        normalized_sql=sql,
        count=count,
        distinct_variants=variants,
        total_elapsed_ms=count * 2,
        example_sql=sql.replace("?", "1"),
    )


def test_a_matching_statement_log_raises_the_tier_to_the_top(
    candidates: Candidates, service: Build, repository: Build
) -> None:
    (candidate,) = candidates(
        service(LOOP),
        repository(DERIVED),
        repeated_statements=[group(count=5000, variants=5000)],
    )

    assert candidate.tier is EvidenceTier.VERY_HIGH
    assert candidate.runtime is not None
    assert candidate.runtime.count == 5000


def test_runtime_backed_findings_are_not_labelled_as_unverified(
    detect: Detect, service: Build, repository: Build
) -> None:
    """`confidence` means "could not be verified"; an observed run was verified."""
    (finding,) = detect(
        service(LOOP),
        repository(DERIVED),
        repeated_statements=[group(count=5000, variants=5000)],
    )

    assert finding.confidence is None
    assert finding.title == "N+1 query pattern, corroborated by a captured statement log"
    assert "5,000 times" in finding.impact


def test_the_impact_never_inflates_the_observed_count(
    detect: Detect, service: Build, repository: Build
) -> None:
    """A log records one run against that run's data; scaling it up is invention."""
    (finding,) = detect(
        service(LOOP),
        repository(DERIVED),
        repeated_statements=[group(count=6, variants=6)],
    )

    assert "6 times" in finding.impact
    # No unearned magnitude words attached to a six-execution sample.
    lowered = finding.impact.lower()
    assert "thousands" not in lowered
    assert "millions" not in lowered


def test_static_only_findings_never_claim_a_measurement(
    detect: Detect, service: Build, repository: Build
) -> None:
    (finding,) = detect(service(LOOP), repository(DERIVED))

    assert finding.title == "Potential N+1 query pattern"
    assert finding.confidence is not None
    assert "times" not in finding.impact


def test_a_shape_repeating_with_identical_binds_is_not_corroboration(
    candidates: Candidates, service: Build, repository: Build
) -> None:
    """Same statement, same parameters, many times: a caching problem, not an N+1."""
    (candidate,) = candidates(
        service(LOOP),
        repository(DERIVED),
        repeated_statements=[group(count=5000, variants=1)],
    )

    assert candidate.runtime is None
    assert candidate.tier is EvidenceTier.MEDIUM_HIGH


def test_a_barely_repeated_shape_is_not_corroboration(
    candidates: Candidates, service: Build, repository: Build
) -> None:
    (candidate,) = candidates(
        service(LOOP),
        repository(DERIVED),
        repeated_statements=[group(count=2, variants=2)],
    )

    assert candidate.runtime is None


def test_a_statement_against_another_table_is_not_attributed(
    candidates: Candidates, service: Build, repository: Build
) -> None:
    (candidate,) = candidates(
        service(LOOP),
        repository(DERIVED),
        repeated_statements=[
            group(count=5000, variants=5000, sql="SELECT id FROM unrelated WHERE x = ?")
        ],
    )

    assert candidate.runtime is None
    assert candidate.tier is EvidenceTier.MEDIUM_HIGH


def test_an_absent_log_never_weakens_a_structural_finding(
    candidates: Candidates, service: Build, repository: Build
) -> None:
    """A test suite that never exercised the path proves nothing either way."""
    without = candidates(service(LOOP), repository(DERIVED))
    with_empty = candidates(service(LOOP), repository(DERIVED), repeated_statements=[])

    assert without[0].tier is with_empty[0].tier is EvidenceTier.MEDIUM_HIGH


def test_corroboration_works_from_a_real_captured_log(
    candidates: Candidates, service: Build, repository: Build, entity: Build
) -> None:
    """End to end from the p6spy fixture, not from a hand-built group.

    The recorded statement reads `orders` while the entity is called `Order` —
    the reserved-word rename that makes a name-derived table wrong. Attribution
    therefore has to go through `@Table(name = …)`, which is what this pins.
    """
    from pathlib import Path

    log = Path("tests/fixtures/p6spy/nplus1-excerpt.log").read_text(encoding="utf-8")
    groups = find_repeated_statements(parse_statement_log(log))
    assert groups, "the recorded log should contain a repeated shape"

    found = candidates(
        service(
            """
        for (Customer customer : customers) {
            orderRepository.findByCustomerId(customer.getId());
        }
""",
            fields="    private final OrderRepository orderRepository;",
            constructor="",
        ),
        repository(
            "List<Order> findByCustomerId(Long customerId);",
            name="OrderRepository",
            base="JpaRepository<Order, Long>",
        ),
        entity(name="Order", table="orders"),
        repeated_statements=groups,
    )

    (candidate,) = found
    assert candidate.runtime is not None
    assert candidate.runtime.count >= 3
    assert "orders" in candidate.runtime.matched_on
    assert candidate.tier is EvidenceTier.VERY_HIGH


def test_the_evidence_list_separates_observed_from_static(
    detect: Detect, service: Build, repository: Build
) -> None:
    (finding,) = detect(
        service(LOOP),
        repository(DERIVED),
        repeated_statements=[group(count=5000, variants=5000)],
    )

    labels = [item.label for item in finding.evidence]
    assert "Repetition (static)" in labels
    assert "Runtime (observed)" in labels
    # A reader must be able to tell which half is which without reading the prose.
    assert any(label.endswith("(observed)") for label in labels)
    assert any(label.endswith("(static)") for label in labels)
