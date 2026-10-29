"""Stage 2 — the extension registry that decides which extractor sees a file.

A registry rather than a conditional in the dispatcher, so that adding Kotlin,
Python, or a Micronaut variant is a registration and a new module instead of an
edit to the code that routes to them. The dispatcher then holds no knowledge of
which languages exist, which is the only way its behaviour can stay fixed while
the set of languages grows.
"""

from __future__ import annotations

from pathlib import Path

from queryfix.pipeline.extract.base import Extractor

__all__ = ["Extractor", "ExtractorRegistry"]


class ExtractorRegistry:
    """Maps normalized filename extensions to the extractor that owns them.

    Registration is a startup-time concern: extractors are registered as an
    import side effect and the registry is read-only from then on, so a run never
    races a registration.

    Duplicate extensions are rejected rather than overwritten. Silently replacing
    an extractor would make the set of registered languages depend on import
    order — a newly imported integration could take ``.sql`` away from the SQL
    extractor, and the only symptom would be findings quietly disappearing.
    """

    def __init__(self) -> None:
        self._extractors: dict[str, Extractor] = {}

    def register(self, extension: str, extractor: Extractor) -> None:
        """Register ``extractor`` as the owner of one filename extension."""
        normalized = _normalize_extension(extension)
        if normalized in self._extractors:
            msg = f"an extractor is already registered for {normalized}"
            raise ValueError(msg)
        self._extractors[normalized] = extractor

    def for_path(self, path: str) -> Extractor | None:
        """The extractor registered for ``path``, or None if the type is unknown.

        Matching is on the final extension only and is case-insensitive, because
        a checkout on a case-insensitive filesystem should not analyze a
        different set of files from one on Linux. ``query.sql.bak`` is therefore
        a ``.bak`` and is not analyzed, which is the intended reading of a backup
        file.
        """
        return self._extractors.get(Path(path).suffix.lower())

    def extensions(self) -> frozenset[str]:
        """Every registered extension, for diagnostics and tests."""
        return frozenset(self._extractors)


def _normalize_extension(extension: str) -> str:
    """Validate and normalize a registered filename extension."""
    normalized = extension.lower()
    if not normalized.startswith(".") or len(normalized) == 1:
        msg = "an extractor extension must begin with '.' and contain a suffix"
        raise ValueError(msg)
    return normalized
