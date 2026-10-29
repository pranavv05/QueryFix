"""Tests for the first Spring Data derived-method extraction milestone."""

from __future__ import annotations

import dataclasses

import pytest

from queryfix.models import QueryKind
from queryfix.pipeline.extract.derived import (
    DerivedOperation,
    entity_table,
    parse_derived_method,
    parse_derived_query,
    render_derived_query,
)
from queryfix.pipeline.extract.java import extract_java


@pytest.mark.parametrize(
    ("method_name", "expected"),
    [
        ("findById", "SELECT *\nFROM customer\nWHERE id = ?"),
        ("findByEmail", "SELECT *\nFROM customer\nWHERE email = ?"),
        ("findByOrderId", "SELECT *\nFROM customer\nWHERE order_id = ?"),
        (
            "findByCustomerIdAndStatus",
            "SELECT *\nFROM customer\nWHERE customer_id = ?\nAND status = ?",
        ),
        ("countByStatus", "SELECT COUNT(*)\nFROM customer\nWHERE status = ?"),
        ("existsByEmail", "SELECT 1\nFROM customer\nWHERE email = ?"),
        ("deleteByStatus", "DELETE\nFROM customer\nWHERE status = ?"),
    ],
)
def test_renders_supported_derived_methods(method_name: str, expected: str) -> None:
    query = parse_derived_method(method_name, "Customer", "CustomerRepository.java", line=12)

    assert query is not None
    assert query.kind is QueryKind.SPRING_DATA_DERIVED
    assert query.text == expected
    assert query.provenance.file == "CustomerRepository.java"
    assert query.provenance.line == 12
    assert query.provenance.symbol == method_name


def test_extracts_multiple_supported_methods_from_a_repository_interface() -> None:
    content = """public interface CustomerRepository {
    Customer findById(Long id);
    List<Customer> findByCustomerIdAndStatus(Long customerId, String status);
    long countByStatus(String status);
    boolean existsByEmail(String email);
    void deleteByStatus(String status);
}
"""

    queries = extract_java("src/CustomerRepository.java", content)

    assert [query.kind for query in queries] == [QueryKind.SPRING_DATA_DERIVED] * 5
    assert [query.provenance.line for query in queries] == [2, 3, 4, 5, 6]
    assert [query.text for query in queries] == [
        "SELECT *\nFROM customer\nWHERE id = ?",
        "SELECT *\nFROM customer\nWHERE customer_id = ?\nAND status = ?",
        "SELECT COUNT(*)\nFROM customer\nWHERE status = ?",
        "SELECT 1\nFROM customer\nWHERE email = ?",
        "DELETE\nFROM customer\nWHERE status = ?",
    ]


@pytest.mark.parametrize(
    "method_name",
    [
        "findByStatusOrderByCreatedAt",
        "findByCustomer_Address",
        "findBy",
        "findCustomerById",
    ],
)
def test_unsupported_or_malformed_methods_are_ignored(method_name: str) -> None:
    assert parse_derived_method(method_name, "Customer", "CustomerRepository.java") is None


def test_empty_repository_produces_no_derived_queries() -> None:
    assert extract_java("CustomerRepository.java", "public interface CustomerRepository {}") == []


def test_unknown_entity_uses_a_stable_table_placeholder() -> None:
    query = parse_derived_method("findById", "", "Repository.java")

    assert query is not None
    assert query.text == "SELECT *\nFROM unknown_entity\nWHERE id = ?"


def test_annotated_derived_method_is_not_extracted_twice() -> None:
    content = """public interface CustomerRepository {
    @Query("SELECT c FROM Customer c WHERE c.id = :id")
    Customer findById(Long id);
}
"""

    queries = extract_java("CustomerRepository.java", content)

    assert len(queries) == 1
    assert queries[0].kind is QueryKind.JPQL


