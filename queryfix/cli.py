"""Command-line entry point for reviewing one GitHub pull request."""

from __future__ import annotations

import argparse
import logging
import re
import sys
import traceback
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from queryfix.models.report import Report, RunContext
from queryfix.pipeline.diff import INGEST_STAGE
from queryfix.policy import (
    EnforcementPolicy,
    EnforcementStatus,
    InvalidEnforcementPolicy,
    ReviewResult,
)

if TYPE_CHECKING:
    from github import Github

    from queryfix.integrations.llm import LLMExplanationProvider
    from queryfix.models.diff import DiffIngest
    from queryfix.pipeline.extract.java_structure import ResolveJavaSource
    from queryfix.pipeline.runner import AnalysisRunner
    from queryfix.pipeline.static_rules.schema import SchemaProvider
    from queryfix.repo_config import RepoConfig

logger = logging.getLogger(__name__)

#: Exit code contract (Phase 9). ``PASS`` and ``DEGRADED`` both use 0 — CLAUDE.md
#: invariant 5: a stage that failed soft must not fail the PR check by default,
#: it must only be visible in the review body and this process's stderr summary.
EXIT_PASS = 0
#: A GitHub Action step exiting non-zero fails the check. ``BLOCKED`` and
#: ``FAILED`` are deliberately different exit codes rather than both mapping to
#: a single "review is red" status, so a consumer's own automation (or a human
#: skimming `queryfix review; echo $?`) can tell "there is a real blocking
#: finding" from "QueryFix itself could not reliably analyze this pull
#: request" without parsing stderr.
EXIT_FAILED = 1
EXIT_BLOCKED = 2
#: A configuration QueryFix cannot safely interpret (a malformed
#: `QUERYFIX_BLOCK_SEVERITIES`, a missing required credential) is neither a
#: review outcome nor a GitHub/network failure — distinct from both so a
#: consumer does not mistake "you configured this wrong" for "the pull request
#: has a problem" or "GitHub was unreachable".
EXIT_CONFIG_ERROR = 3

_EXIT_CODE_FOR_STATUS: dict[EnforcementStatus, int] = {
    EnforcementStatus.PASS: EXIT_PASS,
    EnforcementStatus.DEGRADED: EXIT_PASS,
    EnforcementStatus.BLOCKED: EXIT_BLOCKED,
    EnforcementStatus.FAILED: EXIT_FAILED,
}

_REPOSITORY = re.compile(r"^[^/\s]+/[^/\s]+$")


def _repository(value: str) -> str:
    """Accept precisely GitHub's owner/repository form at the CLI boundary."""
    if not _REPOSITORY.fullmatch(value):
        raise argparse.ArgumentTypeError("--repo must use OWNER/REPO format")
    return value


def _pr_number(value: str) -> int:
    """Parse a positive pull-request number without accepting zero or negatives."""
    try:
        number = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("--pr must be a positive integer") from error
    if number <= 0:
        raise argparse.ArgumentTypeError("--pr must be a positive integer")
    return number


def build_parser() -> argparse.ArgumentParser:
    """Build the small public command surface."""
    parser = argparse.ArgumentParser(prog="queryfix")
    commands = parser.add_subparsers(dest="command", required=True)
    review = commands.add_parser("review", help="review a pull request")
    review.add_argument("--repo", required=True, type=_repository, metavar="OWNER/REPO")
    review.add_argument("--pr", required=True, type=_pr_number, metavar="NUMBER")
    review.add_argument("--post-comment", action="store_true")
    review.add_argument("--dry-run", action="store_true")
    review.add_argument(
        "--no-llm",
        action="store_true",
        help="skip LLM-authored explanations even when GROQ_API_KEY is configured; "
        "findings are still fully deterministic without it",
    )
    review.add_argument(
        "--debug",
        action="store_true",
        help="show an ImportError traceback for local diagnosis",
    )
    review.add_argument(
        "--fixture",
        type=Path,
        metavar="PATH",
        help="run --dry-run against an existing recorded PR fixture, without GitHub or Groq access",
    )
    return parser


