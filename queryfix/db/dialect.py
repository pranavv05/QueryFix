"""Which SQL dialect a repository's queries are written in.

QueryFix parses SQL with ``sqlglot``, and a dialect decides whether a statement
parses at all: MySQL's backticks, ``UPDATE a JOIN b ... SET`` and ``AFTER col``
are syntax errors to the Postgres parser. Reading every file as Postgres made a
pilot on a MySQL codebase fail to extract 55 of its migration scripts, so the
dialect has to follow the repository, not a hard-coded default.

The signal is the one the application itself uses to connect: the JDBC URL in its
Spring configuration. Nothing is guessed from the SQL text (a statement that
happens to parse as two dialects tells you nothing), and a repository whose URL is
a placeholder or an in-memory test database yields no answer rather than a wrong
one — the caller then keeps the default, exactly as before this module existed.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable

from queryfix.db.discovery import DEFAULT_CANDIDATE_PATHS

__all__ = ["config_candidates_for", "detect_sql_dialect", "is_known_dialect"]

#: JDBC subprotocol -> sqlglot dialect. H2 is deliberately absent: it is the
#: in-memory test database, and says nothing about the production dialect.
_JDBC_DIALECTS: dict[str, str] = {
    "mysql": "mysql",
    "mariadb": "mysql",
    "postgresql": "postgres",
    "oracle": "oracle",
    "sqlserver": "tsql",
}

_CONFIG_NAMES = ("application", "bootstrap")
_CONFIG_EXTENSIONS = ("properties", "yml", "yaml")

_JDBC_URL = re.compile(r"jdbc:(?P<protocol>[a-z0-9]+):", re.IGNORECASE)


def config_candidates_for(changed_paths: Iterable[str]) -> tuple[str, ...]:
    """Where a repository might keep its datasource URL, given the files a PR changed.

    A single-module Spring Boot repository keeps ``application.properties`` at the
    root's ``src/main/resources``. A multi-module one keeps it inside the module, and
    Spring Cloud services often put the datasource in ``bootstrap.properties`` instead
    (a real multi-module repository does both), so reading only the root
    found nothing there and every SQL file was parsed as Postgres.

    The conventional root locations come first, unchanged. After them, for each module
    containing a changed file (the directory above its ``src/``), that module's
    ``application`` and ``bootstrap`` files. Derived from the changed paths rather than
    from a repository-wide scan, so a PR costs a handful of reads, and so the module
    consulted is the one the change actually belongs to.
    """
    ordered = list(DEFAULT_CANDIDATE_PATHS)
    ordered += [f"src/main/resources/bootstrap.{ext}" for ext in _CONFIG_EXTENSIONS] + [
        f"bootstrap.{ext}" for ext in _CONFIG_EXTENSIONS
    ]

    modules = sorted(
        {
            path.split("/src/", 1)[0]
            for path in changed_paths
            if "/src/" in path and not path.startswith("src/")
        }
    )
    for module in modules:
        for name in _CONFIG_NAMES:
            ordered += [f"{module}/src/main/resources/{name}.{ext}" for ext in _CONFIG_EXTENSIONS]

    return tuple(dict.fromkeys(ordered))  # de-duplicated, order preserved


def detect_sql_dialect(
    read: Callable[[str], str | None],
    *,
    candidate_paths: tuple[str, ...] = DEFAULT_CANDIDATE_PATHS,
) -> str | None:
    """The dialect named by the first application config that declares a JDBC URL.

    ``read`` returns a path's text, or ``None`` when it does not exist (or cannot
    be read — the caller's exception type is not this module's business). Paths are
    tried in the same order Liquibase discovery uses. Commented-out lines are
    ignored, as is an unrecognised or in-memory protocol; the first config file
    that names a *recognised* database wins.
    """
    for path in candidate_paths:
        text = read(path)
        if text is None:
            continue
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith(("#", "!", "//")):
                continue
            match = _JDBC_URL.search(stripped)
            if match is None:
                continue
            dialect = _JDBC_DIALECTS.get(match.group("protocol").lower())
            if dialect is not None:
                return dialect
    return None


def is_known_dialect(name: str) -> bool:
    """Whether ``sqlglot`` can read ``name`` — used to reject a typo'd override."""
    from sqlglot.dialects.dialect import Dialect

    try:
        return Dialect.get(name) is not None
    except ValueError:
        return False
