"""Coverage for the supported Java extraction grammar."""

from __future__ import annotations

import pytest

from queryfix.models import QueryKind
from queryfix.pipeline.extract.derived import (
    DerivedOperation,
    parse_derived_query,
    render_derived_query,
)
from queryfix.pipeline.extract.java import extract_java


@pytest.mark.parametrize(
    ("annotation", "kind", "expected"),
    [
        (
            '@Query("SELECT u FROM User u WHERE u.email = :email")',
            QueryKind.JPQL,
            "SELECT u FROM User u WHERE u.email = :email",
        ),
        (
            '@Query(value = "SELECT * FROM users WHERE email = ?1", nativeQuery = true)',
            QueryKind.SQL,
            "SELECT * FROM users WHERE email = ?1",
        ),
        (
            '@Query(nativeQuery = false, value = "SELECT u FROM User u")',
            QueryKind.JPQL,
            "SELECT u FROM User u",
        ),
        (
            '@Query("SELECT u FROM User u WHERE u.name = \\"a,b\\"")',
            QueryKind.JPQL,
            'SELECT u FROM User u WHERE u.name = "a,b"',
        ),
    ],
)
def test_annotation_forms(annotation: str, kind: QueryKind, expected: str) -> None:
    (query,) = extract_java("UserRepository.java", annotation)

    assert (query.kind, query.text) == (kind, expected)


def test_text_block_and_nested_annotation_arguments_are_lexically_safe() -> None:
    content = '''@Query(hint = @Hint(name = "x,y"), nativeQuery = true, value = """
SELECT *
FROM users
WHERE email = ?
""")
User findOne();
'''

    (query,) = extract_java("UserRepository.java", content)

    assert query.kind is QueryKind.SQL
    assert query.text == "SELECT *\nFROM users\nWHERE email = ?\n"
    assert query.provenance.line == 2


def test_annotations_and_derived_queries_are_all_extracted_in_order() -> None:
    content = """public interface UserRepository {
    @Query("SELECT u FROM User u")
    User one();
    User findByEmailOrUsername(String email, String username);
    @Query(value = "SELECT * FROM users", nativeQuery = true)
    User nativeOne();
    User findTop10ByStatusOrderByCreatedAtDesc(String status);
}"""

    queries = extract_java("UserRepository.java", content)

    assert [query.kind for query in queries] == [
        QueryKind.JPQL,
        QueryKind.SPRING_DATA_DERIVED,
        QueryKind.SQL,
        QueryKind.SPRING_DATA_DERIVED,
    ]
    assert queries[1].provenance.symbol == "findByEmailOrUsername"
    assert queries[3].text.endswith("ORDER BY created_at DESC\nLIMIT 10")


@pytest.mark.parametrize(
    ("method", "operation", "fragment"),
    [
        ("findByEmail", DerivedOperation.FIND, "email = ?"),
        ("readByEmail", DerivedOperation.FIND, "email = ?"),
        ("getByEmail", DerivedOperation.FIND, "email = ?"),
        ("queryByEmail", DerivedOperation.FIND, "email = ?"),
        ("searchByEmail", DerivedOperation.FIND, "email = ?"),
        ("streamByEmail", DerivedOperation.FIND, "email = ?"),
        ("countByStatus", DerivedOperation.COUNT, "COUNT(*)"),
        ("existsByEmail", DerivedOperation.EXISTS, "SELECT 1"),
        ("deleteByStatus", DerivedOperation.DELETE, "DELETE"),
        ("removeByStatus", DerivedOperation.REMOVE, "DELETE"),
        ("findByEmailAndStatus", DerivedOperation.FIND, "AND status = ?"),
        ("findByEmailOrUsername", DerivedOperation.FIND, "OR username = ?"),
        ("findByAgeGreaterThan", DerivedOperation.FIND, "age > ?"),
        ("findByAgeGreaterThanEqual", DerivedOperation.FIND, "age >= ?"),
        ("findByAgeLessThan", DerivedOperation.FIND, "age < ?"),
        ("findByAgeLessThanEqual", DerivedOperation.FIND, "age <= ?"),
        ("findByPlacedAtAfter", DerivedOperation.FIND, "placed_at > ?"),
        ("findByPlacedAtBefore", DerivedOperation.FIND, "placed_at < ?"),
        ("findByCreatedAtBetween", DerivedOperation.FIND, "created_at BETWEEN ? AND ?"),
        ("findByNameLike", DerivedOperation.FIND, "name LIKE ?"),
        ("findByNameContaining", DerivedOperation.FIND, "name LIKE ?"),
        ("findByNameStartingWith", DerivedOperation.FIND, "name LIKE ?"),
        ("findByNameEndingWith", DerivedOperation.FIND, "name LIKE ?"),
        ("findByStatusIn", DerivedOperation.FIND, "status IN (?)"),
        ("findByStatusNotIn", DerivedOperation.FIND, "status NOT IN (?)"),
        ("findByActiveTrue", DerivedOperation.FIND, "active = TRUE"),
        ("findByDeletedIsNull", DerivedOperation.FIND, "deleted IS NULL"),
        ("findByDeletedIsNotNull", DerivedOperation.FIND, "deleted IS NOT NULL"),
        ("findByNameIgnoreCase", DerivedOperation.FIND, "LOWER(name) = LOWER(?)"),
    ],
)
def test_supported_derived_grammar(method: str, operation: DerivedOperation, fragment: str) -> None:
    derived = parse_derived_query(method)

    assert derived is not None
    assert derived.operation is operation
    assert fragment in render_derived_query(derived, "users")


@pytest.mark.parametrize(
    "method",
    ["findFirstByEmail", "findFirst2ByEmail", "findTopByStatus", "findTop10ByStatus"],
)
def test_derived_limits(method: str) -> None:
    derived = parse_derived_query(method)

    assert derived is not None
    assert derived.limit in {1, 2, 10}


def test_after_keeps_a_temporal_comparison_through_the_derived_ir() -> None:
    derived = parse_derived_query("findByStatusAndPlacedAtAfter")

    assert derived is not None
    assert [predicate.property_name for predicate in derived.predicates] == ["Status", "PlacedAt"]
    assert render_derived_query(derived, "orders") == (
        "SELECT *\nFROM orders\nWHERE status = ?\nAND placed_at > ?"
    )


@pytest.mark.parametrize(
    "content",
    [
        '// @Query("SELECT fake")\ninterface RRepository { void ordinary(); }',
        'interface RRepository { String s = "@Query(\\"fake\\")"; void ordinary(); }',
        "interface RRepository { User someMethodNamedfindByEmail(); void x() { unrelated.findBySomething(); } }",
    ],
)
def test_java_lookalikes_are_not_queries(content: str) -> None:
    assert extract_java("RRepository.java", content) == []
