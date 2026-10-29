"""Tests for narrow Spring Data JPQL ``@Query`` extraction."""

from __future__ import annotations

import pytest

from queryfix.models import QueryKind
from queryfix.pipeline.extract.java import extract_java


def test_extracts_a_single_query_from_a_repository_interface() -> None:
    content = """@Repository
public interface CustomerRepository {

    @Query("SELECT c FROM Customer c WHERE c.id = :id")
    Customer findCustomer(Long id);
}
"""

    (query,) = extract_java("src/CustomerRepository.java", content)

    assert query.id == "src/CustomerRepository.java:1"
    assert query.kind is QueryKind.JPQL
    assert query.text == "SELECT c FROM Customer c WHERE c.id = :id"
    assert query.provenance.file == "src/CustomerRepository.java"
    assert query.provenance.line == 4


def test_extracts_multiple_query_annotations_in_source_order() -> None:
    content = """public interface CustomerRepository {
    @Query("SELECT c FROM Customer c")
    List<Customer> customers();

    @Query("SELECT c FROM Customer c WHERE c.status = :status")
    List<Customer> byStatus(String status);
}
"""

    queries = extract_java("CustomerRepository.java", content)

    assert [query.id for query in queries] == [
        "CustomerRepository.java:1",
        "CustomerRepository.java:2",
    ]
    assert [query.text for query in queries] == [
        "SELECT c FROM Customer c",
        "SELECT c FROM Customer c WHERE c.status = :status",
    ]


def test_extracts_a_query_from_a_service_class() -> None:
    content = """@Service
public class CustomerService {
    @Query("SELECT c FROM Customer c")
    List<Customer> customers();
}
"""

    (query,) = extract_java("src/CustomerService.java", content)

    assert query.text == "SELECT c FROM Customer c"
    assert query.provenance.line == 3


def test_preserves_multiline_text_block_query_and_its_start_line() -> None:
    content = '''package com.example;

public interface CustomerRepository {
    @Query("""
SELECT c
FROM Customer c
WHERE c.status = :status
""")
    List<Customer> byStatus(String status);
}
'''

    (query,) = extract_java("src/CustomerRepository.java", content)

    assert query.text == "SELECT c\nFROM Customer c\nWHERE c.status = :status\n"
    assert query.provenance.line == 5
    assert query.kind is QueryKind.JPQL


def test_extracts_native_sql_when_value_precedes_native_query() -> None:
    content = """@Query(
    value = "SELECT * FROM customers WHERE id = :id",
    nativeQuery = true
)
Customer findCustomer(Long id);
"""

    (query,) = extract_java("src/CustomerRepository.java", content)

    assert query.kind is QueryKind.SQL
    assert query.text == "SELECT * FROM customers WHERE id = :id"
    assert query.provenance.file == "src/CustomerRepository.java"
    assert query.provenance.line == 2


def test_extracts_native_sql_when_native_query_precedes_value() -> None:
    content = """@Query(
    nativeQuery = true,
    value = "SELECT * FROM customers"
)
List<Customer> customers();
"""

    (query,) = extract_java("CustomerRepository.java", content)

    assert query.kind is QueryKind.SQL
    assert query.text == "SELECT * FROM customers"
    assert query.provenance.line == 3


def test_preserves_multiline_native_sql_text_block_and_its_provenance() -> None:
    content = '''package com.example;

public interface CustomerRepository {
    @Query(
        value = """
SELECT *
FROM customers
WHERE status = :status
""",
        nativeQuery = true
    )
    List<Customer> byStatus(String status);
}
'''

    (query,) = extract_java("src/CustomerRepository.java", content)

    assert query.kind is QueryKind.SQL
    assert query.text == "SELECT *\nFROM customers\nWHERE status = :status\n"
    assert query.provenance.line == 6


