"""Per-repository configuration: the ``.queryfix.yml`` a consuming repo may commit.

Organisation-wide adoption needs a way for one team to say "not this path" or
"this rule is advisory for us" without a change to QueryFix or to a shared
workflow. This module is that surface, and it is deliberately small:

.. code-block:: yaml

    # .queryfix.yml, repository root
    block-severities: [critical]        # which severities make the review REQUEST_CHANGES
    warn-rules: [no-limit]              # always visible, never blocking
    block-rules: [missing-where]        # always blocking, whatever the severity
    disable-rules: [select-star]        # static rules that do not run at all
    ignore-paths:                       # files QueryFix does not read
      - "src/test/**"
      - "**/legacy/**"

Three properties matter more than the options themselves:

* **Read from the PR head, over the API.** Like every other input, so no checkout
  is needed and the config a PR carries is the config that reviews it.
* **Strict.** An unknown key or an invalid value is an error, never ignored: a
  typo that silently disables nothing (or, worse, silently disables a gate) is
  exactly the failure a gate cannot have. The error is surfaced in the report as
  a degraded stage and the run falls back to the defaults, so a broken config
  never blocks a merge by itself and never hides one either.
* **The organisation outranks the repository.** A value set through the
  environment (``QUERYFIX_BLOCK_SEVERITIES`` and friends, i.e. the workflow
  inputs an org controls) wins over the file; the file only fills what the org
  left unset. See :func:`apply_to_policy`.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Sequence
from functools import lru_cache

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from queryfix.models.finding import Severity
from queryfix.models.query import SourceFile
from queryfix.policy import EnforcementPolicy

__all__ = [
    "CONFIG_PATH",
    "InvalidRepoConfig",
    "RepoConfig",
    "apply_to_policy",
    "filter_ignored_sources",
    "parse_repo_config",
]

#: Where the file lives, relative to the repository root.
CONFIG_PATH = ".queryfix.yml"


class InvalidRepoConfig(ValueError):
    """The repository's ``.queryfix.yml`` could not be interpreted safely."""


class RepoConfig(BaseModel):
    """The validated contents of a ``.queryfix.yml``. Every field is optional."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    block_severities: list[Severity] | None = Field(default=None, alias="block-severities")
    warn_rules: list[str] = Field(default_factory=list, alias="warn-rules")
    block_rules: list[str] = Field(default_factory=list, alias="block-rules")
    disable_rules: list[str] = Field(default_factory=list, alias="disable-rules")
    ignore_paths: list[str] = Field(default_factory=list, alias="ignore-paths")

    @field_validator("block_severities", mode="before")
    @classmethod
    def _lowercase_severities(cls, value: object) -> object:
        if isinstance(value, list):
            return [item.lower() if isinstance(item, str) else item for item in value]
        return value

    @field_validator("warn_rules", "block_rules", "disable_rules", "ignore_paths")
    @classmethod
    def _no_blank_entries(cls, value: list[str]) -> list[str]:
        if any(not item.strip() for item in value):
            msg = "entries must not be blank"
            raise ValueError(msg)
        return [item.strip() for item in value]


def parse_repo_config(text: str) -> RepoConfig:
    """Parse and validate ``.queryfix.yml`` text.

    An empty file is a valid, all-defaults config. Raises
    :class:`InvalidRepoConfig` with a message safe to show in a PR review.
    """
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as error:
        msg = f"{CONFIG_PATH} is not valid YAML: {error}"
        raise InvalidRepoConfig(msg) from error
    if raw is None:
        return RepoConfig()
    if not isinstance(raw, dict):
        msg = f"{CONFIG_PATH} must be a mapping of settings, not {type(raw).__name__}"
        raise InvalidRepoConfig(msg)
    try:
        return RepoConfig.model_validate(raw)
    except ValidationError as error:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in issue['loc']) or '<root>'}: {issue['msg']}"
            for issue in error.errors()
        )
        msg = f"{CONFIG_PATH} is invalid: {problems}"
        raise InvalidRepoConfig(msg) from error


def apply_to_policy(
    policy: EnforcementPolicy, config: RepoConfig, *, org_sets_block_severities: bool
) -> EnforcementPolicy:
    """Layer a repository's choices onto the organisation's policy.

    Rule lists are unioned: a repo can make more rules advisory, and can add
    blocking rules, on top of whatever the org already said. ``block-severities``
    replaces the org default only when the org did not set one explicitly
    (``org_sets_block_severities``), so a central "CRITICAL,HIGH must block"
    cannot be loosened by a single repository's file.

    Precedence between the rule lists is :meth:`EnforcementPolicy.should_block`'s
    own: a warn rule beats a block rule, so a repo cannot be made to block on a
    rule the org marked advisory-only either.
    """
    blocking = policy.blocking_severities
    if config.block_severities is not None and not org_sets_block_severities:
        blocking = frozenset(config.block_severities)
    return dataclasses.replace(
        policy,
        blocking_severities=blocking,
        warning_rule_ids=policy.warning_rule_ids | frozenset(config.warn_rules),
        blocking_rule_ids=policy.blocking_rule_ids | frozenset(config.block_rules),
    )


def filter_ignored_sources(
    sources: Sequence[SourceFile], config: RepoConfig
) -> tuple[list[SourceFile], list[str]]:
    """Drop the sources ``ignore-paths`` excludes; also return which paths were dropped."""
    if not config.ignore_paths:
        return list(sources), []
    patterns = [_glob_to_regex(pattern) for pattern in config.ignore_paths]
    kept: list[SourceFile] = []
    dropped: list[str] = []
    for source in sources:
        if any(pattern.match(source.path) for pattern in patterns):
            dropped.append(source.path)
        else:
            kept.append(source)
    return kept, dropped


@lru_cache(maxsize=256)
def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    """``**`` crosses directories, ``*`` and ``?`` do not; a leading ``/`` is dropped."""
    pattern = pattern.lstrip("/")
    out: list[str] = []
    i = 0
    while i < len(pattern):
        char = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif char == "*":
            out.append("[^/]*")
            i += 1
        elif char == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(char))
            i += 1
    return re.compile("".join(out) + r"\Z")
