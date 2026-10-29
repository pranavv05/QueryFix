"""QueryFix — reviews database queries for performance problems before they merge.

Package layout mirrors the pipeline described in CLAUDE.md:

- :mod:`queryfix.api` — FastAPI surface
- :mod:`queryfix.pipeline` — one module per stage, in run order
- :mod:`queryfix.db` — isolated reference database, rollback-only sessions
- :mod:`queryfix.integrations` — GitHub, Claude, p6spy
- :mod:`queryfix.models` — the typed contracts between stages
"""

__version__ = "0.1.0"
