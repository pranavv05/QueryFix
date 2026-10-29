"""Stage 3 — Static analysis.

Implemented rules: ``SELECT *``, missing ``WHERE`` on a write, unbounded result
set, unindexed filter, non-sargable predicate.

Still planned: ``OFFSET``-based deep paging, ``IN`` lists that should be joins,
cartesian products, and derived-method patterns that fan out.

Importing this package is what puts rules in the registry: each module under
:mod:`.rules` calls :func:`register` at import time, so it must be imported here
to take effect. The imports below are therefore load-bearing, not conveniences.
"""

from __future__ import annotations

from queryfix.pipeline.static_rules.ast_helpers import (
    clause,
    has_clause,
    resolve_table,
    table_aliases,
)
from queryfix.pipeline.static_rules.base import (
    RULES,
    Rule,
    RuleContext,
    register,
    run_static_rules,
)
from queryfix.pipeline.static_rules.engine import (
    RuleEngine,
    silence_sqlglot_fallback_warnings,
)
from queryfix.pipeline.static_rules.rules.missing_where import MissingWhereRule
from queryfix.pipeline.static_rules.rules.no_limit import NoLimitRule
from queryfix.pipeline.static_rules.rules.non_sargable import NonSargableRule
from queryfix.pipeline.static_rules.rules.select_star import SelectStarRule
from queryfix.pipeline.static_rules.rules.unindexed_filter import UnindexedFilterRule
from queryfix.pipeline.static_rules.schema import (
    UNKNOWN_SCHEMA,
    SchemaProvider,
    StaticSchemaProvider,
    TableSchema,
)

__all__ = [
    "RULES",
    "UNKNOWN_SCHEMA",
    "MissingWhereRule",
    "NoLimitRule",
    "NonSargableRule",
    "Rule",
    "RuleContext",
    "RuleEngine",
    "SchemaProvider",
    "SelectStarRule",
    "StaticSchemaProvider",
    "TableSchema",
    "UnindexedFilterRule",
    "clause",
    "has_clause",
    "register",
    "resolve_table",
    "run_static_rules",
    "silence_sqlglot_fallback_warnings",
    "table_aliases",
]
