"""Connection and transaction helpers — rollback-only.

This module owns the ``BEGIN`` / ``ROLLBACK`` wrapper that every statement
executed against the reference database goes through. Pipeline modules must not
open raw cursors: the invariant is that nothing QueryFix runs can commit, and
that only holds if there is exactly one place where transactions are managed.

This applies to ``EXPLAIN ANALYZE`` (which really executes the query), to HypoPG
index creation, and to any ad-hoc probing.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

__all__ = ["rollback_transaction"]


@contextmanager
def rollback_transaction(connection: Any) -> Iterator[Any]:
    """Yield a cursor inside ``BEGIN`` and always ``ROLLBACK`` on exit.

    Rolls back on the success path too, not just on exception — a committed
    statement against the reference database is a bug regardless of whether the
    surrounding code raised.
    """
    raise NotImplementedError("session.rollback_transaction is not implemented yet")
