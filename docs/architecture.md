# QueryFix Architecture

How the pipeline is put together, what each stage promises the next one, and where
to plug in without editing anything that already exists.

This describes the intended shape of all eight stages. Stages 4–8 are not
implemented; their contracts are written here because the contract is what the
implemented stages are built against, not something to be decided later. Where a
stage does not exist yet, this document says so.

For *what is built*, see [STATUS.md](../STATUS.md). For *conventions and product
invariants*, see [CLAUDE.md](../CLAUDE.md).

---

## 1. The pipeline

A linear chain. Each stage takes the previous stage's typed output and produces
its own. No stage reaches into GitHub, Docker, or the database except the ones
that own those concerns.

```
Extraction → Normalization → Analysis → Execution Plan → Optimization → Explanation → Rendering
```

Mapped onto the modules that implement it:

| # | Stage | Module | Input | Output | State |
| --- | --- | --- | --- | --- | --- |
| 1 | Ingest | `pipeline/ingest.py` | PR event | `RunContext` + diff | Not started |
| 2 | Extraction | `pipeline/extract/` | `SourceFile` | `list[ExtractedQuery]` | SQL done; Java partial |
| 3 | Static analysis | `pipeline/static_rules/` | `list[ExtractedQuery]` | `list[Finding]` | Done (5 of 9 rules) |
| 4 | Provision | `db/provision.py` | schema snapshot | live reference DB | Not started |
| 5 | Execution plan | `pipeline/explain.py` | `ExtractedQuery` + DB | plan JSON → `Finding` | Not started |
| 6 | Optimization | `pipeline/hypopg.py` | plan + predicates | `Suggestion` with cost delta | Not started |
| 7 | Explanation | `pipeline/nplusone.py` | query set + p6spy log | `Finding` | Not started |
| 8 | Rendering | `pipeline/report.py` | `Report` | Markdown | Not started |

**Normalization** is not a module today. `ExtractedQuery.normalized` is populated
by the SQL extractor, and the rule engine re-parses `text` per query. When JPQL
needs to become SQL — it is not SQL, and the rule engine currently parses it as
though it were — that conversion is the Normalization stage, and it belongs
between 2 and 3 rather than inside either. See "Deferred" below.

### Orchestration

`pipeline/runner.py::AnalysisRunner` owns stage order, the fail-soft boundaries,
and the run's log record. It is not a stage. It exists so the HTTP route, the
eventual CLI, and the eventual webhook handler do not each grow their own copy of
the ordering.

It holds no per-run state. `POST /analyze` is a plain `def`, so FastAPI serves it
from a threadpool and one shared instance handles concurrent runs; everything
about a run lives in the `Report` built per call.

### Fail-soft

CLAUDE.md invariant 5, implemented at three widths:

| Boundary | A failure costs | Marker |
| --- | --- | --- |
| One rule | that rule's findings for that query | none (logged) |
| One source | that file's queries | `extract:<path>` |
| One stage | that stage's output | `<stage>` |

A degraded run is HTTP 200 with `status: "degraded"` and the loss named in
`report.degraded_stages`. It is never an exception and never a silently empty
report — answering "no problems found" to input that was never read is the one
failure mode a review bot cannot have.

---

## 2. Stage contracts

The models in `queryfix/models/` are the contracts. Stages exchange these, never
bare dicts or tuples.

All of them derive from `models.base.Contract`, which is frozen. A stage cannot
edit its input: a rule cannot rewrite the query text a later stage reports, and an
enrichment stage cannot re-anchor a finding onto a different file. Deriving a
changed value is `model_copy(update=...)`, which produces a new object rather than
mutating a shared one.

The freeze is shallow — Pydantic blocks attribute assignment, not
`findings.append(...)`. It closes the mistake that actually happens without
forcing tuples into the JSON contract.

