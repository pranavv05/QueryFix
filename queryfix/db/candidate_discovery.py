"""Repository-wide Liquibase changelog candidate search.

The fallback :mod:`queryfix.db.discovery` deliberately does not attempt:
that module reads a fixed, small set of conventional Spring Boot
configuration locations and nothing else — see its own module docstring's
"What this module does not do". When even that fails (no configuration
declares ``db.liquibase.change-log``, or the path it declares does not
resolve to a real changelog), the alternative is not to give up — the task
this module answers is "does *some* file in this repository look like the
root of a real Liquibase changelog tree, with enough corroborating evidence
to act on it rather than guess".

This is evidence-based, not filename-based. A file that happens to be named
``changelog.xml`` is not preferred over one that is not, unless something
about its *content* — a valid ``<databaseChangeLog>`` root, being the target
of another valid changelog's ``<include>``, declaring real ``<changeSet>``
content — corroborates it. Naming is one signal among several, never
sufficient by itself; see :func:`find_changelog_candidates`.

Deliberately conservative about auto-selection. Two or more candidates with
the same, best evidence are :attr:`~queryfix.db.discovery.DiscoveryStatus.AMBIGUOUS`
— named, with their evidence, rather than guessed at — exactly the same
refusal-to-guess :mod:`queryfix.db.discovery` already applies to two
disagreeing ``application.properties`` files.
"""

from __future__ import annotations

import logging
import posixpath
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

from queryfix.db.discovery import DiscoveryResult, DiscoveryStatus
from queryfix.db.liquibase import ReadChangelogFile

__all__ = [
    "MAX_CANDIDATE_FILES_READ",
    "CandidateConfidence",
    "ChangelogCandidate",
    "find_changelog_candidates",
    "resolve_candidate_discovery",
]

logger = logging.getLogger(__name__)

#: Ceiling on how many `*.xml` files this module will fetch and parse in one
#: run. Bounds the worst case (a very large repository) to a fixed number of
#: requests rather than one per XML file the repository happens to contain.
MAX_CANDIDATE_FILES_READ = 200

#: Liquibase's XML root element, namespace stripped — see `_local_tag`. A file
#: that does not parse to this root is not a changelog candidate at all,
#: regardless of its name or location.
_ROOT_TAG = "databaseChangeLog"

#: Substrings that suggest a path was named with Liquibase in mind. One
#: signal among several (see the module docstring) — never sufficient alone.
_NAMING_HINTS = ("changelog", "changeset", "db-changelog", "dbchangelog")


class CandidateConfidence(str, Enum):
    """How much evidence corroborates one candidate, worst to best.

    ``HIGH`` is deliberately not a member here: per CLAUDE.md's own scoring
    scheme, HIGH confidence means "explicitly configured or discovered from
    Spring configuration, includes resolve" — a property of *how* a path was
    found, which :mod:`queryfix.db.discovery` already owns. A path this
    module finds on its own, by definition, was not configured or declared
    anywhere, so the best it can ever earn is MEDIUM.
    """

    LOW = "low"
    MEDIUM = "medium"


@dataclass(frozen=True)
class ChangelogCandidate:
    """One repository-relative POSIX path that parses as a Liquibase changelog."""

    path: str
    confidence: CandidateConfidence
    reasons: tuple[str, ...]


def _local_tag(element: ET.Element) -> str:
    """An element's tag with its XML namespace prefix stripped — matches
    :mod:`queryfix.db.liquibase`'s own helper of the same name and purpose.
    """
    tag = element.tag
    return tag.split("}", 1)[1] if "}" in tag else tag


def find_changelog_candidates(
    paths: Sequence[str],
    read: ReadChangelogFile,
    *,
    limit: int = MAX_CANDIDATE_FILES_READ,
) -> list[ChangelogCandidate]:
    """Every path in ``paths`` that parses as a Liquibase changelog, scored.

    ``paths`` is the whole repository's file list (typically
    :func:`queryfix.integrations.github.list_files_at_ref`) — filtered here
    to ``*.xml`` and capped at ``limit`` before a single byte is fetched, so a
    very large repository costs a bounded number of requests rather than one
    per XML file it happens to contain.

    A file that fails to fetch, fails to parse as XML, or does not have
    ``<databaseChangeLog>`` as its root element is not a candidate at all —
    dropped silently, the same "not found" QueryFix already treats every
    other unreadable candidate as (see
    :mod:`queryfix.db.discovery`'s own ``_read_candidate``).

    Scoring, evidence-based rather than filename-based, and shaped around
    what the caller actually needs: not "is this real Liquibase content" (a
    real changelog chain's children all pass that) but "is this the *root*
    a caller should hand to :func:`~queryfix.db.liquibase.build_table_schemas_remote`",
    since walking from anything other than the true root under-reports the
    schema — a false negative risk, not a merely-incomplete one. A real
    multi-module validation run is why this distinction exists: every
    one of ~40 individual migration patches in its changelog chain is
    ``<include>``-d by something, so "referenced by another changelog" alone
    made all of them tie at the same confidence, and the run correctly (but
    unhelpfully) reported 41-way ambiguity instead of finding the one real
    root.

    * **Root-shaped** — not referenced by any other candidate (nothing in the
      tree points *at* it) and itself declares ``<include>``/``<includeAll>``
      (it points *at* other files). This is the strongest signal: a
      changelog chain has exactly one node with no incoming reference that
      aggregates the rest, and that is the file whose recursive walk produces
      the complete schema. A referenced file — even one that itself includes
      further children, the way a repository's ``main-ddl.xml`` might — is a
      child of the real root, not the root itself, and is scored accordingly.
    * **Standalone and conventionally named** — not referenced by anything,
      declares its own ``<changeSet>`` content, and its path matches a
      Liquibase naming convention. The fallback signal for a repository with
      no aggregator at all: one self-contained changelog nothing points to
      and nothing points from, corroborated by its name.
    * Everything else that at least parses as a changelog — including any
      file another candidate's ``<include>`` names — is LOW: real content,
      but evidence of being a *child*, not the entry point.
    """
    xml_paths = [path for path in paths if path.lower().endswith(".xml")][:limit]

    parsed: dict[str, ET.Element] = {}
    for path in xml_paths:
        try:
            text = read(path)
        except Exception:
            # Not found, not readable, or read failed for any other reason —
            # this candidate simply does not exist, matching how every other
            # discovery path in this codebase treats a failed read.
            continue
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            continue
        if _local_tag(root) == _ROOT_TAG:
            parsed[path] = root

    referenced = _referenced_paths(parsed)

    candidates = [
        _score(path, root, referenced=path in referenced) for path, root in parsed.items()
    ]
    # Best evidence first, then alphabetical — a stable order so that when a
    # caller reports "N equally plausible candidates", the list it prints is
    # deterministic across runs.
    candidates.sort(
        key=lambda candidate: (candidate.confidence is CandidateConfidence.LOW, candidate.path)
    )
    return candidates


