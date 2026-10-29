"""Outbound integrations: GitHub, Claude, Groq, and p6spy log parsing.

Pipeline stages call into these; nothing here imports a pipeline stage back.
Keeping the dependency one-way is what lets a stage be tested without a network.

``llm.py`` holds the provider-neutral N+1 explanation boundary — the request
shape, the reconciliation, and the :class:`~queryfix.integrations.llm.LLMExplanationProvider`
Protocol — that ``groq.py`` (today's real provider) and ``claude.py`` (a
placeholder, re-exporting the same boundary) are both built on.
"""