```
SourceFile ──extract──▶ ExtractedQuery ──analyze──▶ Finding ──render──▶ Markdown
    │                        │                         │
  path                      id                      rule_id, severity
  content                   kind                    explanation, impact
    │                       text (verbatim)          provenance
  SqlSource                 normalized               suggestions
    └─ dialect              dialect                  evidence
                            provenance               confidence
                            parse_error
```

Two properties worth stating because code depends on them:

- **`ExtractedQuery.text` is the query as written.** It is sliced from the source,
  never re-rendered from an AST. A report that quotes SQL the author never wrote
  is hard to trust — `Customer c` would become `Customer AS c`, `:tier` would
  become `%(tier)s`. The re-rendered form lives in `normalized`.
- **`parse_error` means unanalyzable, not absent.** A candidate that could not be
  parsed is still returned, so the report can name it as a caveat rather than
  dropping it. The rule engine skips it; re-reporting it as a finding would
  double-count one problem.

---

## 3. Extraction

The stage that has to absorb the most future change: today SQL and Java, with
Kotlin, `EntityManager` calls, and a JavaParser sidecar all planned. It is
therefore built so that adding a language changes no existing module.

```
              SourceFile
                  │
                  ▼
        ┌─────────────────────┐
        │  extract_source()   │   dispatcher — knows no language
        └──────────┬──────────┘
                   │ registry.for_path(source.path)
                   ▼
        ┌─────────────────────┐
        │  ExtractorRegistry  │   ".sql" → SqlExtractor
        └──────────┬──────────┘   ".java" → JavaExtractor
                   │
       ┌───────────┴───────────┐
       ▼                       ▼
  SqlExtractor            JavaExtractor
       │                       │
   sqlglot              JavaSource (scanner)
                               │
                    ┌──────────┴──────────┐
                    ▼                     ▼
              @Query patterns      derived-method decoder
                                          │
                                    DerivedQuery (IR)
                                          │
                                     renderer
                   │                       │
                   └───────────┬───────────┘
                               ▼
                     list[ExtractedQuery]
```

### The `Extractor` protocol

```python
class Extractor(Protocol):
    def extract(self, source: SourceFile) -> list[ExtractedQuery]: ...
```

The input is a whole `SourceFile`, not a `(path, content)` pair. A pair cannot
carry per-source options, and the moment one language needs one the choice is to
widen every extractor's signature or to smuggle the option past the contract. The
second is how `SqlSource.dialect` came to be a public field that nothing read — a
caller could declare `dialect: "mysql"` and get their MySQL reported as
unanalyzable Postgres. A model subtype carries the option to the extractor that
understands it and stays invisible to the ones that do not.

Correspondingly, `SourceFile` carries no `dialect`. A dialect is meaningless to a
Java file; putting it on the base would make every language's input model carry
every other language's concerns. `SqlExtractor` narrows with one `isinstance`, in
the one component that knows what a dialect is.

Implementations must be stateless and safe to share — one instance per language,
reused across concurrent runs.

### The registry

`ExtractorRegistry` maps normalized extensions to extractors. Duplicate
extensions are **rejected**, not overwritten: silently replacing an extractor
would make the registered language set depend on import order, and the only
symptom would be findings quietly disappearing.

`extract_source(source, registry=None)` takes an optional registry so callers can
substitute the language set without patching module state — the same seam the rule
engine offers for rules.

### The Java scanner

Java recognition is pattern-based (the JavaParser sidecar is the eventual
replacement). What makes that survivable is that patterns run against
`JavaSource`, which has already classified every byte as code, comment, or
literal, and which does real bracket matching.

Without that boundary, two whole classes of defect are unavoidable:

- `// @Query("SELECT c FROM Ghost c")` matches, and a query the pull request does
  not contain is reported against it.
- `content.find("}")` is not a closing brace. A repository carrying a `default`
  method closes *that* method's body first, so every derived method after it is
  silently invisible.

`JavaSource` exposes two masked views, both the same length as the original — so
an offset found in a mask is an offset into the source, and provenance survives
masking:

