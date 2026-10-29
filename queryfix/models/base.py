"""The base every stage contract derives from.

One class for one reason: these models are *values passed between stages*, and a
stage must not be able to edit its input. The pipeline is a chain of eight stages
over a shared set of queries and findings, so without this a rule could rewrite
the query text a later stage reports, an enrichment stage could overwrite the
provenance a finding anchors to, and the resulting report would describe
something no file ever contained. Nothing in the report would say it happened.

Immutability also underwrites two properties the product already promises. The
PR comment is rewritten in place on every run (CLAUDE.md invariant 4), so
identical input must render identical output — which is only checkable if the
input cannot drift between the point it is built and the point it is rendered.
And ``POST /analyze`` is a plain ``def``, so FastAPI serves it from a threadpool
and one shared runner handles concurrent runs; frozen models cannot be a data
race.

The freeze is shallow — Pydantic blocks attribute assignment, not
``findings.append(...)`` on a list field. It closes the mistake that actually
happens (``query.text = normalized``) rather than every mistake conceivable, and
does so without forcing tuples into the JSON contract.

Declared as a base class rather than repeated per model so a contract added later
is immutable by construction instead of by remembering.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

__all__ = ["Contract"]


class Contract(BaseModel):
    """Immutable Pydantic model — the shape stages hand each other."""

    model_config = ConfigDict(frozen=True)
