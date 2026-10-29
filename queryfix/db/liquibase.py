"""Liquibase changelog -> :class:`~queryfix.pipeline.static_rules.schema.TableSchema`.

Turns a Liquibase XML changelog chain into the schema shape the static rules
already understand, so :class:`~queryfix.pipeline.static_rules.rules.unindexed_filter.UnindexedFilterRule`
can answer "does this table have an index on that column" from a repository's
real schema instead of staying silent forever.

Scope, deliberately narrow. This module interprets exactly the structured
change types a real-world Liquibase changelog chain actually
uses to shape a table's columns and indexes:

* ``createTable`` (columns, and inline ``<constraints primaryKey="true"/>`` /
  ``unique="true"``)
* ``addColumn`` (same column shape as ``createTable``)
* ``createIndex``
* ``addPrimaryKey``
* ``addUniqueConstraint``
* ``dropIndex``
* ``<property>`` (for ``${name}`` substitution in column ``type`` attributes)

Everything else a changeset can contain — raw ``<sql>``/``<sqlFile>`` blocks,
``modifyDataType``, ``renameColumn``, ``dropColumn``, ``addForeignKeyConstraint``,
``<preConditions>``, ``<rollback>``, ``<comment>``, ``<modifySql>`` — is walked
over and silently ignored, on purpose. In particular:

* **Raw SQL is never interpreted as schema.** A ``CREATE INDEX`` or
  ``ALTER TABLE ... ADD COLUMN`` sitting inside a ``<sql>`` block is invisible
  to this loader. The audited chain has real examples of exactly this
  (``changelog-56.xml`` adds three columns to ``payment`` this way, never touching
  ``<addColumn>``) — this loader will under-report those columns/indexes
  rather than guess at SQL text, matching the rest of QueryFix's "say
  nothing rather than guess" discipline for schema-dependent rules. The
  practical consequence is a *false positive* risk (``unindexed-filter``
  firing on a column that a raw-SQL block actually indexed), never a false
  negative that hides a real problem.
* **``<preConditions>`` are ignored.** They guard against re-running a
  changeset against a database that already has its effect applied; a static
  walker building a schema from nothing has no such database, so every
  changeset's effect is applied unconditionally, in order. This matches the
  chain's own usage: 617 of 619 changesets in the audited repository use
  ``onFail="MARK_RAN"``, which only ever means "skip if already applied" —
  never "change what gets applied."
* **``<includeAll>`` is not implemented.** The audited chain never uses it —
  only ``<include file="..." relativeToChangelogFile="true"/>``. Adding it
  would be guessing at a form this loader has never had to parse correctly.
* **Changesets are not filtered by ``dbms`` or scoped by ``context``/``labels``.**
  None of the three appear at the ``<changeSet>`` level in the audited chain
  (``dbms`` only ever appears nested inside ``<modifySql>``, which this loader
  already ignores), so there is nothing here to filter on.

Order matters and is preserved. Liquibase applies changesets in the order
their changelog declares them, recursing into ``<include>`` depth-first at
the point it appears. The audited chain proves this is load-bearing, not
cosmetic: ``fedwire_iso_details``'s ``local_instrument`` index is dropped and
recreated three times across three files, and only processing them in
declared order — not alphabetically, not file-by-file in isolation —
reconstructs the real final state.

PR-aware schema (rebuild-at-head, not delta-overlay)
-----------------------------------------------------

A pull request can change the changelog tree itself — add a ``createIndex``,
drop one, add a wholly new included file. :func:`build_table_schemas_remote`
answers "what does the schema look like once this pull request's Liquibase
changes are applied" by re-walking the **complete** tree from a chosen root,
sourced from ``read`` instead of local disk, exactly as :func:`build_table_schemas`
already does for a local checkout. Given a ``read`` that fetches each file at
the pull request's head commit, this reconstructs the tree's true final state
directly — no bookkeeping of "which changesets are new" is needed, because the
existing ordered walk already resolves create → drop → recreate correctly from
nothing. That also means a modified aggregator file that reorders or edits
``<include>`` lines without introducing new changesets cannot be mistaken for
new schema changes: the walk only ever sees the changesets the head tree
actually declares, once each, never replayed as if newly introduced.

:func:`resolve_pr_changelog_touch` is the cheap gate in front of this: most
pull requests never touch the changelog tree at all, and re-fetching every
file over the network on every run would be wasteful. See its docstring for
why checking the *locally resolvable* tree cannot miss a real touch.
"""

from __future__ import annotations