| View | Blanked | Used for |
| --- | --- | --- |
| `code` | comments | matching annotations and declarations (the query *is* a literal, so literals stay) |
| `structure` | comments **and** literals | bracket matching (a brace in a string is not structure) |

It answers questions *about* Java text and knows nothing about queries, so
swapping it for the sidecar replaces one collaborator rather than untangling
extraction from lexing.

### Derived methods

Three responsibilities, three functions, because they change for different
reasons:

| Function | Job | Knows about |
| --- | --- | --- |
| `parse_derived_query(name)` | decode a name → `DerivedQuery` | Spring Data's grammar |
| `render_derived_query(ir, table)` | say what it means in SQL | SQL text |
| `parse_derived_method(...)` | anchor it to a file | provenance |

`DerivedQuery` is the seam that matters. Method-name grammars are not unique to
Spring Data — Micronaut Data and Spring Data JDBC share the shape — so a second
framework's decoder targets the same IR and reuses the renderer unchanged. It is
also what the planned derived-method fan-out rule needs: "this method returns a
collection and is called per row" is a question about semantics, and reading it
back out of rendered SQL would mean parsing what we just printed.

The decoder is narrow and says so by returning `None`. A name it does not fully
understand produces no query rather than a guessed one — see CLAUDE.md's caution
that `findByCustomerId` on a `@ManyToOne` compiles to a *join*, not a bare
`orders.customer_id = ?`. A rule written against the assumed SQL would reason
about a query that is never issued.

### Query identity

| Shape | Used by | Why |
| --- | --- | --- |
| `<path>:<ordinal>` | SQL statements, `@Query` annotations | a statement has only a position |
| `<path>:<symbol>` | derived methods | a derived method *is* its name, and its ID does not move when a query is added above it |

Both are minted in `extract/base.py`. Three extractors produce IDs and the format
is a contract — findings reference a query by ID, so two extractors disagreeing
about the shape means a finding that anchors to nothing.

### Adding a language

```python
# queryfix/pipeline/extract/kotlin.py
class KotlinExtractor:
    def extract(self, source: SourceFile) -> list[ExtractedQuery]: ...


# queryfix/pipeline/extract/dispatcher.py — the only line that changes
_EXTRACTORS.register(".kt", KotlinExtractor())
```

or, from outside the package, `register_extractor(".kt", KotlinExtractor())`.

Nothing downstream changes. `test_a_new_language_needs_no_change_to_the_dispatcher`
pins this.

---

## 4. Static analysis

`RuleEngine` parses each query once, hands the AST to every registered rule, and
ranks the findings. It owns two concerns the rules must not each re-solve:
parsing (one dialect, one place, one way of recording failure) and failure
isolation (a rule that raises loses its own check and nothing else).

Rules receive a `RuleContext` — a parsed sqlglot AST plus provenance and schema —
never a raw string. `RuleContext` is a frozen dataclass rather than a Pydantic
model because it carries a live `Expression`, which is a mutable tree with no
serialization contract; stage *outputs* are Pydantic models, this is an
intra-stage argument holder.

| Concern | Lives in |
| --- | --- |
| `Rule` protocol, registry, `RuleContext` | `base.py` |
| Parsing, dispatch, ranking, isolation | `engine.py` |
| Shared AST readings (`clause`, `table_aliases`, `resolve_table`) | `ast_helpers.py` |
| Schema lookups | `schema.py` |
| One rule per smell | `rules/<smell>.py` |

`ast_helpers.py` exists because two rules previously carried their own copies of
alias resolution, written slightly differently. A rule asking "which table is this
column on?" and getting a different answer depending on which file it lives in is
a bug waiting for the third copy. They are re-exported from `base.py`, so no rule
has to care which of the two files a helper is defined in.

`register()` rejects a duplicate `rule_id`, matching `ExtractorRegistry` —
registration is an import side effect, so the failure being closed is a rule
reachable by two import paths, whose only symptom would be every one of its
findings appearing twice in the PR comment.

