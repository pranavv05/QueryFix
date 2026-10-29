# Adopting QueryFix in another repository

QueryFix's pipeline reads a pull request entirely over the GitHub API (diff,
changed files, schema discovery, cross-file N+1 resolution) — it never needs a
local checkout of the repository it is reviewing. That makes adoption a small,
repository-local piece of CI configuration, not a shared service anything has
to be pointed at. This works identically for public and private repositories;
the one thing that differs between them is how fork pull requests are handled
(below).

**What it actually looks at.** QueryFix analyzes raw SQL (`.sql` files,
migrations), JPQL/HQL (`@Query`), JPA native queries
(`@Query(nativeQuery = true)`, `createNativeQuery`), and Spring Data derived
query methods, in Java sources — today's target is a Java/Spring Boot
repository, optionally with a Liquibase-managed schema. Adding the workflow
below to a repository in a different stack is harmless (it will simply find
nothing to extract, and every run reports clean) but won't produce useful
findings.

## Add it to a repository

Create `.github/workflows/queryfix.yml` in the consumer repository:

```yaml
name: QueryFix
on:
  pull_request:
    types: [opened, synchronize]

concurrency:
  group: queryfix-${{ github.event.pull_request.number }}
  cancel-in-progress: true

jobs:
  review:
    uses: pranavv05/QueryFix/.github/workflows/queryfix-review.yml@main
    permissions:
      contents: read
      pull-requests: write
    # secrets: is optional — omit the whole block if you have no GROQ_API_KEY.
    secrets:
      groq-api-key: ${{ secrets.GROQ_API_KEY }}
```

The snippet above omits `with:` entirely — every one of its inputs
(`liquibase-changelog-path`, `groq-model`, `block-severities`, `pr-number`)
is optional, so leave the block out unless you have a specific reason to set
one, e.g.:

```yaml
    with:
      liquibase-changelog-path: src/main/resources/db/changelog.xml
```

That is the whole integration. Nothing else needs installing, vendoring, or
checking out.

**Pin `@main` to a tag or commit SHA once QueryFix cuts a release.** `@main`
is fine while iterating solo; a repo that isn't yours (or that you want to
stop babysitting) should pin, the same reason you'd pin any third-party
Action.

**Sanity-check it locally before wiring it into CI (optional but recommended).**
This catches most configuration mistakes — a missing token scope, an
unreachable Liquibase path — in seconds, without waiting on an Actions run or
posting anything:

```bash
pip install "git+https://github.com/pranavv05/QueryFix.git@main"
export GITHUB_TOKEN=ghp_...   # a token with at least read access to the repo
queryfix review --repo OWNER/REPO --pr NUMBER --dry-run
```

`--dry-run` reads the real pull request and runs the full pipeline, but never
posts anything — it just prints the report and the enforcement decision
(`PASS`/`BLOCKED`/`DEGRADED`/`FAILED`) to your terminal. Drop `--dry-run` only
once you're ready for it to actually post.

## Required and optional configuration

