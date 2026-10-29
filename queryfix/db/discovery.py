"""Automatic discovery of a Spring Boot repository's Liquibase changelog root.

Every Java/Spring Boot repository is expected to declare its schema's
entry point the same way, in ``application.properties`` (or the YAML
equivalent):

    db.liquibase.change-log=classpath:/db/changelog/db.changelog-master.xml

This module answers exactly one question — *where is the root changelog?* —
and answers it as a value, not an exception: :func:`discover_liquibase_changelog`
returns a :class:`DiscoveryResult` whose :attr:`~DiscoveryResult.status`
distinguishes "found", "no configuration says anything", "more than one
configuration disagrees", and "the key is there but empty". None of those are
failures QueryFix cannot handle — :mod:`queryfix.cli` already knows what to
do with each, and CLAUDE.md invariant 5 (fail soft) applies here exactly as it
does to every other schema-dependent path: a repository this module cannot make
sense of gets the same silent, safe fallback QueryFix has always had.

What this module does not do
-----------------------------

* **It does not read or interpret Liquibase XML.** Once a root changelog path
  is known, :mod:`queryfix.db.liquibase` — the existing loader — walks it.
  Duplicating that walk here would be a second schema implementation, which
  this feature is explicitly not meant to become.
* **It does not touch local disk.** A review has no guaranteed local checkout
  of the repository under analysis (CLI invocations name an arbitrary
  ``OWNER/REPO`` and ``PR`` — there is nothing to assume is checked out where
  QueryFix happens to be running). Every read goes through an injected
  ``read`` callable — the same
  :data:`~queryfix.db.liquibase.ReadChangelogFile` shape
  :func:`~queryfix.db.liquibase.build_table_schemas_remote` already takes —
  so :mod:`queryfix.cli` can back it with a GitHub API read pinned to the
  pull request's head commit, and a test can back it with a plain dict.
* **It does not scan the repository.** Only the fixed, small set of
  conventional Spring Boot configuration locations in
  :data:`DEFAULT_CANDIDATE_PATHS` is ever read — a handful of requests, not a
  repository tree listing and not "every file whose name looks promising".
  Profile-specific files (``application-dev.properties`` and the like) are
  never among them: a profile is a runtime choice this module has no basis for
  guessing, and picking one silently would be worse than finding nothing.

Property vs. YAML, and the key itself
--------------------------------------

``db.liquibase.change-log`` is read from a ``.properties`` file with a small,
intentionally narrow line parser (:func:`_parse_properties`) — not a full Java
properties implementation, because nothing beyond comments, blank lines, and
``key=value``/``key:value`` pairs has ever been observed in an audited
configuration. The YAML form is read the same way: a narrow indentation walk
(:func:`_parse_yaml_change_log`) that understands exactly the nested-mapping
shape Spring configuration uses for this key, plus the equivalent compact
dotted-key form — not a general YAML parser. PyYAML happens to be importable in
some environments this project runs in (a transitive dependency of an *extra*
on ``fastapi``/``pydantic-settings``/``uvicorn`` that this project does not
enable), but it is not a dependency this project has declared for itself, so
this module does not rely on it being present.

The key read is exactly ``db.liquibase.change-log`` — not
``spring.liquibase.change-log``, which is Spring Boot's own Liquibase
autoconfiguration key and a different (if related) setting. Every real-world
repository audited for this feature sets the narrower key, read by application
code rather than by Spring's autoconfiguration, so that is the one this module
looks for.
"""

from __future__ import annotations

import logging
import posixpath
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

from queryfix.db.liquibase import ReadChangelogFile

__all__ = [
    "DEFAULT_CANDIDATE_PATHS",
    "PROPERTY_KEY",
    "DiscoveryResult",
    "DiscoveryStatus",
    "discover_liquibase_changelog",
]

logger = logging.getLogger(__name__)

#: The exact Spring configuration key this module looks for. See the module
#: docstring for why it is not ``spring.liquibase.change-log``.
PROPERTY_KEY = "db.liquibase.change-log"

#: The same key, as the nested-mapping path it takes in YAML.
_YAML_PATH = ("db", "liquibase", "change-log")

#: Standard Spring Boot configuration locations, in deterministic precedence
#: order — searched in full (not stopped at the first hit) so that two base
#: configuration files disagreeing about the changelog can be detected as
#: :attr:`DiscoveryStatus.AMBIGUOUS` rather than one silently shadowing the
#: other.
#:
#: Order, and why: the conventional Maven/Gradle resource root
#: (``src/main/resources/``) before a repository-root fallback, since that is
#: where a Spring Boot module's configuration lives by convention; and within
#: each location, ``.properties`` before ``.yml`` before ``.yaml``, matching
#: every example in this feature's own specification and real
#: repositories. A repository whose configuration lives somewhere else
#: entirely (a non-standard resource root, a multi-module layout with no file
#: at either standard location) is not discovered automatically — see the
#: module docstring's "What this module does not do" — and falls back to
#: :data:`~queryfix.pipeline.static_rules.schema.UNKNOWN_SCHEMA` exactly as
#: an unconfigured repository always has, unless
#: ``LIQUIBASE_CHANGELOG_PATH`` is set explicitly.
DEFAULT_CANDIDATE_PATHS: tuple[str, ...] = (
    "src/main/resources/application.properties",
    "src/main/resources/application.yml",
    "src/main/resources/application.yaml",
    "application.properties",
    "application.yml",
    "application.yaml",
)


