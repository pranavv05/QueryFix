"""Tests for the properties every stage contract has by virtue of its base.

The pipeline is a chain of stages over a shared set of queries and findings. A
stage that could edit its input would produce a report describing something no
file ever contained, with nothing anywhere to say it happened. These tests pin
the guarantee that stops it.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ValidationError

from queryfix.models import (
    Contract,
    Evidence,
    ExtractedQuery,
    Finding,
    Provenance,
    QueryKind,
    Report,
    RunContext,
    Severity,
    SourceFile,
    SqlSource,
    Suggestion,
)

CONTRACTS: list[type[BaseModel]] = [
    Evidence,
    ExtractedQuery,
    Finding,
    Provenance,
    Report,
    RunContext,
    SourceFile,
    SqlSource,
    Suggestion,
]


def query() -> ExtractedQuery:
    return ExtractedQuery(
        id="a.sql:1",
        kind=QueryKind.RAW_SQL,
        text="SELECT * FROM orders",
        provenance=Provenance(file="a.sql", line=1),
    )


@pytest.mark.parametrize("model", CONTRACTS, ids=[model.__name__ for model in CONTRACTS])
def test_every_stage_contract_derives_from_the_immutable_base(model: type[BaseModel]) -> None:
    # Asserted on the base rather than on each `model_config`, so a contract
    # added later cannot opt out by forgetting rather than by deciding.
    assert issubclass(model, Contract)


@pytest.mark.parametrize("model", CONTRACTS, ids=[model.__name__ for model in CONTRACTS])
def test_every_stage_contract_is_frozen(model: type[BaseModel]) -> None:
    assert model.model_config.get("frozen") is True


def test_a_stage_cannot_rewrite_the_query_text_a_later_stage_reports() -> None:
    extracted = query()

    with pytest.raises(ValidationError):
        extracted.text = "SELECT id FROM orders"

    assert extracted.text == "SELECT * FROM orders"


def test_a_stage_cannot_re_anchor_a_finding_onto_a_different_file() -> None:
    finding = Finding(
        rule_id="select-star",
        severity=Severity.MEDIUM,
        title="t",
        explanation="e",
        impact="i",
        provenance=Provenance(file="a.sql", line=1),
    )

    with pytest.raises(ValidationError):
        finding.provenance = Provenance(file="b.sql", line=9)

    assert finding.provenance.file == "a.sql"


def test_a_report_cannot_be_repointed_at_another_run() -> None:
    report = Report(context=RunContext(run_id="r1", repo="acme/x", pr_number=1))

    with pytest.raises(ValidationError):
        report.context = RunContext(run_id="r2", repo="acme/y", pr_number=2)

    assert report.context.run_id == "r1"


def test_freezing_does_not_stop_a_contract_being_rebuilt_with_a_change() -> None:
    # Immutability must not cost the ability to derive a new value — an
    # enrichment stage replaces rather than edits.
    extracted = query()

    normalized = extracted.model_copy(update={"normalized": "SELECT * FROM orders"})

    assert normalized.normalized == "SELECT * FROM orders"
    assert extracted.normalized is None


def test_freezing_does_not_change_the_serialized_shape() -> None:
    # The JSON contract is user-facing through `POST /analyze`; making the
    # models immutable is an internal guarantee and must stay invisible in it.
    report = Report(context=RunContext(run_id="r1", repo="acme/x", pr_number=1), queries=[query()])

    assert Report.model_validate_json(report.model_dump_json()) == report


def test_a_sql_source_is_a_source_file_and_keeps_its_dialect() -> None:
    source = SqlSource(path="migrations/001.sql", content="SELECT 1", dialect="mysql")

    assert isinstance(source, SourceFile)
    assert source.dialect == "mysql"


def test_the_base_source_file_carries_no_sql_specific_field() -> None:
    # A dialect is meaningless to a Java file. Keeping it off the base is what
    # stops every language's input model accumulating every other language's
    # concerns.
    assert "dialect" not in SourceFile.model_fields