import logging
import posixpath
import xml.etree.ElementTree as ET
from collections.abc import Callable, Collection, Iterable
from pathlib import Path

from queryfix.pipeline.static_rules.schema import (
    UNKNOWN_SCHEMA,
    SchemaProvider,
    StaticSchemaProvider,
    TableSchema,
)

__all__ = [
    "LiquibaseParseError",
    "ReadChangelogFile",
    "build_table_schemas",
    "build_table_schemas_remote",
    "load_liquibase_schema",
    "load_liquibase_schema_remote",
    "load_schema_from_settings",
    "resolve_changelog_files",
    "resolve_pr_changelog_touch",
    "resolve_remote_changelog_files",
]

#: Reads one changelog file's full text, addressed by a repo-relative POSIX path.
#: Injected rather than hard-coded to a transport, so the same tree-walking logic
#: in this module can be sourced from local disk or from a pull request's head
#: commit over the GitHub API — see the module docstring's "PR-aware schema" section.
ReadChangelogFile = Callable[[str], str]

logger = logging.getLogger(__name__)

#: Liquibase change types this loader reads directly off a <changeSet>.
#: Anything else encountered there is intentionally skipped — see module docstring.
_CREATE_TABLE = "createTable"
_ADD_COLUMN = "addColumn"
_CREATE_INDEX = "createIndex"
_ADD_PRIMARY_KEY = "addPrimaryKey"
_ADD_UNIQUE_CONSTRAINT = "addUniqueConstraint"
_DROP_INDEX = "dropIndex"


class LiquibaseParseError(RuntimeError):
    """A changelog file could not be read, found, or parsed as XML."""


class _TableState:
    """Mutable accumulator for one table while the changelog chain is walked.

    ``indexes`` is keyed by index/constraint name rather than held as a flat
    column set, because a later ``dropIndex`` removes one specific index by
    name — and the audited schema has real tables (``settlement_ledger``) where
    two independently-named indexes both lead with the same column, so
    dropping one must not un-index a column another index still covers.
    """

    __slots__ = ("column_types", "indexes")

    def __init__(self) -> None:
        self.column_types: dict[str, str] = {}
        self.indexes: dict[str, list[str]] = {}

    def register_index(self, name: str, columns: list[str]) -> None:
        if columns:
            self.indexes[name] = columns

    def to_table_schema(self) -> TableSchema:
        leading_columns = frozenset(columns[0] for columns in self.indexes.values() if columns)
        return TableSchema(
            indexed_columns=leading_columns,
            column_types=dict(self.column_types),
        )


def _local_tag(element: ET.Element) -> str:
    """An element's tag with its XML namespace prefix stripped.

    Liquibase's XML namespace (``http://www.liquibase.org/xml/ns/dbchangelog``)
    makes every tag show up as ``{that-uri}createTable`` under ElementTree.
    Matching on the local name only keeps this loader working across the
    handful of ``dbchangelog-4.NN.xsd`` versions in play without hard-coding one.
    """
    tag = element.tag
    return tag.split("}", 1)[1] if "}" in tag else tag


def _substitute(value: str, properties: dict[str, str]) -> str:
    """Replace every ``${name}`` in ``value`` with its declared property value.

    Undeclared placeholders are left as-is rather than raised on: a property
    this loader never learned about (because it lives in a changelog outside
    the resolved chain) is the same "I do not know" this loader already
    reports elsewhere, not a reason to fail the whole load.
    """
    for name, replacement in properties.items():
        value = value.replace(f"${{{name}}}", replacement)
    return value


def resolve_changelog_files(root: str | Path) -> list[Path]:
    """Every changelog file reachable from ``root``, in first-encountered order.

    A thin, test-friendly view over the same recursive ``<include>`` walk
    :func:`build_table_schemas` uses internally — useful for asserting the
    include tree was resolved and ordered correctly without also asserting
    anything about the schema those files produce.
    """
    files: list[Path] = []
    items: list[tuple[Path, ET.Element]] = []
    _collect(Path(root).resolve(), files, items, set())
    return files


