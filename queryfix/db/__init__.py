"""Reference-database concerns: provisioning, transactions, and schema sourcing.

Stage 4 of the pipeline lives here rather than under ``pipeline/`` because these
two invariants are database-wide, not stage-specific: the database is always
isolated (:mod:`.provision`) and nothing ever commits (:mod:`.session`).

:mod:`.liquibase` is a third, independent concern that happens to live beside
them: it builds the static schema a repository's static rules read
(:mod:`queryfix.pipeline.static_rules.schema`) from that repository's own
Liquibase changelog, with no database, container, or connection involved.
"""