def _load_effective_schema(
    context: RunContext,
    ingested: DiffIngest,
    client: Github,
) -> SchemaProvider:
    """The schema static rules should see for this run.

    Two independent paths, chosen by whether ``LIQUIBASE_CHANGELOG_PATH`` is
    configured — configuration precedence, not a fallback chain within either
    path:

    * **Explicit** (:func:`_load_explicit_schema`) — unchanged from before this
      module gained automatic discovery. A configured path is read from local
      disk and never silently second-guessed by discovery, however discovery
      might resolve the same repository; an operator who set the variable gets
      exactly what they configured.
    * **Discovered** (:func:`_load_discovered_schema`) — only reached when
      nothing is configured. QueryFix looks for the repository's own
      ``db.liquibase.change-log`` declaration instead of requiring one.

    Explicit configuration always wins; see ``queryfix/db/discovery.py``'s
    module docstring for what discovery does and does not attempt.
    """
    from queryfix.config import get_settings

    changelog_path = get_settings().liquibase_changelog_path
    if changelog_path is not None:
        return _load_explicit_schema(context, ingested, client, changelog_path)
    return _load_discovered_schema(context, client)


def _load_explicit_schema(
    context: RunContext,
    ingested: DiffIngest,
    client: Github,
    changelog_path: str,
) -> SchemaProvider:
    """The schema for an explicitly configured ``LIQUIBASE_CHANGELOG_PATH``.

    Local-disk by default — the behaviour QueryFix has always had. If the pull
    request's changed files touch the changelog tree that schema was built from,
    the schema is instead rebuilt from the *complete* changelog tree at the pull
    request's head commit, over the GitHub API, so a query added alongside a
    schema change in the same pull request is judged against the schema the pull
    request will actually produce rather than the one that predates it. See
    ``queryfix/db/liquibase.py``'s module docstring for why this is a full
    rebuild rather than a delta applied on top of the local-disk schema.
    """
    from queryfix.db.liquibase import (
        LiquibaseParseError,
        load_liquibase_schema_remote,
        load_schema_from_settings,
        resolve_pr_changelog_touch,
    )
    from queryfix.integrations.github import read_file_at_ref
    from queryfix.pipeline.static_rules.schema import StaticSchemaProvider

    schema = load_schema_from_settings(changelog_path)

    if not isinstance(schema, StaticSchemaProvider):
        # The local changelog could not be loaded at all — the same silent stub
        # QueryFix has always fallen back to. There is nothing to rebuild from
        # a pull request in that case.
        return schema

    head_sha = context.head_sha
    if head_sha is None:
        return schema

    changed_paths = {
        *(source.path for source in ingested.sources),
        *(skipped.path for skipped in ingested.skipped),
        *(marker.removeprefix(f"{INGEST_STAGE}:") for marker in ingested.degraded_stages),
    }
    if not resolve_pr_changelog_touch(changelog_path, changed_paths):
        return schema

    try:
        return load_liquibase_schema_remote(
            changelog_path,
            lambda path: read_file_at_ref(context, path, ref=head_sha, client=client),
        )
    except LiquibaseParseError:
        logger.exception(
            "failed to rebuild the Liquibase schema from the pull request's head "
            "commit; falling back to the local-disk schema",
            extra={"run_id": context.run_id, "repo": context.repo, "pr_number": context.pr_number},
        )
        return schema


