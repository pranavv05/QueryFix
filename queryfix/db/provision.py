"""Docker/Postgres lifecycle for the isolated reference database.

The hard invariant: QueryFix never connects to a developer or production
database. Every run clones an isolated reference database from a schema
snapshot/dump. If a code path here could read a real connection string out of CI,
that is a bug — not a configuration choice.

One database per PR run, torn down afterwards, never reused across runs.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

__all__ = ["provision_reference_db"]


@contextmanager
def provision_reference_db(snapshot: str) -> Iterator[Any]:
    """Start an isolated Postgres 16 + HypoPG container from a schema snapshot.

    Creates the ``hypopg`` extension, yields a connection, and tears the
    container down on exit.
    """
    raise NotImplementedError("provision.provision_reference_db is not implemented yet")
