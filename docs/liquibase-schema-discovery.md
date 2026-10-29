# Liquibase schema discovery

How QueryFix finds a repository's schema without being told where it is, and
what happens when it can't.

For the loader that actually reads Liquibase XML once a changelog root is
known, see `queryfix/db/liquibase.py`'s module docstring — this document is
about *finding that root*, not about interpreting what it contains.

---

## 1. What this is

Every Java/Spring Boot repository declares its schema's entry point the
same way, in `application.properties` (or the YAML equivalent):

```properties
# application.properties
db.liquibase.change-log=classpath:/db/changelog/master.xml
```

Before this feature, QueryFix needed that path handed to it explicitly, via
`LIQUIBASE_CHANGELOG_PATH`. Now, `queryfix review --repo OWNER/REPO --pr
NUMBER` finds it on its own for any repository that follows this convention —
no configuration required. `unindexed-filter` and the implicit-cast half of
`non-sargable` (CLAUDE.md's schema-dependent static rules) go from silent by
default to actually answering "is this column indexed?" for a standard
repository with zero setup.

## 2. Supported Spring Boot configuration

Discovery reads a fixed, small set of conventional locations — never a
repository-wide scan — in this precedence order:

```
src/main/resources/application.properties
src/main/resources/application.yml
src/main/resources/application.yaml
application.properties
application.yml
application.yaml
```

Every candidate is checked (not just the first that exists), so that two base
configuration files disagreeing about the changelog can be caught rather than
one silently shadowing the other — see [§8](#8-multi-module-and-ambiguity)
below.

**Profile-specific files are never read.** `application-dev.properties`,
`application-prod.yml`, and the like are not among the candidates. Which
profile is active is a runtime decision this module has no basis for guessing,
and guessing wrong would misjudge every schema-dependent finding for the rest
of the run — worse than finding nothing.

**Multi-module repositories** are supported to the extent their configuration
lives at one of the standard locations under a module directory — pass
`candidate_paths` to `discover_liquibase_changelog` for a non-standard layout,
or set `LIQUIBASE_CHANGELOG_PATH` explicitly (see [§5](#5-explicit-override)).
A repository whose configuration lives somewhere else entirely is not
discovered automatically and falls back exactly as an unconfigured repository
always has.

## 3. `db.liquibase.change-log`

This is the *only* key discovery looks for. It is deliberately not
`spring.liquibase.change-log` — Spring Boot's own Liquibase autoconfiguration
key, and a related but different setting. Every repository audited for
this feature sets the narrower `db.liquibase.change-log` key, read by
application code rather than by Spring's autoconfiguration, so that is the one
discovery looks for.

Both `.properties` and YAML forms are supported:

```properties
db.liquibase.change-log=classpath:/db/changelog/master.xml
```

```yaml
db:
  liquibase:
    change-log: classpath:/db/changelog/master.xml
```

```yaml
# the equivalent compact dotted-key form
db.liquibase.change-log: classpath:/db/changelog/master.xml
```

Whitespace around `=`/`:`, `#`/`!` comments, blank lines, and a value wrapped
in quotes are all tolerated. Neither parser is general-purpose — see
`queryfix/db/discovery.py`'s module docstring for exactly what each does and
does not handle, and why (in short: nothing beyond this has ever been observed
in an audited configuration, and PyYAML is not a dependency this
project has declared for itself).

## 4. Classpath resolution

A declared value can take any of these forms, and all resolve to the same
repository-relative path:

| Declared value | Resolves to (when found under `src/main/resources/application.properties`) |
| --- | --- |
| `classpath:/db/changelog/master.xml` | `src/main/resources/db/changelog/master.xml` |
| `db/changelog/master.xml` (no prefix — Liquibase treats this as classpath-relative too) | `src/main/resources/db/changelog/master.xml` |
| `src/main/resources/db/changelog/master.xml` (already fully repository-relative) | used as-is |

The resource root (`src/main/resources` above) is *derived from where the
declaring configuration file was found* — not hard-coded — so a module whose
configuration lives at `service-a/src/main/resources/application.properties`
resolves its classpath references under `service-a/src/main/resources/` too.
Backslashes are normalized to forward slashes and a leading slash is stripped
before any of this, so Windows-style paths and a stray absolute-looking `/`
are both handled the same way.

## 5. Explicit override

`LIQUIBASE_CHANGELOG_PATH` continues to work exactly as before, and always
takes precedence over discovery:

```
1. LIQUIBASE_CHANGELOG_PATH configured  → use it. Discovery is never even
                                           attempted — application.properties
                                           is not read.
2. Not configured, discovery succeeds   → use the discovered changelog.
3. Not configured, discovery fails      → the existing fallback (§7).
```

An operator who has set the variable gets exactly what they configured, never
silently second-guessed by discovery — even if discovery would have found a
different (or additional) changelog.

## 6. PR-head schema reconstruction

Unchanged for the explicit path: a local-disk schema is loaded first, and only
rebuilt from the pull request's head commit (over the GitHub API) if the pull
request's changed files actually touch the changelog tree
(`resolve_pr_changelog_touch`) — see `queryfix/db/liquibase.py`.

The discovered path has no local-disk baseline to speak of — there is no
guaranteed local checkout of an arbitrary `OWNER/REPO`, so every read,
including the initial discovery reads, goes over the GitHub API pinned to the
pull request's head commit (`read_file_at_ref`). This makes the "did the pull
request touch Liquibase" question moot for the discovered path: a query-only
pull request and a schema-changing one are answered by the same one remote
walk of the changelog tree, and both correctly see the schema as it will
actually be once the pull request merges. This is a core acceptance
criterion — a pull request that only changes `ReportingService.java` or
`OrderRepository.java` still gets full schema-aware analysis, with no
Liquibase file anywhere in its diff.

## 7. Fallback behavior

None of the following ever fail the review — every one degrades to
`UNKNOWN_SCHEMA`, the same silent stub every schema-dependent rule has always
had without a configured changelog:

| Situation | Result |
| --- | --- |
| No candidate configuration file exists | `UNKNOWN_SCHEMA`, logged at debug level |
| A configuration file exists but never mentions the key | `UNKNOWN_SCHEMA`, logged at debug level |
| The key is present but its value is blank | `UNKNOWN_SCHEMA`, logged at warning level |
| Discovery succeeds but the named changelog cannot actually be read/parsed | `UNKNOWN_SCHEMA`, logged at error level (`could not be loaded`) |
| A GitHub API failure while reading a candidate file | that one candidate is treated as absent; the others are still checked |
| Two or more configuration files disagree (§8) | `UNKNOWN_SCHEMA`, logged at warning level, naming the conflicting sources |

A discovery failure never turns an otherwise-analyzable repository's *other*
findings into nothing — only the schema-dependent rules go silent, exactly as
they always have when unconfigured.

## 8. Multi-module and ambiguity

Every candidate configuration file is checked, not just the first that
resolves. If two or more declare *different* `db.liquibase.change-log`
values, discovery refuses to guess:

```
2 application configuration files declare different db.liquibase.change-log
values (src/main/resources/application.properties,
src/main/resources/application.yml); refusing to guess which applies
```

— and the run falls back to `UNKNOWN_SCHEMA`, logged as a warning naming both
sources, with neither candidate changelog tree ever fetched. Two files
declaring the *same* value are not a conflict (they agree on the effective
schema) and discovery proceeds normally.

QueryFix's architecture assumes one effective schema per review; this
preserves that invariant rather than attempting to analyze several schemas at
once. Set `LIQUIBASE_CHANGELOG_PATH` explicitly to resolve a genuine conflict.

---

## Example

```properties
# application.properties
db.liquibase.change-log=classpath:/db/changelog/master.xml
```

```bash
queryfix review --repo acme/orders-service --pr 128
```

No `LIQUIBASE_CHANGELOG_PATH` needed. QueryFix discovers the changelog from
the repository's own `application.properties`, loads the complete schema via
the existing Liquibase loader (recursively resolving every `<include>`), and
`unindexed-filter` now has real indexed-column data to check the pull
request's queries against — even when the pull request touches only a Java
repository or service file and no Liquibase file appears anywhere in its diff.