def _load_discovered_schema(context: RunContext, client: Github) -> SchemaProvider:
    """The schema QueryFix finds on its own, with no configured changelog path.

    Sourced entirely over the GitHub API, pinned to the pull request's head
    commit — never local disk, because nothing guarantees a local checkout of
    an arbitrary ``OWNER/REPO`` corresponds to the repository being reviewed
    (see ``queryfix/db/discovery.py``'s module docstring). Discovery first
    locates ``db.liquibase.change-log`` in the repository's own configuration
    (a handful of small reads, never a repository-wide scan), then the
    *existing* remote Liquibase loader walks the changelog tree it names —
    exactly the loader :func:`_load_explicit_schema` already uses for a
    pull-request-head rebuild, reused rather than duplicated.

    Because this always reads at the pull request's head, there is no separate
    "did the pull request touch the changelog" gate the way
    :func:`_load_explicit_schema` has: a query-only pull request and a
    schema-changing one are both answered by the same one remote walk, and
    both correctly see the schema as it will actually be once the pull request
    merges.

    If conventional-location discovery finds nothing usable — no configuration
    declared the key, the declared path is blank, or the path it names does
    not actually resolve to a real changelog — this does not give up
    immediately (Phase 2.3): it falls back to a repository-wide candidate
    search (:mod:`queryfix.db.candidate_discovery`) before degrading to
    :data:`~queryfix.pipeline.static_rules.schema.UNKNOWN_SCHEMA`. The one
    outcome that does *not* trigger the fallback is
    :attr:`~queryfix.db.discovery.DiscoveryStatus.AMBIGUOUS`: two
    configuration files genuinely disagreeing is a reason to stop guessing,
    not a reason to guess harder from the repository tree.

    Every degraded outcome is logged but never raised: an unconfigured,
    undiscoverable repository must analyze exactly as it did before this
    feature existed, not turn into a failed run.
    """
    from queryfix.db.candidate_discovery import resolve_candidate_discovery
    from queryfix.db.discovery import (
        DiscoveryResult,
        DiscoveryStatus,
        discover_liquibase_changelog,
    )
    from queryfix.db.liquibase import LiquibaseParseError, build_table_schemas_remote
    from queryfix.integrations.github import (
        GitHubUnavailable,
        list_files_at_ref,
        read_file_at_ref,
    )
    from queryfix.pipeline.static_rules.schema import UNKNOWN_SCHEMA, StaticSchemaProvider

    log_context = {"run_id": context.run_id, "repo": context.repo, "pr_number": context.pr_number}

    head_sha = context.head_sha
    if head_sha is None:
        # No commit to pin reads to — nothing here could describe a tree that
        # actually exists.
        return UNKNOWN_SCHEMA

    def read(path: str) -> str:
        return read_file_at_ref(context, path, ref=head_sha, client=client)

    def load(result: DiscoveryResult, *, source: str) -> StaticSchemaProvider | None:
        """One discovery outcome, loaded if it names a working changelog.

        ``None`` — not :data:`UNKNOWN_SCHEMA` — on anything short of success,
        so the caller can tell "this attempt found nothing usable, the next
        fallback should run" from "this attempt is the final answer" without
        re-inspecting ``result.status`` itself.
        """
        if result.status is not DiscoveryStatus.DISCOVERED or result.changelog_path is None:
            return None
        logger.info(
            "Liquibase changelog found (%s): %s (%s)",
            source,
            result.changelog_path,
            result.source_file,
            extra={**log_context, "changelog_path": result.changelog_path, "source": source},
        )
        try:
            tables = build_table_schemas_remote(result.changelog_path, read)
        except LiquibaseParseError:
            logger.exception(
                "changelog %s (%s) could not be loaded",
                result.changelog_path,
                source,
                extra=log_context,
            )
            return None
        logger.info(
            "Liquibase schema loaded (%s): %d table(s)",
            source,
            len(tables),
            extra={**log_context, "number_of_tables": len(tables)},
        )
        return StaticSchemaProvider(tables)

    primary = discover_liquibase_changelog(read)
    schema = load(primary, source="application configuration")
    if schema is not None:
        return schema

    if primary.status is DiscoveryStatus.AMBIGUOUS:
        logger.warning(
            "Liquibase changelog discovery is ambiguous: %s; set "
            "LIQUIBASE_CHANGELOG_PATH explicitly to resolve it",
            primary.reason,
            extra=log_context,
        )
        return UNKNOWN_SCHEMA

    if primary.status is DiscoveryStatus.NOT_FOUND:
        logger.debug(
            "no LIQUIBASE_CHANGELOG_PATH configured and no application "
            "configuration declared db.liquibase.change-log",
            extra=log_context,
        )
    elif primary.status is DiscoveryStatus.INVALID:
        logger.warning(
            "Liquibase changelog discovery found an unusable configuration in %s: %s",
            primary.source_file,
            primary.reason,
            extra=log_context,
        )
    # DISCOVERED-but-unloadable falls through here too: `load` already logged
    # why, via LiquibaseParseError above.

    logger.info(
        "falling back to a repository-wide Liquibase candidate search",
        extra=log_context,
    )
    try:
        paths = list_files_at_ref(context, ref=head_sha, client=client)
    except GitHubUnavailable:
        logger.warning(
            "could not list the repository tree for candidate search; continuing without a schema",
            extra=log_context,
        )
        return UNKNOWN_SCHEMA

    fallback = resolve_candidate_discovery(paths, read)
    schema = load(fallback, source="repository fallback")
    if schema is not None:
        return schema

    if fallback.status is DiscoveryStatus.AMBIGUOUS:
        logger.warning(
            "Liquibase changelog candidate search is ambiguous: %s; set "
            "LIQUIBASE_CHANGELOG_PATH explicitly to resolve it",
            fallback.reason,
            extra=log_context,
        )
    else:
        logger.debug(
            "no Liquibase changelog could be found, configured, or discovered; "
            "continuing without a schema",
            extra=log_context,
        )
    return UNKNOWN_SCHEMA


