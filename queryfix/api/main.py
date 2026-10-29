"""QueryFix FastAPI service.

Run locally::

    uvicorn queryfix.api.main:app --reload

Endpoints:

- ``GET  /health``  — liveness probe.
- ``POST /analyze`` — run the review pipeline over the SQL in a pull request.

The route validates the request, hands it to
:class:`~queryfix.pipeline.runner.AnalysisRunner`, and shapes the response. The
ordering of the stages and their failure handling belong to the runner, so this
module holds no analysis logic.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, status
from github import Github
from pydantic import BaseModel, Field

from queryfix.api.deps import get_analysis_runner, get_github_client
from queryfix.models import Report, SourceFile, SqlSource
from queryfix.pipeline.runner import AnalysisRunner

#: Provenance path recorded for SQL passed inline as ``sql``, which has no file of
#: its own. Findings from it still anchor to a line — the line within the snippet.
INLINE_SQL_PATH = "inline.sql"

app = FastAPI(
    title="QueryFix",
    version="0.1.0",
    description="Reviews SQL, JPQL/HQL, JPA native queries, and Spring Data "
    "derived methods on a pull request for performance problems.",
)


class AnalyzeRequest(BaseModel):
    """Input to a review run.

    Supply the sources directly — ``sql`` for a single snippet, ``sql_files`` for
    several named SQL sources, ``sources`` for anything the extract stage can
    route by extension — to analyze them without calling GitHub. This is the path
    local runs and tests use.
    """

    repo: str = Field(description='Owner/name, e.g. "acme/billing-service".')
    pr_number: int = Field(gt=0)
    sql: str | None = Field(
        default=None,
        description="SQL to analyze, one or more statements. Reported against "
        f"`{INLINE_SQL_PATH}`. Analyzed before `sql_files`.",
    )
    sql_files: list[SqlSource] = Field(
        default_factory=list,
        description="Named SQL sources. Each is extracted independently, so one "
        "file that cannot be parsed does not cost the others.",
    )
    sources: list[SourceFile] = Field(
        default_factory=list,
        description="Named sources in any language the extract stage supports, "
        "routed by file extension (`.sql`, `.java`). Use `sql_files` instead when "
        "a source needs a non-default SQL dialect. Analyzed after `sql_files`.",
    )
    diff: str | None = Field(
        default=None,
        description="Unified diff. Not yet analyzable — extracting queries from a "
        "diff is stage 2's dispatch, which is unimplemented.",
    )
    post_comment: bool = Field(
        default=False,
        description="Whether to upsert the tagged report comment on the PR. "
        "QueryFix searches for its own marker comment, edits it in place if found, "
        "or creates one. Requires a valid GITHUB_TOKEN.",
    )

    def sql_sources(self) -> list[SqlSource]:
        """Every SQL source this request carries, in the order they are analyzed."""
        if self.sql is None:
            return self.sql_files
        return [SqlSource(path=INLINE_SQL_PATH, content=self.sql), *self.sql_files]

    def analysis_sources(self) -> list[SourceFile]:
        """Every source this request carries, in the order they are analyzed.

        The extract stage routes by extension and returns ``ExtractedQuery``
        whatever the language, so the route has no reason to know which of these
        are SQL. ``sql_files`` stays a separate field only because it is typed as
        :class:`~queryfix.models.query.SqlSource` and can therefore carry a
        dialect; both lists reach the same stage by the same path.
        """
        return [*self.sql_sources(), *self.sources]


class AnalyzeResponse(BaseModel):
    """Result of a review run."""

    run_id: str
    status: Literal["completed", "degraded"] = Field(
        description='"degraded" when a stage failed soft; the report is still valid, '
        "but it covers less than was submitted. See `report.degraded_stages`.",
    )
    report: Report
    comment_id: int | None = Field(
        default=None,
        description="Set when the report was posted to the PR.",
    )

    @classmethod
    def from_report(cls, report: Report) -> AnalyzeResponse:
        """Wrap a finished report, deriving the run's status from what degraded."""
        return cls(
            run_id=report.context.run_id,
            status="degraded" if report.degraded_stages else "completed",
            report=report,
            comment_id=report.comment_id,
        )


class HealthResponse(BaseModel):
    status: Literal["ok"]
    version: str


@app.get("/health", response_model=HealthResponse, tags=["ops"])
def health() -> HealthResponse:
    """Liveness probe."""
    return HealthResponse(status="ok", version=app.version)


@app.post("/analyze", response_model=AnalyzeResponse, tags=["analysis"])
def analyze(
    request: AnalyzeRequest,
    runner: Annotated[AnalysisRunner, Depends(get_analysis_runner)],
    client: Annotated[Github | None, Depends(get_github_client)] = None,
) -> AnalyzeResponse:
    """Review one pull request's SQL.

    Runs the two implemented stages — extract, then the static rules — and returns
    the ranked findings. Malformed SQL degrades the run rather than failing it: the
    offending source is reported as unanalyzable in ``report.queries`` and named in
    ``report.degraded_stages``, and every other source is still analyzed.

    ``diff`` is rejected with 501 rather than ignored: it depends on stages that do
    not exist yet, and answering "no problems found" to a request whose input was
    never read is the one failure mode a review bot cannot have.

    ``post_comment`` posts a single, idempotent Markdown comment on the PR.  If the
    comment upsert fails, the run degrades but the report is still returned.
    """
    if request.diff is not None and request.diff.strip():
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Analyzing a diff is not implemented yet. Supply the SQL directly "
            "via `sql` or `sql_files`.",
        )

    report = runner.run(
        repo=request.repo,
        pr_number=request.pr_number,
        sources=request.analysis_sources(),
        post_comment=request.post_comment,
        client=client,
    )

    return AnalyzeResponse.from_report(report)