def _collect(
    path: Path,
    files: list[Path],
    items: list[tuple[Path, ET.Element]],
    seen: set[Path],
) -> None:
    """Depth-first walk of one changelog file's ``<include>``-bearing children.

    ``items`` collects every ``<property>`` and ``<changeSet>`` root-level
    child, in document order, across the whole recursive walk — both can
    appear as plain siblings at changelog root level (the audited chain's own
    ``<property name="boolean.type" .../>`` in ``main-ddl.xml`` is exactly
    this shape, not nested inside a changeset), and a property must be seen in
    the same sequence a changeset referencing it would be, or substitution
    would silently use a value declared later in the chain than it should.
    ``files`` records every path the first time it is reached, for
    :func:`resolve_changelog_files`.
    """
    if path in seen:
        # Liquibase does not expect a changelog to include itself, directly or
        # through a cycle. Guarding here turns a malformed chain into "stop
        # walking" rather than infinite recursion.
        return
    seen.add(path)
    files.append(path)

    try:
        root_element = ET.parse(path).getroot()
    except ET.ParseError as error:
        raise LiquibaseParseError(f"{path} is not well-formed XML: {error}") from error
    except OSError as error:
        raise LiquibaseParseError(f"could not read changelog {path}: {error}") from error

    for child in root_element:
        tag = _local_tag(child)
        if tag in ("property", "changeSet"):
            items.append((path, child))
        elif tag == "include":
            file_attr = child.get("file")
            if not file_attr:
                continue
            relative = child.get("relativeToChangelogFile", "false").strip().lower() == "true"
            # Liquibase resolves a non-relative <include> against the classpath
            # root. This loader has no classpath, so it approximates that root
            # as the directory of the very first changelog file walked — the
            # one the caller passed in, which is the only "root" it can know.
            base_dir = path.parent if relative else files[0].parent
            _collect((base_dir / file_attr).resolve(), files, items, seen)
        # <includeAll> and anything else at changelog root level: not used by
        # the audited chain, not implemented — see module docstring.


def _parse_column(column: ET.Element, properties: dict[str, str]) -> tuple[str, str, bool, bool]:
    """One ``<column>`` element's name, substituted type, and inline constraints."""
    name = column.get("name", "")
    declared_type = _substitute(column.get("type", ""), properties)
    is_primary_key = False
    is_unique = False
    for child in column:
        if _local_tag(child) == "constraints":
            is_primary_key = child.get("primaryKey", "false").strip().lower() == "true"
            is_unique = child.get("unique", "false").strip().lower() == "true"
    return name, declared_type, is_primary_key, is_unique


def _split_column_names(column_names: str) -> list[str]:
    """Liquibase's comma-separated ``columnNames`` attribute, trimmed and filtered."""
    return [name.strip() for name in column_names.split(",") if name.strip()]


def _apply_columns(
    op: ET.Element,
    table_name: str,
    tables: dict[str, _TableState],
    properties: dict[str, str],
) -> None:
    """Shared handling for ``createTable`` and ``addColumn``: both list ``<column>``.

    An inline ``<constraints primaryKey="true"/>`` or ``unique="true"`` creates
    a real, single-column Postgres index just as much as a standalone
    ``createIndex`` does, so it is registered the same way — under a
    synthetic name scoped to the table and column, since Liquibase does not
    require one here and this loader never needs to address it again by name.
    """
    state = tables.setdefault(table_name, _TableState())
    for column in op:
        if _local_tag(column) != "column":
            continue
        name, declared_type, is_primary_key, is_unique = _parse_column(column, properties)
        if not name:
            continue
        state.column_types[name] = declared_type
        if is_primary_key:
            state.register_index(f"{table_name}.{name}#inline_pk", [name])
        if is_unique:
            state.register_index(f"{table_name}.{name}#inline_unique", [name])


def _apply_create_index(op: ET.Element, tables: dict[str, _TableState]) -> None:
    table_name = op.get("tableName")
    index_name = op.get("indexName")
    if not table_name or not index_name:
        return
    columns = [
        column.get("name", "")
        for column in op
        if _local_tag(column) == "column" and column.get("name")
    ]
    tables.setdefault(table_name, _TableState()).register_index(index_name, columns)


def _apply_named_columns_constraint(
    op: ET.Element, tables: dict[str, _TableState], synthetic_prefix: str
) -> None:
    """Shared handling for ``addPrimaryKey`` and ``addUniqueConstraint``.

    Both declare their columns the same way — a ``tableName`` plus a
    comma-separated ``columnNames`` — and both back a real index led by the
    first named column.
    """
    table_name = op.get("tableName")
    column_names = op.get("columnNames")
    if not table_name or not column_names:
        return
    columns = _split_column_names(column_names)
    constraint_name = op.get("constraintName") or f"{synthetic_prefix}#{table_name}"
    tables.setdefault(table_name, _TableState()).register_index(constraint_name, columns)