| What | Where it's set | Required? |
| --- | --- | --- |
| GitHub token | Automatic (`secrets.GITHUB_TOKEN`) | Always present, nothing to configure — see the permissions note below, though |
| `GROQ_API_KEY` | Repo or org **secret** ([Settings → Secrets and variables → Actions → New repository secret](https://docs.github.com/en/actions/security-guides/using-secrets-in-github-actions)), passed through as shown above | Optional — unset means N+1 findings are reported without LLM-authored prose, everything else is unaffected |
| `LIQUIBASE_CHANGELOG_PATH` | `with:` input | Optional — auto-discovered from the repo's own `db.liquibase.change-log` declaration; only needed for a non-standard or ambiguous layout |
| `QUERYFIX_BLOCK_SEVERITIES` | `with:` input | Optional — defaults to `CRITICAL,HIGH` blocking (`REQUEST_CHANGES`); everything else posts as `COMMENT` |

No fork of QueryFix, no copy of its source, no per-repo Python environment
to maintain — every consumer always runs whatever `queryfix-ref` resolves
to (a tag once one exists, `main` today).

## What a review covers: the lines the PR changed

QueryFix reports and enforces only findings on code the pull request **wrote**.
A pilot on a real repository showed why: a PR that changed 225 lines was blocked by
16 findings, none of them on a line it touched, all of them pre-existing code in files
it happened to edit. Holding every edit to the file's history is how a gate gets
switched off.

- A query finding counts when any line of that statement was added or rewritten.
- An N+1 finding counts when the PR wrote the **loop line or the call line**. An
  unrelated edit somewhere inside a long loop body does not make the PR the author of
  the loop.
- Unchanged context lines around an edit (GitHub pads every hunk with three) do not
  count as changed.
- The review says how many findings it left out ("2 more findings in code this change
  did not touch are not shown"), so a quiet review is never mistaken for a clean file.
- Set `QUERYFIX_SCOPE=all` to report and enforce on every finding in a changed file
  instead (useful for a one-off audit of a legacy module).

## SQL dialect (MySQL, Oracle, SQL Server, Postgres)

SQL is parsed with `sqlglot`, and the dialect decides whether a statement parses at
all (MySQL backticks are a syntax error to the Postgres parser). QueryFix works
the dialect out from the repository itself: the JDBC URL in `application.properties`
or `application.yml` at the PR head (`jdbc:mysql:` and `jdbc:mariadb:` read as MySQL,
`jdbc:postgresql:` as Postgres, `jdbc:oracle:` as Oracle, `jdbc:sqlserver:` as SQL
Server). Nothing is guessed from the SQL text.

It looks in the conventional root locations first, then in the module of every file
the PR changed: `<module>/src/main/resources/application.*` and `bootstrap.*`. That
matters for multi-module repositories (and Spring Cloud services that keep the
datasource in `bootstrap.properties`), where the root has no config at all.

- A URL that is a placeholder (`${DB_URL}`), commented out, or an in-memory H2 test
  database names nothing, and the default (Postgres) is kept.
- Set `QUERYFIX_SQL_DIALECT` (a `sqlglot` dialect name such as `mysql`) to
  override detection. A name `sqlglot` does not know is reported in the review as a
  degraded `sql_dialect` stage instead of being silently ignored.
- A file that still cannot be parsed (stored procedures with `DELIMITER`,
  `<tenant>` placeholders in schema names, `mysqldump` directives) is named in the
  review as `extract:<path>` and the rest of the run carries on.

## Tuning it per repository: `.queryfix.yml`

A repository can commit a `.queryfix.yml` at its root to tune QueryFix without
touching the shared workflow. It is read from the pull request's head commit over
the API, like everything else, so no checkout is needed. Every key is optional:

```yaml
block-severities: [critical]   # which severities make the review REQUEST_CHANGES
warn-rules: [no-limit]         # always shown, never blocking
block-rules: [missing-where]   # always blocking, whatever the severity
disable-rules: [select-star]   # static rules that do not run at all
ignore-paths:                  # files QueryFix does not read
  - "src/test/**"
  - "**/legacy/**"
```

- `block-severities: []` makes one repository advisory-only (only when the
  organisation has not set `block-severities` or `advisory` itself). To make
  *every* repository advisory, pass the workflow's `advisory: true`, which outranks
  both the other blocking settings and this file. A blank `block-severities` does
  **not** mean advisory: blank falls back to the default (`CRITICAL,HIGH` block). It is
  the recommended way to start: findings are posted, nothing blocks, and the team can judge the
  signal before turning the gate on.
- **The organisation outranks the repository.** If `QUERYFIX_BLOCK_SEVERITIES` is
  set (the `block-severities` workflow input), the file cannot loosen it. Rule lists
  are additive on top of whatever the org set, and a `warn-rules` entry always beats
  a `block-rules` entry.
- **Strict on purpose.** An unknown key or invalid value is reported in the review as
  a degraded `repo_config` stage and QueryFix falls back to the defaults, so a typo
  never silently turns a gate off.
- `**` crosses directories, `*` and `?` do not.
- `disable-rules` applies to the static rules (`missing-where`, `no-limit`,
  `unindexed-filter`, `select-star`, `non-sargable`); to quiet N+1 findings use
  `warn-rules` instead.

## Verifying it worked, and the most common first-run failure

Open (or push to) a pull request in the consumer repository, then check two
places: the **Actions** tab for a "QueryFix" run, and the pull request's
**Reviews** for one from the token's identity (`github-actions[bot]` for the
default token) whose body starts with a hidden `<!-- queryfix:review -->`
marker. A second push updating that same review in place, rather than adding
a new one, confirms the idempotency is working too.

**The failure you are most likely to hit on a first setup:** the job runs,
QueryFix produces real findings, and the very last step fails with
`403: Resource not accessible by personal access token` (or similarly,
"Resource not accessible by integration"). This means QueryFix analyzed the
pull request correctly but could not submit its review — the automatic token
didn't have write access to pull requests, even though the workflow's own
`permissions:` block asks for it. The workflow's `permissions:` block is a
ceiling, not a guarantee: a repository (or organization) can restrict the
default `GITHUB_TOKEN` below what any individual workflow requests. Check
**Settings → Actions → General → Workflow permissions** and make sure it is
not locked to "Read repository contents permission" — either set it to "Read
and write permissions", or, if your organization enforces the read-only
default, add an explicit exception for this workflow. (The same failure shows
up if you run `queryfix review --post-comment` locally with a personal
access token that lacks "Pull requests: Read and write" for the target
repository — same fix, different settings page: the token's own repository
permissions, not the repo's Actions settings.)

## Making a blocking finding actually block the merge

Adding the workflow above is not, by itself, enough to stop a bad pull
request from merging. QueryFix submits a `REQUEST_CHANGES` review when a
finding meets the blocking threshold (`CRITICAL`/`HIGH` by default) and exits
its CI job with a non-zero status — but it never touches branch protection,
never merges, and never closes anything (CLAUDE.md's own invariant). Whether
either of those signals actually stops the merge button is entirely a
property of the consuming repository's branch protection settings, not
something QueryFix controls. On a repo with no branch protection at all, a
`REQUEST_CHANGES` review is advisory: anyone with write access can merge past
it.

To make it a real gate, configure branch protection on the target branch
(**Settings → Branches → Branch protection rules**) with one or both of
these — most repositories want both:

1. **Require approvals.** Under "Require a pull request before merging",
   enable "Require approvals" (1 is enough). With this on, an outstanding
   `REQUEST_CHANGES` review — QueryFix's or a human's — genuinely disables
   the merge button until it is resolved: either QueryFix clears it on a
   later run (pushing a fix and re-triggering `synchronize` makes it re-post
   its own review, moving to `COMMENT` if nothing blocking remains), or
   someone with permission dismisses the review manually. Caveat: a repo
   admin can still bypass this unless "Do not allow bypassing the above
   settings" is also checked, and dismissing QueryFix's review is always
   possible for anyone with that permission — this lever is a policy nudge,
   not a hard wall.
2. **Require status checks to pass.** Under "Require status checks to pass
   before merging", mark the QueryFix job (`review`, from the workflow
   above) as required. `queryfix review` exits `2` when the run is
   `BLOCKED`, `1` when QueryFix itself could not reliably analyze the pull
   request, `3` on misconfiguration, and `0` for `PASS`/`DEGRADED` — so a
   blocking finding fails the CI check itself, independent of the review
   mechanism above. This is the harder-to-accidentally-bypass gate, and the
   one most teams should treat as the actual enforcement point.

Without at least one of these configured, QueryFix is purely advisory:
useful information on the pull request, but nothing stops a merge.

## The one thing that differs: public repos and fork pull requests

A pull request from a **branch of the same repository** works exactly the
same on public and private repos: the workflow's `permissions:` block
(`pull-requests: write`) grants the automatic token enough access to post
QueryFix's review, and that is true regardless of visibility.

A pull request from an **external fork** — common on public repositories,
essentially never seen on private ones — is different. GitHub deliberately
issues a **read-only** `GITHUB_TOKEN` to a workflow triggered by
`pull_request` from a fork, no matter what the `permissions:` block asks for.
QueryFix still runs and still produces a report (fail-soft, per its own
invariants) but the final `--post-comment` step cannot create the review, and
the run fails with a permissions error from the GitHub API.

If a public repository needs QueryFix to review fork PRs, switch the
trigger from `pull_request` to `pull_request_target`:

```yaml
on:
  pull_request_target:
    types: [opened, synchronize]
```

This is safe specifically because QueryFix never checks out or executes the
fork's code — every read goes through the GitHub API at the PR's head SHA,
and the only thing installed is QueryFix itself from its own trusted repo.
`pull_request_target` is dangerous when a workflow checks out the fork's ref
and then runs something from it (a build step, a test suite); QueryFix does
neither, so that risk doesn't apply here. Restricting `on:` this way is
still worth doing deliberately, not by default — treat it as a per-repository
decision, not something to blanket-apply.

## Rolling it out across an organization

Moving from "one repo that works" to "every repo" is mostly about where QueryFix
lives and how the call is pinned. In order:

1. **Host QueryFix in the organization**, not a personal account, and give its
   repository **internal** (or private) visibility. A repository that every
   consumer installs from is shared infrastructure and needs an owning team.
2. **Let other repositories call it.** For a private or internal repo, open
   *Settings → Actions → General → Access* on the QueryFix repository and allow
   access from repositories in the organization. Without this, a consumer's run
   fails at "workflow not found".
3. **Point the workflow at the org copy.** Consumers call
   `ORG/QueryFix/.github/workflows/queryfix-review.yml@<tag>` and pass
   `queryfix-repo: ORG/QueryFix`. If that repo is private or internal, also pass
   the optional `queryfix-token` secret (a read-only token for that one repo),
   because the install step is otherwise anonymous.
4. **Pin a release tag**, never `@main`. Every consumer installs the ref on every
   run, so a moving ref is a moving org-wide gate. Cut `v0.1.0`, and roll consumers
   forward by changing the tag.
5. **Decide the LLM question centrally.** Pass `disable-llm: true` until an LLM
   provider has been approved for source code. It wins over a supplied
   `groq-api-key`: no provider is constructed and nothing but GitHub traffic leaves
   the runner. The review is then fully deterministic.
6. **Roll out advisory first.** Pass `advisory: true` (the workflow input; it sets
   `QUERYFIX_ADVISORY`) so findings are posted but nothing
   blocks. Review the false-positive rate on real PRs, then turn blocking on and
   mark the check required.
7. **Avoid one PR per repository** where your GitHub plan allows it: an organization
   ruleset that requires a workflow can enforce a thin caller from a central repo
   (check availability on your plan). Otherwise a one-time script that opens the
   same small caller-file PR in each repo does the job.

A minimal org-pinned consumer:

```yaml
name: QueryFix
on:
  pull_request:
    types: [opened, synchronize]

concurrency:
  group: queryfix-${{ github.event.pull_request.number }}
  cancel-in-progress: true

jobs:
  review:
    uses: ORG/QueryFix/.github/workflows/queryfix-review.yml@v0.1.0
    permissions:
      contents: read
      pull-requests: write
    with:
      queryfix-repo: ORG/QueryFix
      queryfix-ref: v0.1.0
      disable-llm: true
      advisory: true              # report everything, block nothing, while piloting
    secrets:
      queryfix-token: ${{ secrets.QUERYFIX_INSTALL_TOKEN }}   # only if QueryFix is private/internal
```

## What this does not (yet) give you

A GitHub App installation model — install once at the organization level,
every current and future repo covered automatically, no per-repo workflow
file — does not exist yet; today's model is one small workflow file per
repository, which is deliberately the simpler thing to ship first. If
QueryFix ends up covering many repositories across an org, that is the
natural next step (`api/routes/webhooks.py` in the architecture already
reserves the shape for it), but it is not required for "any repo, public or
private" today — the workflow-call model above already covers that.