class DiscoveryStatus(str, Enum):
    """What :func:`discover_liquibase_changelog` was able to establish."""

    #: Exactly one changelog reference was found (or several agreeing ones).
    DISCOVERED = "discovered"
    #: No candidate configuration file declared the key at all.
    NOT_FOUND = "not_found"
    #: Two or more candidate files declared *different* changelog values.
    AMBIGUOUS = "ambiguous"
    #: The key was present but its value was blank or otherwise unusable.
    INVALID = "invalid"


@dataclass(frozen=True)
class DiscoveryResult:
    """The outcome of one discovery attempt.

    ``changelog_path`` is populated only when ``status`` is
    :attr:`DiscoveryStatus.DISCOVERED`, already normalized to the
    repository-relative POSIX path
    :func:`~queryfix.db.liquibase.build_table_schemas_remote` expects as its
    ``root`` — classpath-relative values resolved against the resource root the
    declaring configuration file was itself found under (see the module
    docstring), backslashes converted to forward slashes, no leading slash.
    """

    status: DiscoveryStatus
    changelog_path: str | None = None
    source_file: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class _Candidate:
    """One configuration file that declared a changelog reference."""

    source_file: str
    changelog_path: str


def discover_liquibase_changelog(
    read: ReadChangelogFile,
    *,
    candidate_paths: Sequence[str] = DEFAULT_CANDIDATE_PATHS,
) -> DiscoveryResult:
    """Locate the root Liquibase changelog from a repository's own configuration.

    ``read`` is called with each of ``candidate_paths`` in turn — repository
    root-relative POSIX paths, the same shape
    :data:`~queryfix.db.liquibase.ReadChangelogFile` already uses. A ``read``
    failure (however it is signalled — the caller's exception type is not this
    module's business, matching
    :func:`~queryfix.db.liquibase._collect_remote`'s own handling of its
    ``read``) means only "this candidate does not exist", never a reason to
    stop looking at the others or to raise.

    Every candidate is checked, not just the first that resolves, precisely so
    that two disagreeing configuration files are caught as
    :attr:`DiscoveryStatus.AMBIGUOUS` rather than one being silently preferred —
    picking the wrong one would misjudge every schema-dependent finding for the
    rest of the run.
    """
    found: list[_Candidate] = []
    first_invalid: tuple[str, str] | None = None

    for path in candidate_paths:
        text = _read_candidate(read, path)
        if text is None:
            continue

        raw_value = _extract_change_log_value(path, text)
        if raw_value is None:
            continue

        if not raw_value.strip():
            if first_invalid is None:
                first_invalid = (path, f"{PROPERTY_KEY} is declared but blank")
            continue

        resource_root = posixpath.dirname(path)
        changelog_path = _resolve_reference(raw_value, resource_root)
        found.append(_Candidate(source_file=path, changelog_path=changelog_path))

    if not found:
        if first_invalid is not None:
            source, reason = first_invalid
            return DiscoveryResult(
                status=DiscoveryStatus.INVALID, source_file=source, reason=reason
            )
        return DiscoveryResult(
            status=DiscoveryStatus.NOT_FOUND,
            reason=f"no application configuration declared {PROPERTY_KEY}",
        )

    distinct_targets = {candidate.changelog_path for candidate in found}
    if len(distinct_targets) > 1:
        sources = ", ".join(candidate.source_file for candidate in found)
        reason = (
            f"{len(found)} application configuration files declare different "
            f"{PROPERTY_KEY} values ({sources}); refusing to guess which applies"
        )
        return DiscoveryResult(status=DiscoveryStatus.AMBIGUOUS, reason=reason)

    winner = found[0]
    return DiscoveryResult(
        status=DiscoveryStatus.DISCOVERED,
        changelog_path=winner.changelog_path,
        source_file=winner.source_file,
    )


def _read_candidate(read: ReadChangelogFile, path: str) -> str | None:
    """``read(path)``, or None for any failure — "not found" is not exceptional."""
    try:
        return read(path)
    except Exception:
        return None


def _extract_change_log_value(path: str, text: str) -> str | None:
    """The raw, unresolved changelog reference declared in one file, if any."""
    if path.endswith((".yml", ".yaml")):
        return _parse_yaml_change_log(text)
    return _parse_properties(text).get(PROPERTY_KEY)