def _java_source_resolver(context: RunContext, client: Github) -> ResolveJavaSource | None:
    """Read a Java file at the pull request's head, for cross-file resolution.

    An N+1 routinely spans files a pull request did not touch: the loop is edited,
    the repository interface it calls is not. Without a way to read that
    interface, its type resolves by naming convention alone and the finding is
    reported a tier weaker than the evidence deserves.

    Three properties make this safe to hand to the analyzer:

    * **Targeted.** The analyzer asks only for types actually used as receivers,
      by a path derived from the calling file's own package and imports. It never
      enumerates the repository.
    * **Cached, including misses.** A path that 404s is remembered as absent, so a
      type referenced from twenty call sites costs at most one request.
    * **Silent on failure.** GitHub being unavailable degrades resolution, not the
      run — the caller still gets every structural finding, just with the weaker
      tier. That is the same fail-soft contract every other optional input has.

    Returns None when the context carries no head SHA, since a read that cannot be
    pinned to a commit could describe a tree that never existed.
    """
    from queryfix.integrations.github import GitHubUnavailable, read_file_at_ref

    head_sha = context.head_sha
    if head_sha is None:
        return None

    cache: dict[str, str | None] = {}

    def resolve(path: str) -> str | None:
        if path not in cache:
            try:
                cache[path] = read_file_at_ref(context, path, ref=head_sha, client=client)
            except GitHubUnavailable:
                logger.debug(
                    "could not read %s at the head commit; falling back to "
                    "name-convention resolution",
                    path,
                    extra={"run_id": context.run_id, "source_path": path},
                )
                cache[path] = None
        return cache[path]

    return resolve


def _resolve_dialect(
    context: RunContext, client: Github, changed_paths: Sequence[str] = ()
) -> tuple[str | None, list[str]]:
    """The SQL dialect for this run, plus any degradation worth naming.

    ``QUERYFIX_SQL_DIALECT`` wins; otherwise it is detected from the repository's
    own JDBC URL at the PR head (:func:`~queryfix.db.dialect.detect_sql_dialect`).
    ``None`` means "keep the default". An override ``sqlglot`` cannot read is named
    in the report and ignored rather than silently producing a Postgres run that the
    operator believes is MySQL.
    """
    from queryfix.config import get_settings
    from queryfix.db.dialect import config_candidates_for, detect_sql_dialect, is_known_dialect
    from queryfix.integrations.github import GitHubUnavailable, read_file_at_ref

    override = (get_settings().sql_dialect or "").strip()
    if override:
        if is_known_dialect(override):
            return override, []
        return None, [f"sql_dialect:unknown dialect {override!r}; reading SQL as the default"]

    head_sha = context.head_sha
    if head_sha is None:
        return None, []

    def read(path: str) -> str | None:
        try:
            return read_file_at_ref(context, path, ref=head_sha, client=client)
        except GitHubUnavailable:
            return None

    return detect_sql_dialect(read, candidate_paths=config_candidates_for(changed_paths)), []


def _load_repo_config(context: RunContext, client: Github) -> tuple[RepoConfig, list[str]]:
    """The repository's ``.queryfix.yml`` at the PR head, plus any degradation.

    Absent (or unreadable) means the defaults, silently: most repositories will
    never have one. A present-but-invalid file is the opposite case — the author
    meant something and QueryFix did not do it — so it is named in the report
    rather than swallowed.
    """
    from queryfix.integrations.github import GitHubUnavailable, read_file_at_ref
    from queryfix.repo_config import (
        CONFIG_PATH,
        InvalidRepoConfig,
        RepoConfig,
        parse_repo_config,
    )

    if context.head_sha is None:
        return RepoConfig(), []
    try:
        text = read_file_at_ref(context, CONFIG_PATH, ref=context.head_sha, client=client)
    except GitHubUnavailable:
        return RepoConfig(), []
    try:
        return parse_repo_config(text), []
    except InvalidRepoConfig as error:
        logger.warning("%s", error, extra={"run_id": context.run_id})
        return RepoConfig(), [f"repo_config:{error}"]


