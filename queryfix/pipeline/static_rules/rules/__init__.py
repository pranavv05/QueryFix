"""One rule per module, named after the smell it detects.

Empty until the first rule lands. A module added here must be imported by
:mod:`queryfix.pipeline.static_rules` for its :func:`register` call to run —
otherwise the rule exists but never executes.
"""