@pytest.mark.parametrize(
    "content",
    [
        '@Query(value = "SELECT c FROM Customer c", nativeQuery = false)',
        '@Query(nativeQuery = false, value = "SELECT c FROM Customer c")',
    ],
)
def test_native_query_false_remains_jpql(content: str) -> None:
    (query,) = extract_java("CustomerRepository.java", content)

    assert query.kind is QueryKind.JPQL
    assert query.text == "SELECT c FROM Customer c"


@pytest.mark.parametrize(
    "content",
    [
        "",
        "public class CustomerService {}",
        '@Query("SELECT c FROM Customer c"',
        "@Query(nativeQuery = true)",
        '@Query(value = "SELECT * FROM customers", nativeQuery = true',
        '@Query("SELECT " + "c FROM Customer c")',
    ],
)
def test_unsupported_or_malformed_annotations_produce_no_queries(content: str) -> None:
    assert extract_java("CustomerRepository.java", content) == []


def test_value_only_annotation_is_jpql() -> None:
    (query,) = extract_java("CustomerRepository.java", '@Query(value = "SELECT c FROM Customer c")')

    assert query.kind is QueryKind.JPQL


# --- What the source is, versus what merely looks like it -------------------
#
# Extraction matches patterns against a scanned view of the file rather than its
# raw text. These are the cases that distinction exists for; before it, each of
# them produced a query the application never issues, or lost one it does.


@pytest.mark.parametrize(
    "content",
    [
        'public interface R {\n    // @Query("SELECT c FROM Ghost c")\n    void a();\n}',
        'public interface R {\n    /* @Query("SELECT c FROM Ghost c") */\n    void a();\n}',
        'public interface R {\n    /** @Query("SELECT c FROM Ghost c") */\n    void a();\n}',
    ],
)
def test_a_query_annotation_inside_a_comment_is_not_a_query(content: str) -> None:
    # Reporting a finding against SQL that exists only in a comment is the one
    # kind of wrong a review bot cannot be: the reviewer looks, finds nothing
    # wrong, and stops believing the next report too.
    assert extract_java("R.java", content) == []


def test_a_query_annotation_inside_a_string_literal_is_not_a_query() -> None:
    content = 'class Template {\n    String source = "@Query(\\"SELECT c FROM Ghost c\\")";\n}'

    assert extract_java("Template.java", content) == []


def test_a_commented_out_query_does_not_suppress_the_method_below_it() -> None:
    # The mirror of the case above: the comment must vanish completely, not turn
    # into an annotation that hides the derived method it sits over.
    content = """public interface CustomerRepository {
    // @Query("SELECT c FROM Customer c WHERE c.email = :email")
    Customer findByEmail(String email);
}
"""

    (query,) = extract_java("CustomerRepository.java", content)

    assert query.kind is QueryKind.SPRING_DATA_DERIVED
    assert query.provenance.symbol == "findByEmail"


def test_derived_methods_after_a_default_method_are_still_found() -> None:
    # The interface body ends at its *matching* brace. Taking the first `}` in
    # the file ends it inside `all()`, and everything below silently disappears.
    content = """public interface CustomerRepository {
    default List<Customer> all() { return List.of(); }
    Customer findByEmail(String email);
    long countByStatus(String status);
}
"""

    queries = extract_java("CustomerRepository.java", content)

    assert [query.provenance.symbol for query in queries] == ["findByEmail", "countByStatus"]


def test_a_default_method_calling_a_derived_method_is_not_extracted_twice() -> None:
    # `findByCustomerId(id).stream()...;` satisfies the declaration pattern just
    # as `findByCustomerId(Long id);` does: both close their own parens before the
    # line's final `;`. Without a way to tell them apart, the call site inside the
    # default method's body would be extracted as a second, phantom declaration of
    # the same method — at the wrong line, and duplicated in the report.
    content = """public interface OrderRepository {
    List<Order> findByCustomerId(Long id);

    default List<Order> findRecentByCustomer(Long id) {
        return findByCustomerId(id).stream().limit(10).toList();
    }

    List<Order> findByStatus(String status);
}
"""

    queries = extract_java("OrderRepository.java", content)

    assert [query.provenance.symbol for query in queries] == ["findByCustomerId", "findByStatus"]
    assert [query.provenance.line for query in queries] == [2, 8]