def _referenced_paths(parsed: dict[str, ET.Element]) -> set[str]:
    """Every path any parsed candidate's own ``<include>`` names, resolved.

    Mirrors :mod:`queryfix.db.liquibase`'s own ``<include>`` resolution
    (``relativeToChangelogFile`` against the declaring file's own directory,
    otherwise against the first file in the walk) closely enough to answer
    "is this path named by another candidate", which is all this function
    needs — it does not need to walk the tree Liquibase's own semantics would,
    only to collect every reference any candidate makes.
    """
    referenced: set[str] = set()
    for path, root in parsed.items():
        for child in root:
            if _local_tag(child) != "include":
                continue
            file_attr = child.get("file")
            if not file_attr:
                continue
            relative = child.get("relativeToChangelogFile", "false").strip().lower() == "true"
            base_dir = posixpath.dirname(path) if relative else ""
            referenced.add(posixpath.normpath(posixpath.join(base_dir, file_attr)))
    return referenced


def _score(path: str, root: ET.Element, *, referenced: bool) -> ChangelogCandidate:
    declares_children = any(_local_tag(child) in ("include", "includeAll") for child in root)
    declares_changesets = any(_local_tag(child) == "changeSet" for child in root)
    matches_convention = any(hint in path.lower() for hint in _NAMING_HINTS)
    # In-degree 0 (nothing references it), out-degree > 0 (it references
    # others): the topology of an aggregator root, not a leaf — see
    # find_changelog_candidates' docstring for why this replaced
    # "referenced by another candidate" as the strongest signal.
    root_shaped = declares_children and not referenced

    reasons = ["valid databaseChangeLog root element"]
    if root_shaped:
        reasons.append(
            "not referenced by any other candidate, and itself includes others (root-shaped)"
        )
    elif referenced:
        reasons.append("referenced by another candidate's <include> (a child, not a root)")
    if declares_changesets:
        reasons.append("declares its own changeSet content")
    if matches_convention:
        reasons.append("path matches conventional Liquibase naming")

    if root_shaped:
        confidence = CandidateConfidence.MEDIUM
    elif not referenced and declares_changesets and matches_convention:
        # No aggregator anywhere in the tree — the fallback signal is a
        # self-contained changelog nothing points to and nothing points
        # from, corroborated by its name.
        confidence = CandidateConfidence.MEDIUM
    else:
        confidence = CandidateConfidence.LOW
    return ChangelogCandidate(path=path, confidence=confidence, reasons=tuple(reasons))


def resolve_candidate_discovery(
    paths: Sequence[str],
    read: ReadChangelogFile,
    *,
    limit: int = MAX_CANDIDATE_FILES_READ,
) -> DiscoveryResult:
    """The repository-wide fallback, in :mod:`queryfix.db.discovery`'s own result shape.

    Reused rather than a parallel type so :mod:`queryfix.cli` handles both
    discovery paths — conventional-location and repository-wide fallback —
    through one :class:`~queryfix.db.discovery.DiscoveryResult` contract.
    ``source_file`` carries the winning candidate's evidence, since "which
    application.properties declared it" has no equivalent here — there is no
    declaration, only a candidate this module judged sufficient.
    """
    candidates = find_changelog_candidates(paths, read, limit=limit)
    if not candidates:
        return DiscoveryResult(
            status=DiscoveryStatus.NOT_FOUND,
            reason="no candidate Liquibase changelog found anywhere in the repository",
        )

    best_tier = candidates[0].confidence
    best = [candidate for candidate in candidates if candidate.confidence is best_tier]

    if len(best) > 1:
        listed = "; ".join(f"{candidate.path} ({candidate.confidence.value})" for candidate in best)
        reason = (
            f"{len(best)} equally plausible changelog candidates found ({listed}); "
            "refusing to guess which applies"
        )
        return DiscoveryResult(status=DiscoveryStatus.AMBIGUOUS, reason=reason)

    winner = best[0]
    return DiscoveryResult(
        status=DiscoveryStatus.DISCOVERED,
        changelog_path=winner.path,
        source_file=f"repository fallback, {winner.confidence.value} confidence "
        f"({', '.join(winner.reasons)})",
    )
