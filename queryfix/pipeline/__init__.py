"""The analysis pipeline, one module per stage, in run order.

1. :mod:`.ingest` — PR event to run context and diff
2. :mod:`.extract` — diff to query candidates with provenance
3. :mod:`.static_rules` — deterministic AST checks
4. :mod:`queryfix.db.provision` — isolated reference database
5. :mod:`.explain` — ``EXPLAIN ANALYZE`` and plan inspection
6. :mod:`.hypopg` — candidate indexes and simulated cost deltas
7. :mod:`.nplusone` — cross-query analysis via Claude
8. :mod:`.report` — findings to ranked Markdown

Each stage takes the previous stage's typed output and fails soft: a crashed
stage degrades the report, it does not fail the PR check. No stage reaches into
GitHub or Docker directly except the ones that own those concerns.
"""