def _scope_to_changes() -> bool:
    """Whether findings are limited to code the PR touched (``QUERYFIX_SCOPE``)."""
    from queryfix.config import get_settings

    scope = get_settings().scope.strip().lower() or "changed"
    if scope not in {"changed", "all"}:
        msg = f"QUERYFIX_SCOPE must be 'changed' or 'all', not {scope!r}"
        raise InvalidEnforcementPolicy(msg)
    return scope == "changed"


def review(
    repo: str,
    pr_number: int,
    *,
    post_comment: bool = False,
    dry_run: bool = False,
    client: Github | None = None,
    runner: AnalysisRunner | None = None,
    llm_provider: LLMExplanationProvider | None = None,
    enforcement_policy: EnforcementPolicy | None = None,
    org_sets_block_severities: bool = False,
) -> ReviewResult:
    """Orchestrate ingest, analysis, rendering, and the Pull Request Review.

    An injected client is the offline seam used by recorded fixtures. In a normal
    invocation, ``new_client`` obtains and validates GitHub configuration.

    ``llm_provider`` defaults to ``None``: this function reads no LLM-related
    configuration on its own, so calling it directly — as the whole test suite
    does — costs nothing and touches no network beyond GitHub's. ``main`` is the
    one caller that constructs a provider from process configuration (respecting
    ``--no-llm``) and passes it in; a caller embedding :func:`review` that wants
    the same behavior can do the same with
    :func:`queryfix.integrations.groq.create_default_llm_provider`. Ignored
    when ``runner`` is supplied directly, the same way ``runner`` already
    supersedes the schema this function would otherwise load. ``enforcement_policy``
    defaults the same way, to :class:`~queryfix.policy.EnforcementPolicy`'s own
    default rather than reading configuration here — ``main`` builds the real one
    from :meth:`~queryfix.config.Settings.enforcement_policy`.

    A :class:`~queryfix.integrations.github.GitHubUnavailable` from ingestion —
    GitHub could not be reached, or refused a request, before a single file was
    read — degrades rather than raises (CLAUDE.md invariant 5): the caller still
    gets a :class:`~queryfix.policy.ReviewResult` whose status is
    :attr:`~queryfix.policy.EnforcementStatus.FAILED`, naming the failure, with
    no queries and no findings rather than either an exception or a review that
    could read as "nothing wrong here". Nothing else is caught this broadly. A
    programming error — ``TypeError``, ``AssertionError``, any bug in extraction
    or the rule engine — is not GitHub being unavailable, and must still reach
    the caller: silently degrading those would hide a real QueryFix failure
    behind a green check, which is a worse outcome than a loud one.

    The repository's own ``.queryfix.yml`` (:mod:`queryfix.repo_config`), read at
    the PR head, layers per-repo choices onto ``enforcement_policy`` and filters
    ``ignore-paths`` out of the sources. ``org_sets_block_severities`` says the
    caller's policy came from an explicit organisation setting, which the file
    must not override. A broken config degrades the run (named in the report) and
    falls back to the unmodified policy.

    ``post_comment`` posts QueryFix's Pull Request Review
    (:func:`~queryfix.integrations.github.upsert_review`) — CLAUDE.md invariant
    3/4's primary output. The parameter name predates that becoming a real
    review rather than an issue comment; kept rather than renamed, since a CLI
    flag is a compatibility surface the same way a public function's name is,
    and ``--post-comment`` in an existing workflow file must keep working.
    """
    from queryfix.integrations.github import GitHubUnavailable, new_client, upsert_review
    from queryfix.pipeline.ingest import ingest_pull_request
    from queryfix.pipeline.runner import AnalysisRunner
    from queryfix.pipeline.static_rules import RuleEngine
    from queryfix.pipeline.static_rules.base import RULES
    from queryfix.repo_config import CONFIG_PATH, apply_to_policy, filter_ignored_sources

    resolved_client = client if client is not None else new_client()
    policy = enforcement_policy if enforcement_policy is not None else EnforcementPolicy()

    try:
        ingested = ingest_pull_request(repo, pr_number, client=resolved_client)
    except GitHubUnavailable as error:
        # The review's target is `repo`/`pr_number` alone — already known from
        # the caller's own arguments, not from anything `ingest_pull_request`
        # would have resolved. A degraded review can still be attempted below
        # even though ingestion itself never got that far.
        context = RunContext(run_id=str(uuid.uuid4()), repo=repo, pr_number=pr_number)
        report = Report(context=context, degraded_stages=[f"{INGEST_STAGE}:{error}"])
        result = policy.evaluate(report)
    else:
        context = ingested.context
        dialect_degraded: list[str] = []
        repo_config, config_degraded = _load_repo_config(context, resolved_client)
        policy = apply_to_policy(
            policy, repo_config, org_sets_block_severities=org_sets_block_severities
        )
        sources, ignored_paths = filter_ignored_sources(ingested.sources, repo_config)
        if ignored_paths:
            logger.info(
                "%s ignore-paths excluded %d file(s) from review",
                CONFIG_PATH,
                len(ignored_paths),
                extra={"run_id": context.run_id},
            )
        if runner is not None:
            resolved_runner = runner
        else:
            schema = _load_effective_schema(context, ingested, resolved_client)
            dialect, dialect_degraded = _resolve_dialect(
                context, resolved_client, [source.path for source in sources]
            )
            disabled = set(repo_config.disable_rules)
            resolved_runner = AnalysisRunner(
                engine=RuleEngine(
                    rules=[rule for rule in RULES if rule.rule_id not in disabled],
                    schema=schema,
                ),
                llm_provider=llm_provider,
                dialect=dialect,
                scope_to_changes=_scope_to_changes(),
            )
        result = resolved_runner.run_review(
            repo=context.repo,
            pr_number=context.pr_number,
            sources=sources,
            context=context,
            initial_degraded_stages=[
                *ingested.degraded_stages,
                *config_degraded,
                *dialect_degraded,
            ],
            resolve_source=_java_source_resolver(context, resolved_client),
            enforcement_policy=policy,
        )

    if post_comment and not dry_run:
        # The integration owns the idempotent create-or-update behavior.
        try:
            upsert_review(context, result, client=resolved_client)
        except GitHubUnavailable:
            # Posting is optional: retain the useful report and make the
            # coverage gap visible, matching the runner's fail-soft contract.
            # Re-evaluated (not just tagged onto the existing result) so a
            # posting failure that pushes the run from PASS to DEGRADED is
            # reflected in the status this function returns, not just in
            # degraded_stages.
            degraded_report = result.report.model_copy(
                update={"degraded_stages": [*result.report.degraded_stages, "post_review"]}
            )
            result = policy.evaluate(degraded_report, findings=result.findings)

    return result