def _apply_drop_index(op: ET.Element, tables: dict[str, _TableState]) -> None:
    table_name = op.get("tableName")
    index_name = op.get("indexName")
    if not table_name or not index_name:
        return
    state = tables.get(table_name)
    if state is None:
        return
    state.indexes.pop(index_name, None)


def _apply_items(items: Iterable[tuple[object, ET.Element]]) -> dict[str, TableSchema]:
    """Walk collected ``(source, element)`` pairs, in document order, into schemas.

    Shared by the local-disk and PR-head walks: once ``<include>`` resolution has
    produced the same ordered sequence of ``<property>``/``<changeSet>`` elements,
    everything downstream — change-type interpretation, property substitution,
    ordering — is identical regardless of where the bytes came from. ``source`` is
    carried alongside each element only because :func:`_collect` and
    :func:`_collect_remote` already track it for their own path bookkeeping; this
    function does not read it.
    """
    tables: dict[str, _TableState] = {}
    properties: dict[str, str] = {}

    for _source, element in items:
        if _local_tag(element) == "property":
            name, value = element.get("name"), element.get("value")
            if name and value is not None:
                properties[name] = value
            continue

        # Everything else in `items` is a <changeSet> — walk its own children
        # for the change types this loader understands.
        for op in element:
            tag = _local_tag(op)
            if tag in (_CREATE_TABLE, _ADD_COLUMN):
                table_name = op.get("tableName")
                if table_name:
                    _apply_columns(op, table_name, tables, properties)
            elif tag == _CREATE_INDEX:
                _apply_create_index(op, tables)
            elif tag == _ADD_PRIMARY_KEY:
                _apply_named_columns_constraint(op, tables, "__pk__")
            elif tag == _ADD_UNIQUE_CONSTRAINT:
                _apply_named_columns_constraint(op, tables, "__unique__")
            elif tag == _DROP_INDEX:
                _apply_drop_index(op, tables)
            # Every other change type (raw <sql>, modifyDataType, renameColumn,
            # dropColumn, addForeignKeyConstraint, ...) is intentionally not
            # interpreted here — see the module docstring.

    return {name: state.to_table_schema() for name, state in tables.items()}


def build_table_schemas(root: str | Path) -> dict[str, TableSchema]:
    """Walk the changelog chain rooted at ``root`` into one :class:`TableSchema` per table.

    ``root`` is the top-level changelog Liquibase is configured to run — for
    a typical Spring Boot service that is
    ``src/main/resources/db/db.changelog-master.xml``, which resolves through
    one more ``<include>`` into the real 38-file chain. Any changelog root
    works: this function does not know or care that it came from that
    repository specifically.
    """
    files: list[Path] = []
    items: list[tuple[Path, ET.Element]] = []
    _collect(Path(root).resolve(), files, items, set())
    return _apply_items(items)


def load_liquibase_schema(root: str | Path) -> StaticSchemaProvider:
    """Build a :class:`StaticSchemaProvider` from a Liquibase changelog chain.

    The composition CLAUDE.md's schema-aware plan describes:
    ``Liquibase changelog -> build_table_schemas -> StaticSchemaProvider``,
    ready to hand to :class:`~queryfix.pipeline.static_rules.engine.RuleEngine`.
    """
    return StaticSchemaProvider(build_table_schemas(root))


def _collect_remote(
    path: str,
    files: list[str],
    items: list[tuple[str, ET.Element]],
    seen: set[str],
    read: ReadChangelogFile,
) -> None:
    """The :func:`_collect` walk, sourced from ``read`` over repo-relative POSIX paths.

    ``<include>`` resolution, ordering, and cycle guarding mirror ``_collect``
    exactly; the only difference is that a path is a POSIX string joined with
    :mod:`posixpath` instead of a filesystem :class:`~pathlib.Path` resolved
    against a real directory, because a pull request's head commit has no
    filesystem to resolve against.
    """
    normalized = posixpath.normpath(path)
    if normalized in seen:
        return
    seen.add(normalized)
    files.append(normalized)

    try:
        text = read(normalized)
    except Exception as error:
        # `read` is caller-supplied — a GitHub content fetch pinned to a specific
        # commit in production, a recorded fixture in tests — and this module has
        # no business knowing the shape of its failures. Folding every failure into
        # LiquibaseParseError gives callers one exception to handle regardless of
        # where the changelog came from, matching what `_collect` already does for
        # a local OSError.
        msg = f"could not read changelog {normalized}: {error}"
        raise LiquibaseParseError(msg) from error

    try:
        root_element = ET.fromstring(text)
    except ET.ParseError as error:
        msg = f"{normalized} is not well-formed XML: {error}"
        raise LiquibaseParseError(msg) from error

    for child in root_element:
        tag = _local_tag(child)
        if tag in ("property", "changeSet"):
            items.append((normalized, child))
        elif tag == "include":
            file_attr = child.get("file")
            if not file_attr:
                continue
            relative = child.get("relativeToChangelogFile", "false").strip().lower() == "true"
            base_dir = posixpath.dirname(normalized) if relative else posixpath.dirname(files[0])
            target = posixpath.normpath(posixpath.join(base_dir, file_attr))
            _collect_remote(target, files, items, seen, read)
        # <includeAll> and anything else at changelog root level: not implemented,
        # matching `_collect` — see the module docstring.