**Schema-dependent rules are silent by default.** `UNKNOWN_SCHEMA` answers "I do
not know" to every lookup, and a rule that depends on it must then report nothing.
"No index on that column" is unfalsifiable from query text alone; a rule that
guessed would fire on every predicate in the diff.

### Adding a rule

1. One file under `static_rules/rules/`, named after the smell, exposing a class
   with `rule_id` and `check(context)`, calling `register()` at module scope.
2. Import it in `static_rules/__init__.py` — registration is an import side
   effect, so an unimported rule silently never runs.
3. Add its ID to the registry assertion in `tests/unit/test_placeholders.py`.
4. Ship at least one query it must flag and one similar query it must not.

---

## 5. Extension points

| Point | Interface | Add by | Modifies |
| --- | --- | --- | --- |
| Source language | `Extractor` | `register_extractor(ext, impl)` | nothing |
| Static rule | `Rule` | `register(rule)` | one import line |
| Schema source | `SchemaProvider` | pass to `RuleEngine(schema=...)` | nothing |
| Rule set | `list[Rule]` | `RuleEngine(rules=[...])` | nothing |
| Pipeline | `AnalysisRunner` | `Depends(get_analysis_runner)` override | nothing |

Each is a Protocol satisfied structurally — an implementation inherits nothing and
imports nothing of ours beyond the models it returns.

---

## 6. Deferred, deliberately

Recorded here so the next person does not have to rediscover the reasoning.

**JavaParser sidecar.** Extraction still matches patterns. The scanner removes the
two defect classes that made that dangerous, and the `Extractor` boundary means
the sidecar swaps in behind one interface. Doing it now would be a JVM dependency
and a versioned JSON contract in exchange for annotation shapes nobody has asked
for yet.

**Rendering derived SQL through a sqlglot AST.** Every identifier reaching the
renderer has passed a `[A-Za-z0-9]` character class, so there is no quoting
decision to get wrong and no untrusted text. What the formatting buys is a stable,
readable rendering quoted back to a reviewer; a sqlglot round-trip would reformat
it for no gain the reviewer can see. The moment the decoder accepts something with
real syntax — ordering, limits, joins for nested properties — that reasoning
expires, and the IR boundary is what makes it a change to one function.

**A distinct Normalization stage.** Worth building when JPQL must become SQL. The
rule engine currently parses JPQL as SQL, so entity names are read as tables; that
is a real limitation, but fixing it means entity-to-table mapping, which needs
metadata extraction that does not exist. Inserting an empty stage now would be
ceremony.

**Run-unique query IDs.** IDs are unique within a file, not within a run: the same
path submitted twice yields the same IDs. Pinned deliberately by
`test_the_same_source_supplied_twice_is_analyzed_twice`. It breaks when a consumer
keys on ID — the Markdown renderer is the likely first victim — and that consumer
is the right one to state the requirement.

---

## 6a. Liquibase schema discovery

`unindexed-filter` and the implicit-cast half of `non-sargable` need real
schema data, and no longer require `LIQUIBASE_CHANGELOG_PATH` to be configured
by hand: `queryfix/db/discovery.py` finds a repository's own
`db.liquibase.change-log` declaration in `application.properties`/`.yml` and
hands the resolved root to the existing loader in `queryfix/db/liquibase.py`
— one module answers "where is the root changelog?", the other (unchanged)
answers "what schema does it produce?". See
[docs/liquibase-schema-discovery.md](liquibase-schema-discovery.md) for the
full behavior: precedence against the explicit environment variable, classpath
resolution, PR-head reconstruction, and fallback/ambiguity handling.

---

## 7. Invariants

Product-level, from CLAUDE.md. Not weakened for convenience.

1. Never connect to a developer or production database.
2. Every statement runs inside `BEGIN` … `ROLLBACK`.
3. Read-only with respect to the PR.
4. One comment per PR, updated in place.
5. Fail soft.

Invariants 1 and 2 are currently prose: `db/session.py` and `db/provision.py` are
stubs, so there is no code path that could violate them and none that enforces
them. They need tests written with the first line of body, not after.