def main(argv: Sequence[str] | None = None) -> int:
    """Run a command, returning an exit status instead of leaking tracebacks."""
    args = build_parser().parse_args(argv)
    if args.command != "review":  # pragma: no cover - argparse enforces this today.
        return 2
    if args.fixture is not None and not args.dry_run:
        build_parser().error("--fixture requires --dry-run")
    if args.fixture is not None and args.post_comment:
        build_parser().error("--fixture cannot be used with --post-comment")

    try:
        client = None
        if args.fixture is not None:
            from queryfix.fixtures import FixtureError, load_recorded_fixture

            recorded, client = load_recorded_fixture(args.fixture)
            if (args.repo, args.pr) != (recorded.repo, recorded.number):
                raise FixtureError(
                    "--repo and --pr must match the repository and pull request recorded by --fixture"
                )

        # Fixture mode is the offline, no-credentials path (see --fixture's help
        # text); Groq is real network the same way GitHub would be, so it stays
        # off there regardless of --no-llm. Everywhere else, GROQ_API_KEY's
        # presence is the switch — --no-llm is the explicit override for someone
        # who has a key configured but wants a purely deterministic run anyway.
        llm_provider = None
        if not args.no_llm and args.fixture is None:
            from queryfix.integrations.groq import create_default_llm_provider

            llm_provider = create_default_llm_provider()

        from queryfix.config import get_settings

        # Built inside the try block: an invalid QUERYFIX_BLOCK_SEVERITIES
        # (etc.) raises InvalidEnforcementPolicy, which the except block below
        # maps to EXIT_CONFIG_ERROR rather than a generic failure.
        enforcement_policy = get_settings().enforcement_policy()

        result = review(
            args.repo,
            args.pr,
            post_comment=args.post_comment,
            dry_run=args.dry_run,
            client=client,
            llm_provider=llm_provider,
            enforcement_policy=enforcement_policy,
            org_sets_block_severities=_org_sets_block_severities(),
        )
        # Rendering is deliberately here, after every optional operation, so stdout
        # is always exactly the final report body and never a credential or log.
        from queryfix.pipeline.report import render_markdown

        _print_markdown(render_markdown(result.report, enforcement=result))
        _print_enforcement_summary(result)
        return _EXIT_CODE_FOR_STATUS[result.status]
    except Exception as error:
        # GitHub's integration already provides safe, actionable messages. For
        # every other boundary (including configuration import), avoid rendering an
        # arbitrary third-party exception string that could contain a credential.
        # Do not import these classes here: a missing token can be raised while
        # importing ``config`` itself, before the module finished loading.
        safe_error_types = {
            ("queryfix.config", "MissingConfiguration"),
            ("queryfix.fixtures", "FixtureError"),
            ("queryfix.integrations.github", "GitHubUnavailable"),
            ("queryfix.policy", "InvalidEnforcementPolicy"),
        }
        # A configuration QueryFix refuses to guess at (EXIT_CONFIG_ERROR) is
        # a materially different outcome from every other safe error here
        # (EXIT_FAILED) — see the exit-code constants' own documentation.
        config_error_types = {
            ("queryfix.config", "MissingConfiguration"),
            ("queryfix.policy", "InvalidEnforcementPolicy"),
        }
        exit_code = (
            EXIT_CONFIG_ERROR
            if (type(error).__module__, type(error).__name__) in config_error_types
            else EXIT_FAILED
        )
        if (type(error).__module__, type(error).__name__) in safe_error_types:
            print(f"QueryFix failed: {error}", file=sys.stderr)
        elif isinstance(error, ImportError):
            # ImportError (including ModuleNotFoundError) messages name a missing
            # module or symbol, never request/response data or configured
            # secrets, so the message and traceback are always safe to show —
            # unconditionally, not just under --debug. This is what turns an
            # opaque "ModuleNotFoundError" in CI into something actionable (e.g.
            # a package declared in requirements.txt but missing from
            # pyproject.toml's install dependencies, which `pip install
            # git+...` builds from instead).
            print(f"QueryFix failed: {type(error).__name__}: {error}", file=sys.stderr)
            traceback.print_exception(type(error), error, error.__traceback__, file=sys.stderr)
        else:
            print(f"QueryFix failed: {type(error).__name__}", file=sys.stderr)
            if args.debug:
                # Deliberately opt-in only: an arbitrary third-party exception's
                # message could embed secret-bearing context (e.g. a URL or
                # header), unlike ImportError above.
                traceback.print_exception(type(error), error, error.__traceback__, file=sys.stderr)
        return exit_code