# --- The intermediate representation ----------------------------------------
#
# Decoding a method name, saying what it means in SQL, and anchoring it to a file
# are three jobs. These test the first two directly, because the seam between
# them is what a second framework's decoder and the planned fan-out rule will
# both attach to.


@pytest.mark.parametrize(
    ("method_name", "operation", "properties"),
    [
        ("findById", DerivedOperation.FIND, ["Id"]),
        ("countByStatus", DerivedOperation.COUNT, ["Status"]),
        ("existsByEmail", DerivedOperation.EXISTS, ["Email"]),
        ("deleteByStatus", DerivedOperation.DELETE, ["Status"]),
        ("findByCustomerIdAndStatus", DerivedOperation.FIND, ["CustomerId", "Status"]),
    ],
)
def test_decoding_a_name_yields_framework_neutral_semantics(
    method_name: str, operation: DerivedOperation, properties: list[str]
) -> None:
    derived = parse_derived_query(method_name)

    assert derived is not None
    assert derived.operation is operation
    assert [predicate.property_name for predicate in derived.predicates] == properties


def test_the_intermediate_representation_is_immutable() -> None:
    derived = parse_derived_query("findById")

    assert derived is not None
    with pytest.raises(dataclasses.FrozenInstanceError):
        derived.operation = DerivedOperation.COUNT  # type: ignore[misc]


def test_the_unsupported_grammar_is_declared_rather_than_absent() -> None:
    # The decoder does not produce these yet, but the IR has somewhere to put
    # them — so widening the decoder does not reshape the contract underneath
    # the renderer.
    derived = parse_derived_query("findById")

    assert derived is not None
    assert (derived.ordering, derived.limit, derived.distinct) == (None, None, False)


@pytest.mark.parametrize(
    "method_name",
    ["findByStatusOrderByCreatedAt", "findBy"],
)
def test_decoding_rejects_grammar_it_does_not_understand(method_name: str) -> None:
    assert parse_derived_query(method_name) is None


def test_all_ignore_case_is_rejected_rather_than_read_as_a_property_named_all() -> None:
    # `findByStatusAllIgnoreCase` is Spring Data's method-level "ignore case on
    # every string property" keyword. Before this, `All` fell through to the
    # per-property branch and rendered a column, `all`, that does not exist —
    # exactly the guessed query the decoder's contract says it must not produce.
    assert parse_derived_query("findByStatusAllIgnoreCase") is None


def test_rendering_is_a_pure_function_of_the_semantics_and_the_table() -> None:
    derived = parse_derived_query("findByCustomerIdAndStatus")

    assert derived is not None
    assert render_derived_query(derived, "orders") == (
        "SELECT *\nFROM orders\nWHERE customer_id = ?\nAND status = ?"
    )


def test_rendering_the_same_semantics_twice_is_byte_identical() -> None:
    # The PR comment is rewritten in place on every run, so churn in rendered
    # text is churn a reviewer sees for no reason.
    derived = parse_derived_query("findByEmail")

    assert derived is not None
    assert render_derived_query(derived, "customer") == render_derived_query(derived, "customer")


@pytest.mark.parametrize(
    ("entity", "expected"),
    [
        ("Customer", "customer"),
        ("CustomerRepository", "customer"),
        ("OrderItemRepository", "order_item"),
        ("", "unknown_entity"),
        ("Repository", "unknown_entity"),
    ],
)
def test_entity_names_resolve_to_stable_table_placeholders(entity: str, expected: str) -> None:
    assert entity_table(entity) == expected


def test_the_composed_entry_point_matches_its_parts() -> None:
    # `parse_derived_method` must stay the composition of the three pieces, not
    # a fourth implementation of the same rendering.
    derived = parse_derived_query("findByEmail")
    query = parse_derived_method("findByEmail", "Customer", "CustomerRepository.java", line=7)

    assert derived is not None
    assert query is not None
    assert query.text == render_derived_query(derived, entity_table("Customer"))
    assert query.id == "CustomerRepository.java:findByEmail"