def _parse_properties(text: str) -> dict[str, str]:
    """A narrow ``.properties`` reader: whitespace, comments, ``=``/``:``, quotes.

    Handles exactly what an audited ``application.properties`` needs: blank
    lines and ``#``/``!`` comment lines are skipped, a key and value split on
    the first ``=`` or ``:`` with surrounding whitespace trimmed from each
    side, and a value wrapped in a single matching pair of quotes has them
    removed. Not a full Java properties implementation — no line continuation,
    no ``\\uXXXX`` escapes, no whitespace-only separators — because none of
    those appear in any configuration this feature has been audited against,
    and the point of this module is one key, not a general-purpose format.
    """
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", "!")):
            continue
        separator = _first_of(line, "=:")
        if separator is None:
            continue
        key = line[:separator].strip()
        value = _unquote(line[separator + 1 :].strip())
        if key:
            values[key] = value
    return values


def _first_of(text: str, characters: str) -> int | None:
    """The index of the first character in ``text`` that appears in ``characters``."""
    for index, char in enumerate(text):
        if char in characters:
            return index
    return None


def _parse_yaml_change_log(text: str) -> str | None:
    """Read ``db.liquibase.change-log`` out of a narrow Spring-shaped YAML block.

    Understands exactly two shapes, both by walking indentation rather than
    parsing YAML generally:

    * The nested block Spring conventionally uses::

          db:
            liquibase:
              change-log: classpath:/db/changelog/master.xml

    * The equivalent compact dotted key some configurations use instead::

          db.liquibase.change-log: classpath:/db/changelog/master.xml

    A stack of ``(indent, key)`` tracks the current nesting path; a line whose
    indentation is less than or equal to the stack's top pops back out to the
    right ancestor first, matching how YAML block structure itself is
    delimited by indentation. List items (``- foo``) are skipped rather than
    guessed at — this key is never a sequence element in any Spring
    configuration this module has been audited against.
    """
    stack: list[tuple[int, str]] = []

    for raw_line in text.splitlines():
        line = _strip_yaml_comment(raw_line)
        if not line.strip():
            continue

        indent = len(line) - len(line.lstrip(" "))
        content = line.strip()
        if content.startswith("- "):
            continue

        key, colon, rest = content.partition(":")
        if not colon:
            continue
        key = _unquote(key.strip())
        value = _unquote(rest.strip())

        while stack and stack[-1][0] >= indent:
            stack.pop()
        stack.append((indent, key))

        if not value:
            continue
        path = tuple(entry_key for _, entry_key in stack)
        if path == _YAML_PATH or key == PROPERTY_KEY:
            return value

    return None


def _strip_yaml_comment(line: str) -> str:
    """Drop a trailing ``# comment``, honouring simple quoting.

    A ``#`` only starts a comment at the beginning of the line or when
    preceded by whitespace — YAML's own rule for when ``#`` is significant —
    and never while inside a single- or double-quoted run, so a changelog path
    containing ``#`` (unlikely, but not this module's business to forbid)
    survives.
    """
    in_single = False
    in_double = False
    for index, char in enumerate(line):
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif (
            char == "#"
            and not in_single
            and not in_double
            and (index == 0 or line[index - 1].isspace())
        ):
            return line[:index]
    return line


def _unquote(value: str) -> str:
    """Strip one matching pair of surrounding quotes, if present."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _resolve_reference(raw: str, resource_root: str) -> str:
    """Normalize a declared changelog reference into a repository-relative POSIX path.

    Handles every form the task's own specification names:

    * ``classpath:/db/changelog/master.xml`` — the ``classpath:`` prefix is
      stripped, and the remainder is resolved against ``resource_root`` (the
      directory the declaring configuration file was itself found under),
      exactly matching where Liquibase's own classpath resolution would find
      it under Spring Boot's default resource root. This is derived from
      *where the configuration file was found* rather than a hard-coded
      ``src/main/resources``, so a module whose configuration lives at
      ``service-a/src/main/resources/application.properties`` resolves its
      classpath references under ``service-a/src/main/resources/`` too.
    * ``db/changelog/master.xml`` (no prefix) — Liquibase treats an unprefixed
      reference as classpath-relative by default, so this resolves the same
      way as the ``classpath:`` form above.
    * ``src/main/resources/db/changelog/master.xml`` (already fully
      repository-relative) — used as-is, on the theory that a value already
      naming a resource root is deliberate and should not be joined with one
      again.

    Backslashes are converted to forward slashes and a leading slash is
    stripped before any of the above, so Windows-style paths and a stray
    absolute-looking leading ``/`` are both handled the same way.
    """
    value = _unquote(raw.strip())
    value = value.replace("\\", "/")
    if value.startswith("classpath:"):
        value = value[len("classpath:") :]
    value = value.lstrip("/")

    if not resource_root or value.startswith(("src/", "test/")):
        return posixpath.normpath(value)
    return posixpath.normpath(posixpath.join(resource_root, value))
