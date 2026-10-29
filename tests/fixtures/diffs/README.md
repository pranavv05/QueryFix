# Recorded pull-request fixture

A **real** GitHub pull request, recorded verbatim. Stage 1's whole test suite runs
from these files with no network, no credentials, and no Docker.

It is recorded rather than hand-written on purpose. A hand-written payload agrees
with whatever the parser expects, which is precisely the property that makes it
useless for finding out whether the parser is wrong. Everything awkward in here —
that GitHub spells a deletion `removed`, that a rename arrives as one entry with
`previous_filename` rather than a delete plus an add, that a patch omits the
`diff --git` preamble, that hunk headers drop a count when it is `1` — is a fact
about GitHub that this fixture carries and no invented one would.

## Source

<https://github.com/pranavv05/QueryFix/pull/4> — a **draft** pull request
opened solely to be recorded, and **not for merge**. It touches only
`queryfix-sandbox/`. Its branch, `queryfix/diff-fixture`, is built through the
Git Data API so recording it never disturbs a working tree.

| File | Content |
| --- | --- |
| `sandbox-pull-request.json` | `GET /repos/{repo}/pulls/4` |
| `sandbox-pull-request-files.json` | `GET /repos/{repo}/pulls/4/files` |
| `sandbox-pull-request.diff` | the same PR as one unified diff, for reading |
| `sandbox-head/**` | each surviving file's text at the head SHA |

`sandbox-head/` exists because a source carries the **whole head file**, not a
reconstruction of its patch — see the module docstring of
`queryfix/pipeline/diff.py` for why, and for the case in this very fixture that
proves it.

## What each changed file is for

The PR was composed so that every case stage 1 has to handle appears exactly once.
`test_the_recording_still_carries_every_case_this_stage_must_handle` fails if a
re-recording loses one.

| Path (under `queryfix-sandbox/`) | Status | The case it covers |
| --- | --- | --- |
| `…/db/migration/V2__reporting_indexes.sql` | added | A whole new file: one hunk, `@@ -0,0 +1,10 @@`, base side at zero |
| `…/db/migration/V1__create_tables.sql` | modified | A single hunk starting at head line 57 — a finding here anchors nowhere near the top of the file |
| `…/repository/OrderRepository.java` | modified | **Two** hunks, far apart. The second adds a `@Query` at head line 55, and adds a derived method whose enclosing `interface` declaration is thirty lines above the hunk |
| `…/repository/OrderLineRepository.java` | renamed | Renamed *and* modified, so findings must anchor to the new path while the old one appears only as `previous_filename` |
| `…/domain/OrderStatus.java` | removed | A deleted `.java` file — an extractor claims the extension, so only the status can be what excludes it |
| `README.md` | modified | A language no extractor claims: skipped, and never fetched |

## Re-recording it

Only needed if the cases above change. Note that the recorded SHAs move, so
re-record all four artifacts together — a `files.json` from one commit beside a
`sandbox-head/` from another is a fixture that describes a tree that never
existed.

The recorder builds the branch through the Git Data API, reads its GitHub token
from the git credential helper, and never prints it. It is not checked in: it
writes to a real repository, which is not something a test run should be one
`pytest` away from doing.