def resolve_remote_changelog_files(root: str, read: ReadChangelogFile) -> list[str]:
    """Every changelog path reachable from ``root``, sourced from ``read``, in
    first-encountered order — the PR-head analogue of :func:`resolve_changelog_files`.
    """
    files: list[str] = []
    items: list[tuple[str, ET.Element]] = []
    _collect_remote(posixpath.normpath(root), files, items, set(), read)
    return files


def build_table_schemas_remote(root: str, read: ReadChangelogFile) -> dict[str, TableSchema]:
    """Like :func:`build_table_schemas`, but walks the changelog chain via ``read``.

    Used to rebuild the schema exactly as it stands at a pull request's head
    commit: the same ordered, ``<include>``-resolving walk, sourced from the
    network instead of a checkout. See the module docstring's "PR-aware schema"
    section for why this is a full rebuild rather than a delta applied to a base
    schema.
    """
    files: list[str] = []
    items: list[tuple[str, ET.Element]] = []
    _collect_remote(posixpath.normpath(root), files, items, set(), read)
    return _apply_items(items)


def load_liquibase_schema_remote(root: str, read: ReadChangelogFile) -> StaticSchemaProvider:
    """The PR-head analogue of :func:`load_liquibase_schema`."""
    return StaticSchemaProvider(build_table_schemas_remote(root, read))


def resolve_pr_changelog_touch(root: str | Path, changed_paths: Collection[str]) -> bool:
    """Whether any of ``changed_paths`` falls inside the changelog tree at ``root``.

    A cheap gate in front of :func:`build_table_schemas_remote`: most pull requests
    never touch the changelog tree, and re-fetching every file over the network on
    every run to find that out would be wasteful. ``changed_paths`` are repo-relative,
    as GitHub reports them (e.g. from a pull request's changed-file list); ``root``
    is resolved on local disk, exactly as :func:`resolve_changelog_files` already does.

    This cannot produce a false negative. A change that alters the tree's *shape* —
    a new ``<include>``, a reordered one, a brand-new file — necessarily edits some
    file already in the tree, because nothing outside the tree is reachable from
    ``root`` to begin with; that edited file is what this function catches. A false
    positive only costs one avoidable rebuild, not a wrong answer.

    Returns ``False``, rather than raising, when the local tree cannot be resolved
    at all (no checkout, a bad path) — the caller's fallback in that case is the
    local-disk schema, which is already unusable, so there is nothing here worth
    detecting.
    """
    try:
        local_files = resolve_changelog_files(root)
    except LiquibaseParseError:
        return False

    repo_root = Path.cwd()
    local_relative: set[str] = set()
    for file_path in local_files:
        try:
            local_relative.add(file_path.relative_to(repo_root).as_posix())
        except ValueError:
            # Outside the working directory entirely — cannot correspond to any
            # GitHub-reported path, so it cannot match and is not an error.
            continue

    return not local_relative.isdisjoint(changed_paths)


def load_schema_from_settings(changelog_path: str | None) -> SchemaProvider:
    """The real schema if a changelog path is configured, or the silent stub if not.

    Fail-soft per CLAUDE.md invariant 5: an unset, missing, or unparseable
    changelog degrades to :data:`~queryfix.pipeline.static_rules.schema.UNKNOWN_SCHEMA`
    — the same "say nothing" behaviour every schema-dependent rule already has
    today — rather than failing analysis outright. A run with no configured
    changelog behaves exactly as QueryFix did before this module existed.
    """
    if not changelog_path:
        return UNKNOWN_SCHEMA
    try:
        return load_liquibase_schema(changelog_path)
    except LiquibaseParseError:
        logger.exception(
            "failed to load Liquibase schema from %s; continuing without it", changelog_path
        )
        return UNKNOWN_SCHEMA