def _org_sets_block_severities() -> bool:
    """Whether ``QUERYFIX_BLOCK_SEVERITIES`` was set explicitly (see ``repo_config``)."""
    from queryfix.config import get_settings

    value = get_settings().block_severities
    return value is not None and bool(value.strip())


def _print_enforcement_summary(result: ReviewResult) -> None:
    """Print the structured merge decision to stderr, never stdout.

    Stdout stays exactly the review body (:func:`_print_markdown`'s own
    contract, unaffected by this) — this is the machine/operator-facing
    summary a CI log shows beside the exit code, not part of what QueryFix
    would post to GitHub.
    """
    print(f"QueryFix: {result.status.value}", file=sys.stderr)
    if not result.blocking_findings:
        return
    print(f"Blocking findings: {len(result.blocking_findings)}", file=sys.stderr)
    for finding in result.blocking_findings:
        print(f"- {finding.severity.name}: {finding.title}", file=sys.stderr)


def _print_markdown(markdown: str) -> None:
    """Write Markdown even when a Windows console uses a legacy code page."""
    try:
        sys.stdout.write(markdown)
    except UnicodeEncodeError:
        # Report sections use severity emoji. GitHub Actions is UTF-8, but an
        # installed command can also be piped from a Windows terminal configured
        # with cp1252. Bytes preserve the report rather than turning a successful
        # offline review into a spurious CLI failure.
        sys.stdout.buffer.write(markdown.encode("utf-8"))
        sys.stdout.buffer.flush()


def entrypoint() -> None:
    """Console-script wrapper required by ``project.scripts``."""
    raise SystemExit(main())