def test_a_brace_inside_a_string_does_not_end_the_interface_body() -> None:
    content = """public interface CustomerRepository {
    String MARKER = "}";
    Customer findByEmail(String email);
}
"""

    (query,) = extract_java("CustomerRepository.java", content)

    assert query.provenance.symbol == "findByEmail"


def test_an_interface_with_unbalanced_braces_yields_no_derived_queries() -> None:
    # No knowable body extent, so no guess at one. Fail soft, not fail loud.
    content = "public interface CustomerRepository {\n    Customer findByEmail(String email);\n"

    assert extract_java("CustomerRepository.java", content) == []


def test_queries_are_emitted_in_source_order() -> None:
    content = """public interface CustomerRepository {
    Customer findByEmail(String email);

    @Query("SELECT c FROM Customer c")
    List<Customer> all();

    long countByStatus(String status);
}
"""

    queries = extract_java("CustomerRepository.java", content)

    assert [query.provenance.line for query in queries] == [2, 4, 7]
    assert [query.kind for query in queries] == [
        QueryKind.SPRING_DATA_DERIVED,
        QueryKind.JPQL,
        QueryKind.SPRING_DATA_DERIVED,
    ]


def test_identity_is_positional_for_annotations_and_symbolic_for_derived_methods() -> None:
    content = """public interface CustomerRepository {
    Customer findByEmail(String email);

    @Query("SELECT c FROM Customer c")
    List<Customer> all();
}
"""

    queries = extract_java("CustomerRepository.java", content)

    # A derived method *is* its name, so its ID does not move when a query is
    # added above it. An annotation has only a position, so its ordinal is its
    # position among all queries extracted from the file.
    assert [query.id for query in queries] == [
        "CustomerRepository.java:findByEmail",
        "CustomerRepository.java:2",
    ]


def test_an_unsupported_query_annotation_still_suppresses_derived_decoding() -> None:
    # The method's SQL comes from the annotation whether or not this extractor
    # can read that annotation yet. Decoding the *name* instead would invent a
    # query the application never issues.
    content = """public interface CustomerRepository {
    @Query("SELECT c FROM Customer c WHERE " + "c.email = :email")
    Customer findByEmail(String email);
}
"""

    assert extract_java("CustomerRepository.java", content) == []


def test_a_comment_between_an_annotation_and_its_method_does_not_break_the_link() -> None:
    content = """public interface CustomerRepository {
    @Query("SELECT c FROM Customer c WHERE c.email = :email")
    // looked up on every login
    Customer findByEmail(String email);
}
"""

    (query,) = extract_java("CustomerRepository.java", content)

    assert query.kind is QueryKind.JPQL


def test_an_annotation_on_an_earlier_method_does_not_suppress_a_later_one() -> None:
    content = """public interface CustomerRepository {
    @Query("SELECT c FROM Customer c")
    List<Customer> all();

    Customer findByEmail(String email);
}
"""

    queries = extract_java("CustomerRepository.java", content)

    assert [query.kind for query in queries] == [QueryKind.JPQL, QueryKind.SPRING_DATA_DERIVED]


def test_derived_methods_are_found_in_a_single_line_interface() -> None:
    content = "public interface CustomerRepository { Customer findByEmail(String e); }"

    (query,) = extract_java("CustomerRepository.java", content)

    assert query.provenance.symbol == "findByEmail"
    assert query.provenance.line == 1


def test_several_repository_interfaces_in_one_file_are_all_scanned() -> None:
    content = """public interface CustomerRepository {
    Customer findByEmail(String email);
}

interface OrderRepository {
    List<Order> findByStatus(String status);
}
"""

    queries = extract_java("Repositories.java", content)

    assert [query.provenance.symbol for query in queries] == ["findByEmail", "findByStatus"]
    # Each resolves its table from its own interface, not from the first one.
    assert "FROM customer\n" in queries[0].text
    assert "FROM order\n" in queries[1].text
