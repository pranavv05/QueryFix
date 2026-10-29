"""Builders for the N+1 suite.

Every fixture here produces *generic* Java — `Parent`, `Thing`, `ExampleService`.
That is deliberate and load-bearing. The detector must work on any Spring/JPA
repository, so a suite written against a particular application's class names
would pass while proving nothing about the next one. Where a real fixture is
wanted, the sandbox has one and `test_stage_integration.py` uses it; these
builders exist so the behavioural tests cannot accidentally encode a name.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

import pytest

from queryfix.models.finding import Finding
from queryfix.models.nplusone import NPlusOneCandidate
from queryfix.models.query import ExtractedQuery, SourceFile
from queryfix.pipeline.extract import extract_source
from queryfix.pipeline.extract.java_structure import ResolveJavaSource, analyze_java_program
from queryfix.pipeline.nplusone import build_candidates, detect_n_plus_one

#: Where generated sources live. A nested package under a conventional Maven
#: layout, so import-path derivation has something real to resolve against.
SERVICE_PACKAGE = "com.example.service"
DATA_PACKAGE = "com.example.data"
DOMAIN_PACKAGE = "com.example.domain"

ServiceBuilder = Callable[..., SourceFile]
RepositoryBuilder = Callable[..., SourceFile]
EntityBuilder = Callable[..., SourceFile]


def _path(package: str, name: str) -> str:
    return f"src/main/java/{package.replace('.', '/')}/{name}.java"


@pytest.fixture
def service() -> ServiceBuilder:
    """A Spring-style service class wrapping a method body.

    Defaults to constructor injection of one repository, which is the shape most
    real services use; ``fields`` and ``constructor`` override it for the tests
    that are specifically about injection style.
    """

    def build(
        body: str,
        *,
        name: str = "ExampleService",
        fields: str = "    private final ThingRepository thingRepository;",
        constructor: str | None = None,
        signature: str = "public void run(List<Parent> parents)",
        extra: str = "",
        imports: Sequence[str] = (f"{DATA_PACKAGE}.ThingRepository",),
    ) -> SourceFile:
        injected = (
            constructor
            if constructor is not None
            else (
                f"    public {name}(ThingRepository thingRepository) {{\n"
                f"        this.thingRepository = thingRepository;\n"
                f"    }}"
            )
        )
        import_block = "".join(f"import {item};\n" for item in imports)
        return SourceFile(
            path=_path(SERVICE_PACKAGE, name),
            content=(
                f"package {SERVICE_PACKAGE};\n\n"
                f"{import_block}\n"
                f"public class {name} {{\n"
                f"{fields}\n\n"
                f"{injected}\n\n"
                f"    {signature} {{\n"
                f"{body}\n"
                f"    }}\n"
                f"{extra}\n"
                f"}}\n"
            ),
        )

    return build


@pytest.fixture
def repository() -> RepositoryBuilder:
    """A Spring Data repository interface declaring the given members."""

    def build(
        *members: str,
        name: str = "ThingRepository",
        base: str = "JpaRepository<Thing, Long>",
        package: str = DATA_PACKAGE,
    ) -> SourceFile:
        declared = "\n\n".join(f"    {member}" for member in members)
        extends = f" extends {base}" if base else ""
        return SourceFile(
            path=_path(package, name),
            content=(
                f"package {package};\n\n"
                f"import org.springframework.data.jpa.repository.JpaRepository;\n"
                f"import org.springframework.data.jpa.repository.Query;\n\n"
                f"public interface {name}{extends} {{\n\n"
                f"{declared}\n"
                f"}}\n"
            ),
        )

    return build


@pytest.fixture
def entity() -> EntityBuilder:
    """A JPA entity with the given field declarations."""

    def build(
        *fields: str,
        name: str = "Parent",
        package: str = DOMAIN_PACKAGE,
        table: str | None = None,
    ) -> SourceFile:
        declared = "\n\n".join(f"    {field}" for field in fields)
        mapping = f'@Table(name = "{table}")\n' if table is not None else ""
        return SourceFile(
            path=_path(package, name),
            content=(
                f"package {package};\n\n"
                f"import jakarta.persistence.Entity;\n\n"
                f"@Entity\n"
                f"{mapping}"
                f"public class {name} {{\n\n"
                f"    @Id\n    private Long id;\n\n"
                f"{declared}\n"
                f"}}\n"
            ),
        )

    return build


def extract_all(sources: Sequence[SourceFile]) -> list[ExtractedQuery]:
    """Every query the extract stage finds, as the runner would supply them."""
    return [query for source in sources for query in extract_source(source)]


@pytest.fixture
def detect() -> Callable[..., list[Finding]]:
    """Run the whole stage over some sources and return its findings."""

    def run(
        *sources: SourceFile,
        repeated_statements: Sequence[object] | None = None,
        resolve_source: ResolveJavaSource | None = None,
    ) -> list[Finding]:
        return detect_n_plus_one(
            list(sources),
            queries=extract_all(sources),
            repeated_statements=repeated_statements,  # type: ignore[arg-type]
            resolve_source=resolve_source,
        )

    return run


@pytest.fixture
def candidates() -> Callable[..., list[NPlusOneCandidate]]:
    """The deterministic half alone, for asserting on evidence rather than prose."""

    def run(
        *sources: SourceFile,
        repeated_statements: Sequence[object] | None = None,
        resolve_source: ResolveJavaSource | None = None,
    ) -> list[NPlusOneCandidate]:
        program = analyze_java_program(
            list(sources),
            queries=extract_all(sources),
            resolve_source=resolve_source,
        )
        return build_candidates(
            program,
            repeated_statements=repeated_statements,  # type: ignore[arg-type]
        )

    return run
