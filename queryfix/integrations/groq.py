"""Groq-backed :class:`~queryfix.integrations.llm.LLMExplanationProvider`.

Groq does not decide whether an N+1 exists — the detector in
:mod:`queryfix.pipeline.nplusone` already has, deterministically, before this
module is ever reached. Groq's only job is to turn one already-established
:class:`~queryfix.models.nplusone.NPlusOneCandidate` into developer-facing
prose: what was found, why it matters, what the evidence shows, and what to do
about it. It cannot relocate a finding, invent a measurement, or change a
severity, because :class:`~queryfix.models.nplusone.NPlusOneExplanation` — the
only channel its answer travels through — has no field for any of those, and
:func:`queryfix.integrations.llm.reconcile_nplusone_explanation` rejects prose
that tries to smuggle one in as text instead.

Everything here is best-effort. :meth:`GroqExplanationProvider.explain_nplusone`
never raises: a missing key, a bad key, a rate limit, a timeout, a network
failure, a malformed response, or a response that fails schema validation are all
"no explanation for this candidate", never an exception that could take the
review down (CLAUDE.md invariant 5). A candidate this method has nothing to say
about is simply absent from the returned mapping, and its deterministic finding
is what the report shows.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping, Sequence

import groq
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from queryfix.integrations.llm import NPLUSONE_SYSTEM_PROMPT, build_nplusone_request
from queryfix.models.finding import Finding
from queryfix.models.nplusone import NPlusOneCandidate, NPlusOneExplanation

__all__ = ["GroqExplanationProvider", "create_default_llm_provider"]

logger = logging.getLogger(__name__)

#: Shaped like a real Groq API key, so a diagnostic that echoes an error body
#: (unlikely, but not this module's to trust) cannot leak one. Mirrors
#: ``integrations/github.py::redact`` — same discipline, this module's own
#: credential shape.
_GROQ_KEY_SHAPED = re.compile(r"gsk_[A-Za-z0-9]{20,}")


def _redact(text: str) -> str:
    """Replace anything Groq-key-shaped in ``text`` before it reaches a log."""
    return _GROQ_KEY_SHAPED.sub("<redacted>", text)


#: Every network call this module makes carries this ceiling. Groq is an
#: explanation layer, not the review itself — a hung request must not be able to
#: hold up a PR check indefinitely.
_DEFAULT_TIMEOUT_SECONDS = 20.0

#: The JSON Schema Groq's structured-output mode is constrained to. Matches the
#: four fields CLAUDE.md's PROMPT DESIGN section specifies, and nothing else —
#: there is deliberately no `file`, `line`, `severity`, `confidence`, `query_id`,
#: or `execution_count` field for the model to fill in.
#:
#: Sent below with `"strict": True` — per Groq's structured-outputs
#: documentation, strict mode is supported only by `openai/gpt-oss-20b` and
#: `openai/gpt-oss-120b` (see `config.py::DEFAULT_GROQ_MODEL`'s docstring). A
#: `GROQ_MODEL` override naming any other model will 400 on every call; that
#: failure is now diagnosable from the log line in `_explain_one` rather than
#: a bare status code.
_RESPONSE_JSON_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": "One sentence naming the pattern.",
        },
        "why_it_matters": {
            "type": "string",
            "description": "The performance consequence, grounded in the supplied evidence.",
        },
        "evidence_explanation": {
            "type": "string",
            "description": "What the supplied evidence shows and how it supports the finding.",
        },
        "recommendation": {
            "type": "string",
            "description": "A concrete remediation.",
        },
    },
    "required": ["summary", "why_it_matters", "evidence_explanation", "recommendation"],
    "additionalProperties": False,
}


class _GroqNPlusOneResponse(BaseModel):
    """Validates Groq's raw JSON before it is allowed to become an explanation.

    A provider-specific shape, deliberately distinct from
    :class:`~queryfix.models.nplusone.NPlusOneExplanation`: this is what Groq is
    asked to return; that is what every provider is allowed to hand back to the
    detector. Keeping them separate means a wire-format change here (a renamed
    field, an extra one Groq starts sending) touches this module only — the
    provider-neutral contract downstream never moves.
    """

    # `str_strip_whitespace=True` so a whitespace-only `summary` ("   ") fails
    # `min_length=1` instead of sneaking past it — pydantic's `min_length` counts
    # characters, not meaningful content, and does not strip on its own.
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    summary: str = Field(min_length=1)
    why_it_matters: str = ""
    evidence_explanation: str = ""
    recommendation: str = ""

    def to_explanation(self) -> NPlusOneExplanation:
        """Adapt into the provider-neutral shape every provider must answer in."""
        reasoning = " ".join(
            part.strip()
            for part in (self.why_it_matters, self.evidence_explanation)
            if part.strip()
        )
        recommendation = self.recommendation.strip()
        return NPlusOneExplanation(
            summary=self.summary,
            reasoning=reasoning,
            remediation=(recommendation,) if recommendation else (),
        )


class GroqExplanationProvider:
    """Explains N+1 candidates using a Groq chat-completion model.

    ``client`` is the injectable seam this codebase uses everywhere a network
    client would otherwise make a test reach for the real thing (see
    ``integrations/github.py::new_client``) — tests pass a fake implementing just
    ``chat.completions.create`` and never construct a real :class:`groq.Groq`.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str,
        timeout: float = _DEFAULT_TIMEOUT_SECONDS,
        client: groq.Groq | None = None,
    ) -> None:
        self._model = model
        self._client = client if client is not None else groq.Groq(api_key=api_key, timeout=timeout)

    def explain_nplusone(
        self, pairs: Sequence[tuple[NPlusOneCandidate, Finding]]
    ) -> Mapping[tuple[str, ...], NPlusOneExplanation]:
        """One Groq call per candidate. See the Protocol docstring for the contract.

        A call per candidate rather than one batched request: each candidate's
        evidence is independent, and a single malformed or oversized batch
        response would otherwise cost every candidate its explanation instead of
        just one. Nothing here prevents a future batching implementation behind
        this same method — the call site only ever sees the returned mapping.
        """
        explanations: dict[tuple[str, ...], NPlusOneExplanation] = {}
        for candidate, finding in pairs:
            explanation = self._explain_one(candidate, finding)
            if explanation is not None:
                explanations[candidate.identity] = explanation
        return explanations

    def _explain_one(
        self, candidate: NPlusOneCandidate, finding: Finding
    ) -> NPlusOneExplanation | None:
        payload = build_nplusone_request(candidate, finding=finding)
        where = f"{candidate.file}:{candidate.line}"

        try:
            response = self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": NPLUSONE_SYSTEM_PROMPT},
                    {"role": "user", "content": json.dumps(payload, sort_keys=True)},
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "nplusone_explanation",
                        "schema": _RESPONSE_JSON_SCHEMA,
                        "strict": True,
                    },
                },
                timeout=_DEFAULT_TIMEOUT_SECONDS,
            )
        except groq.AuthenticationError:
            logger.warning("groq: authentication failed; skipping explanation for %s", where)
            return None
        except groq.RateLimitError:
            logger.warning("groq: rate limited; skipping explanation for %s", where)
            return None
        except groq.APITimeoutError:
            logger.warning("groq: request timed out; skipping explanation for %s", where)
            return None
        except groq.APIConnectionError:
            logger.warning("groq: network error; skipping explanation for %s", where)
            return None
        except groq.APIStatusError as error:
            # The status code alone ("API returned 400") is not diagnosable —
            # it was all a real-world run's logs ever carried, because
            # this handler used to discard everything past error.status_code.
            # Groq's own error message names the actual cause (an
            # unsupported model, a schema Groq rejected, strict mode on a
            # model that does not support it) — surface it, redacted the same
            # way a GitHub error message is (never trusting a third-party
            # string not to echo a credential back).
            logger.warning(
                "groq: API returned %s; skipping explanation for %s: %s",
                error.status_code,
                where,
                _redact(error.message),
                extra={
                    "status_code": error.status_code,
                    "model": self._model,
                },
            )
            return None
        except groq.GroqError:
            # Catch-all for the base error class: anything the SDK itself raises
            # that is not one of the more specific cases above still must not
            # take the review down.
            logger.warning("groq: request failed; skipping explanation for %s", where)
            return None

        content = _first_message_content(response)
        if not content:
            logger.warning("groq: empty response; skipping explanation for %s", where)
            return None

        try:
            raw = json.loads(content)
        except json.JSONDecodeError:
            logger.warning("groq: malformed JSON response; discarding explanation for %s", where)
            return None

        try:
            parsed = _GroqNPlusOneResponse.model_validate(raw)
        except ValidationError:
            logger.warning(
                "groq: response failed schema validation; discarding explanation for %s", where
            )
            return None

        return parsed.to_explanation()


