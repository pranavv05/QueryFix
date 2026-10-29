"""Spring Data derived-method decoding and SQL-shaped rendering."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from queryfix.models.query import ExtractedQuery, Provenance, QueryKind
from queryfix.pipeline.extract.base import symbol_query_id

__all__ = [
    "DerivedOperation",
    "DerivedPredicate",
    "DerivedQuery",
    "EqualityPredicate",
    "Ordering",
    "entity_table",
    "parse_derived_method",
    "parse_derived_query",
    "render_derived_query",
]


class DerivedOperation(str, Enum):
    FIND = "find"
    COUNT = "count"
    EXISTS = "exists"
    DELETE = "delete"
    REMOVE = "remove"


class DerivedOperator(str, Enum):
    EQUALS = "equals"
    NOT_EQUALS = "not_equals"
    LESS_THAN = "less_than"
    LESS_THAN_EQUAL = "less_than_equal"
    GREATER_THAN = "greater_than"
    GREATER_THAN_EQUAL = "greater_than_equal"
    BETWEEN = "between"
    LIKE = "like"
    NOT_LIKE = "not_like"
    STARTING_WITH = "starting_with"
    ENDING_WITH = "ending_with"
    CONTAINING = "containing"
    IN = "in"
    NOT_IN = "not_in"
    TRUE = "true"
    FALSE = "false"
    IS_NULL = "is_null"
    IS_NOT_NULL = "is_not_null"


@dataclass(frozen=True)
class DerivedPredicate:
    """One supported predicate, retaining the original Java property spelling."""

    property_name: str
    operator: DerivedOperator = DerivedOperator.EQUALS
    conjunction: str | None = None
    ignore_case: bool = False


# Compatibility name for callers of the initial equality-only milestone.
EqualityPredicate = DerivedPredicate


@dataclass(frozen=True)
class Ordering:
    property_name: str
    direction: str


@dataclass(frozen=True)
class DerivedQuery:
    operation: DerivedOperation
    predicates: tuple[DerivedPredicate, ...]
    ordering: Ordering | None = None
    limit: int | None = None
    distinct: bool = False


_PREFIX = re.compile(
    r"^(?P<operation>find|read|get|query|search|stream|count|exists|delete|remove)"
    r"(?P<limit>(?:First|Top)\d*)?By(?P<criteria>[A-Z][A-Za-z0-9]*)$"
)
_CONNECTOR = re.compile(r"(And|Or)(?=[A-Z])")
_ORDER = re.compile(r"OrderBy(?P<property>[A-Z][A-Za-z0-9]*?)(?P<direction>Asc|Desc)$")
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")

# Longest first: ``NotIn`` must not be read as an ``In`` property, and
# ``LessThanEqual`` must not be shortened to ``LessThan``.
_OPERATORS: tuple[tuple[str, DerivedOperator], ...] = (
    ("IsNotNull", DerivedOperator.IS_NOT_NULL),
    ("IsNull", DerivedOperator.IS_NULL),
    ("LessThanEqual", DerivedOperator.LESS_THAN_EQUAL),
    ("GreaterThanEqual", DerivedOperator.GREATER_THAN_EQUAL),
    ("StartingWith", DerivedOperator.STARTING_WITH),
    ("EndingWith", DerivedOperator.ENDING_WITH),
    ("Containing", DerivedOperator.CONTAINING),
    ("NotLike", DerivedOperator.NOT_LIKE),
    ("NotIn", DerivedOperator.NOT_IN),
    ("GreaterThan", DerivedOperator.GREATER_THAN),
    ("LessThan", DerivedOperator.LESS_THAN),
    # Spring Data's temporal aliases carry the same comparison semantics.
    ("After", DerivedOperator.GREATER_THAN),
    ("Before", DerivedOperator.LESS_THAN),
    ("Between", DerivedOperator.BETWEEN),
    ("Equals", DerivedOperator.EQUALS),
    ("IsNot", DerivedOperator.NOT_EQUALS),
    ("Not", DerivedOperator.NOT_EQUALS),
    ("Like", DerivedOperator.LIKE),
    ("In", DerivedOperator.IN),
    ("True", DerivedOperator.TRUE),
    ("False", DerivedOperator.FALSE),
    ("Is", DerivedOperator.EQUALS),
)


def parse_derived_query(method_name: str) -> DerivedQuery | None:
    """Decode the explicitly supported Spring Data repository grammar.

    Unsupported Spring Data constructs intentionally return ``None`` rather than
    guessing. In particular, nested-property traversal (``Address_City``),
    ``AllIgnoreCase``, projections, and arbitrary keyword extensions are outside
    this static-only decoder.
    """
    match = _PREFIX.fullmatch(method_name)
    if match is None:
        return None

    operation = _operation(match.group("operation"))
    criteria = match.group("criteria")
    ordering = None
    order = _ORDER.search(criteria)
    if order is not None:
        property_name = order.group("property")
        if not property_name:
            return None
        ordering = Ordering(property_name=property_name, direction=order.group("direction").upper())
        criteria = criteria[: order.start()]
    elif "OrderBy" in criteria:
        return None
    if not criteria:
        return None

    predicates = _parse_predicates(criteria)
    if predicates is None:
        return None
    return DerivedQuery(
        operation=operation,
        predicates=tuple(predicates),
        ordering=ordering,
        limit=_limit(match.group("limit")),
    )


def _operation(value: str) -> DerivedOperation:
    return {
        "find": DerivedOperation.FIND,
        "read": DerivedOperation.FIND,
        "get": DerivedOperation.FIND,
        "query": DerivedOperation.FIND,
        "search": DerivedOperation.FIND,
        "stream": DerivedOperation.FIND,
        "count": DerivedOperation.COUNT,
        "exists": DerivedOperation.EXISTS,
        "delete": DerivedOperation.DELETE,
        "remove": DerivedOperation.REMOVE,
    }[value]


def _limit(value: str | None) -> int | None:
    if value is None:
        return None
    digits = value.removeprefix("First").removeprefix("Top")
    return int(digits) if digits else 1


def _parse_predicates(criteria: str) -> list[DerivedPredicate] | None:
    parts = _CONNECTOR.split(criteria)
    predicate = _parse_predicate(parts[0], None)
    if predicate is None:
        return None
    result = [predicate]
    for connector, segment in zip(parts[1::2], parts[2::2], strict=True):
        predicate = _parse_predicate(segment, connector.upper())
        if predicate is None:
            return None
        result.append(predicate)
    return result


def _parse_predicate(segment: str, conjunction: str | None) -> DerivedPredicate | None:
    if segment.endswith("AllIgnoreCase"):
        # `findByStatusAllIgnoreCase` — Spring Data's method-level "ignore case on
        # every string property" keyword, appended after the last property, not a
        # property named "All". Without this, "All" fell through to the property
        # branch below and rendered a column, `status_all`, that does not exist:
        # exactly the guessed-rather-than-unsupported query this decoder's own
        # contract forbids. Rejected outright rather than parsed as "Status" plus
        # the keyword, since a query-wide case-insensitivity flag is not the same
        # semantics as this decoder's per-property `ignore_case`.
        return None

    ignore_case = segment.endswith("IgnoreCase")
    if ignore_case:
        segment = segment.removesuffix("IgnoreCase")
    for suffix, operator in _OPERATORS:
        if segment.endswith(suffix):
            property_name = segment[: -len(suffix)]
            if property_name:
                return DerivedPredicate(property_name, operator, conjunction, ignore_case)
    if not segment:
        return None
    return DerivedPredicate(segment, DerivedOperator.EQUALS, conjunction, ignore_case)


def render_derived_query(derived: DerivedQuery, table: str) -> str:
    """Render normalized semantics for the generic static rule engine."""
    predicates = [_render_predicate(predicate) for predicate in derived.predicates]
    where = f"WHERE {predicates[0]}" + "".join(
        f"\n{predicate.conjunction} {rendered}"
        for predicate, rendered in zip(derived.predicates[1:], predicates[1:], strict=True)
    )
    if derived.operation is DerivedOperation.FIND:
        text = f"SELECT *\nFROM {table}\n{where}"
        if derived.ordering is not None:
            text += f"\nORDER BY {_camel_to_snake(derived.ordering.property_name)} {derived.ordering.direction}"
        if derived.limit is not None:
            text += f"\nLIMIT {derived.limit}"
        return text
    if derived.operation is DerivedOperation.COUNT:
        return f"SELECT COUNT(*)\nFROM {table}\n{where}"
    if derived.operation is DerivedOperation.EXISTS:
        return f"SELECT 1\nFROM {table}\n{where}"
    return f"DELETE\nFROM {table}\n{where}"


def _render_predicate(predicate: DerivedPredicate) -> str:
    property_name = _camel_to_snake(predicate.property_name)
    left = f"LOWER({property_name})" if predicate.ignore_case else property_name
    parameter = "LOWER(?)" if predicate.ignore_case else "?"
    match predicate.operator:
        case DerivedOperator.EQUALS:
            return f"{left} = {parameter}"
        case DerivedOperator.NOT_EQUALS:
            return f"{left} <> {parameter}"
        case DerivedOperator.LESS_THAN:
            return f"{left} < {parameter}"
        case DerivedOperator.LESS_THAN_EQUAL:
            return f"{left} <= {parameter}"
        case DerivedOperator.GREATER_THAN:
            return f"{left} > {parameter}"
        case DerivedOperator.GREATER_THAN_EQUAL:
            return f"{left} >= {parameter}"
        case DerivedOperator.BETWEEN:
            return f"{left} BETWEEN ? AND ?"
        case DerivedOperator.LIKE:
            return f"{left} LIKE {parameter}"
        case DerivedOperator.NOT_LIKE:
            return f"{left} NOT LIKE {parameter}"
        case DerivedOperator.STARTING_WITH:
            return f"{left} LIKE {parameter}"
        case DerivedOperator.ENDING_WITH:
            return f"{left} LIKE {parameter}"
        case DerivedOperator.CONTAINING:
            return f"{left} LIKE {parameter}"
        case DerivedOperator.IN:
            return f"{left} IN (?)"
        case DerivedOperator.NOT_IN:
            return f"{left} NOT IN (?)"
        case DerivedOperator.TRUE:
            return f"{left} = TRUE"
        case DerivedOperator.FALSE:
            return f"{left} = FALSE"
        case DerivedOperator.IS_NULL:
            return f"{left} IS NULL"
        case DerivedOperator.IS_NOT_NULL:
            return f"{left} IS NOT NULL"


def parse_derived_method(
    method_name: str, entity: str, path: str, line: int | None = None
) -> ExtractedQuery | None:
    derived = parse_derived_query(method_name)
    if derived is None:
        return None
    return ExtractedQuery(
        id=symbol_query_id(path, method_name),
        kind=QueryKind.SPRING_DATA_DERIVED,
        text=render_derived_query(derived, entity_table(entity)),
        provenance=Provenance(file=path, line=line, symbol=method_name),
    )


def entity_table(entity: str) -> str:
    entity_name = entity.removesuffix("Repository")
    return _camel_to_snake(entity_name) if entity_name else "unknown_entity"


def _camel_to_snake(value: str) -> str:
    return _CAMEL_BOUNDARY.sub("_", value).lower()
