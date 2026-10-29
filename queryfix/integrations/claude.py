"""Anthropic client, prompts, and structured outputs.

Conventions from CLAUDE.md:

- Default model :data:`MODEL`. Use the ``anthropic`` SDK — never raw HTTP.
- Structured outputs (``output_config.format``) so findings deserialize into
  :mod:`queryfix.models.finding` without ad-hoc parsing.
- Prompt caching: keep the system prompt and rule descriptions stable and cached;
  put the per-PR diff and query set after the cache breakpoint.
- Handle ``stop_reason == "refusal"`` before reading ``response.content``.

The N+1 boundary
----------------

What reaches an explanation provider is a
:class:`~queryfix.models.nplusone.NPlusOneCandidate` — a call site, a loop, a
resolved repository type, and whatever a statement log corroborated — and what
comes back is prose. The model does not get to choose the file, the line, the
loop, the query, or the count, because
:class:`~queryfix.models.nplusone.NPlusOneExplanation` has no field in which it
could express one. That boundary — the request shape, the reconciliation that
enforces it, and the :class:`~queryfix.integrations.llm.LLMExplanationProvider`
Protocol a provider implements — is provider-neutral and lives in
:mod:`queryfix.integrations.llm` now, not here, so Claude and Groq (and anything
after them) build on exactly the same enforcement rather than each carrying its
own copy. This module re-exports the three symbols below for callers already
importing them from here; new code should prefer importing from
:mod:`queryfix.integrations.llm` directly.

Today's real implementation of that Protocol is
:class:`queryfix.integrations.groq.GroqExplanationProvider`. No Claude client is
constructed here yet: :func:`request_findings` remains unimplemented, so nothing
in this module can reach the network. A future ``ClaudeExplanationProvider``
implementing :class:`~queryfix.integrations.llm.LLMExplanationProvider` belongs
here once ``request_findings`` is real — it needs no change to the detector or to
:mod:`queryfix.integrations.llm`, only a class satisfying the same Protocol
:class:`~queryfix.integrations.groq.GroqExplanationProvider` already does.
"""

from __future__ import annotations

from queryfix.integrations.llm import (
    ContradictedEvidence,
    build_nplusone_request,
    reconcile_nplusone_explanation,
)
from queryfix.models.finding import Finding

__all__ = [
    "MODEL",
    "ContradictedEvidence",
    "build_nplusone_request",
    "reconcile_nplusone_explanation",
    "request_findings",
]

MODEL = "claude-opus-5"


def request_findings(system_prompt: str, user_content: str) -> list[Finding]:
    """Ask Claude for findings, returning them already validated.

    Callers get :class:`Finding` objects or an exception — never half-parsed
    model output. Note that findings are *claims*: the caller is responsible for
    verifying what is checkable and labelling ``confidence`` on what is not.
    """
    raise NotImplementedError("claude.request_findings is not implemented yet")