def _first_message_content(response: object) -> str | None:
    """The text of the first choice, or None if the response carries none.

    Narrowed to ``object`` rather than the SDK's response type so a fake test
    double only has to look like the real response, not import it.
    """
    choices = getattr(response, "choices", None)
    if not choices:
        return None
    message = getattr(choices[0], "message", None)
    content = getattr(message, "content", None)
    return content if isinstance(content, str) else None


def create_default_llm_provider() -> GroqExplanationProvider | None:
    """The explanation provider QueryFix should use, from process configuration.

    None when ``GROQ_API_KEY`` is not configured — the pipeline continues without
    LLM-authored prose, exactly the deterministic report it produced before this
    integration existed (CLAUDE.md invariant 5). Callers that want LLM
    explanations unconditionally should construct :class:`GroqExplanationProvider`
    themselves; this is the convenience path for "use it if it's configured."
    """
    # Imported here, not at module scope, so importing this module never requires
    # configuration to be present — the same lazy-config convention
    # ``integrations/github.py::new_client`` uses.
    from queryfix.config import get_settings

    settings = get_settings()
    if settings.groq_api_key is None or not settings.groq_api_key.get_secret_value():
        return None
    return GroqExplanationProvider(
        api_key=settings.groq_api_key.get_secret_value(),
        model=settings.groq_model,
    )
