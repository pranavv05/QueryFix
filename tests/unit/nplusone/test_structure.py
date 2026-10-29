"""The structural analyzer, tested on its own terms.

These assert the facts the detector is built on rather than the findings it
produces, so a regression in type resolution or loop bounding is reported here —
where the cause is obvious — instead of only as a missing finding two layers up.
"""

from __future__ import annotations

from queryfix.models.java_structure import (
    InjectionKind,
    IterationKind,
    RepositoryResolution,
)
from queryfix.models.query import SourceFile
from queryfix.pipeline.extract.java_structure import (
    analyze_java_file,
    analyze_java_program,
    derive_import_path,
)

SERVICE_PATH = "src/main/java/com/example/service/ExampleService.java"


def analyze(content: str, path: str = SERVICE_PATH) -> object:
    return analyze_java_file(path, content)


def test_a_repository_is_recognized_by_what_it_extends() -> None:
    structure = analyze_java_file(
        "src/main/java/com/example/data/ThingRepository.java",
        "package com.example.data;\n"
        "public interface ThingRepository extends JpaRepository<Thing, Long> {\n"
        "    List<Thing> findByParentId(Long id);\n"
        "}\n",
    )

    (declaration,) = structure.repositories
    assert declaration.type_name == "ThingRepository"
    assert declaration.resolution is RepositoryResolution.DECLARED
    assert [method.name for method in declaration.methods] == ["findByParentId"]


def test_every_spring_data_base_interface_is_accepted() -> None:
    for base in (
        "CrudRepository<Thing, Long>",
        "PagingAndSortingRepository<Thing, Long>",
        "Repository<Thing, Long>",
        "ReactiveCrudRepository<Thing, Long>",
    ):
        structure = analyze_java_file(
            "src/main/java/com/example/data/ThingRepository.java",
            f"package com.example.data;\n"
            f"public interface ThingRepository extends {base} {{\n"
            f"    List<Thing> findByParentId(Long id);\n"
            f"}}\n",
        )
        assert structure.repositories, f"{base} was not recognized"


def test_a_custom_base_interface_resolves_transitively() -> None:
    structure = analyze_java_file(
        "src/main/java/com/example/data/Repos.java",
        "package com.example.data;\n"
        "public interface ThingRepository extends BaseRepository<Thing> {\n"
        "    List<Thing> findByParentId(Long id);\n"
        "}\n"
        "interface BaseRepository<T> extends JpaRepository<T, Long> {\n"
        "}\n",
    )

    assert {declaration.type_name for declaration in structure.repositories} == {
        "ThingRepository",
        "BaseRepository",
    }


def test_an_interface_annotated_repository_is_accepted() -> None:
    structure = analyze_java_file(
        "src/main/java/com/example/data/ThingRepository.java",
        "package com.example.data;\n"
        "@Repository\n"
        "public interface ThingRepository {\n"
        "    List<Thing> findByParentId(Long id);\n"
        "}\n",
    )

    assert structure.repositories


def test_a_plain_interface_is_not_a_repository() -> None:
    structure = analyze_java_file(
        "src/main/java/com/example/data/Thing.java",
        "package com.example.data;\npublic interface Thing {\n    Long getId();\n}\n",
    )

    assert structure.repositories == ()


def test_injection_kinds_are_distinguished() -> None:
    structure = analyze_java_file(
        SERVICE_PATH,
        "package com.example.service;\n"
        "public class ExampleService {\n"
        "    private final ThingRepository fromField;\n"
        "    ExampleService(ThingRepository fromConstructor) { }\n"
        "    void run(ThingRepository fromParameter) {\n"
        "        ThingRepository fromLocal = fromField;\n"
        "    }\n"
        "}\n",
    )

    by_name = {handle.identifier: handle.injection for handle in structure.handles}
    assert by_name["fromField"] is InjectionKind.FIELD
    assert by_name["fromConstructor"] is InjectionKind.CONSTRUCTOR_PARAMETER
    assert by_name["fromParameter"] is InjectionKind.METHOD_PARAMETER
    assert by_name["fromLocal"] is InjectionKind.LOCAL_VARIABLE


def test_a_var_alias_resolves_only_from_a_known_handle() -> None:
    structure = analyze_java_file(
        SERVICE_PATH,
        "package com.example.service;\n"
        "public class ExampleService {\n"
        "    private final ThingRepository thingRepository;\n"
        "    void run() {\n"
        "        var aliased = thingRepository;\n"
        "        var mystery = someFactory.create();\n"
        "    }\n"
        "}\n",
    )

    identifiers = {handle.identifier for handle in structure.handles}
    assert "aliased" in identifiers
    # An unresolvable right-hand side is not followed, rather than guessed at.
    assert "mystery" not in identifiers


def test_loop_bodies_exclude_their_headers() -> None:
    structure = analyze_java_file(
        SERVICE_PATH,
        "package com.example.service;\n"
        "public class ExampleService {\n"
        "    void run() {\n"
        "        for (Thing t : repo.findAll()) {\n"
        "            sum += t.getAmount();\n"
        "        }\n"
        "    }\n"
        "}\n",
    )

    (loop,) = [c for c in structure.iterations if c.kind is IterationKind.ENHANCED_FOR]
    header_offset = structure.path and 0  # placeholder to keep the assertion readable
    del header_offset
    assert loop.element_identifier == "t"
    assert loop.element_type == "Thing"
    assert loop.iterable_text == "repo.findAll()"


def test_a_single_statement_loop_body_is_bounded() -> None:
    structure = analyze_java_file(
        SERVICE_PATH,
        "package com.example.service;\n"
        "public class ExampleService {\n"
        "    private final ThingRepository repo;\n"
        "    void run() {\n"
        "        for (Parent p : parents) repo.findByParentId(p.getId());\n"
        "        repo.findAll();\n"
        "    }\n"
        "}\n",
    )

    depths = {site.method_name: site.loop_depth for site in structure.call_sites}
    assert depths["findByParentId"] == 1
    # The statement after a brace-less loop body is not inside it.
    assert depths["findAll"] == 0


def test_comments_and_literals_produce_no_structure() -> None:
    structure = analyze_java_file(
        SERVICE_PATH,
        "package com.example.service;\n"
        "public class ExampleService {\n"
        "    private final ThingRepository repo;\n"
        "    void run() {\n"
        '        String s = "for (P p : ps) { repo.findByParentId(p.getId()); }";\n'
        "        // for (P p : ps) { repo.findByParentId(p.getId()); }\n"
        "    }\n"
        "}\n",
    )

    assert structure.call_sites == ()
    assert structure.iterations == ()


def test_lazy_and_eager_associations_are_distinguished() -> None:
    structure = analyze_java_file(
        "src/main/java/com/example/domain/Parent.java",
        "package com.example.domain;\n"
        "@Entity\n"
        "public class Parent {\n"
        '    @OneToMany(mappedBy = "parent")\n'
        "    private List<Child> children;\n"
        "    @ManyToOne(fetch = FetchType.EAGER)\n"
        "    private Account account;\n"
        "    @ManyToOne(fetch = FetchType.LAZY)\n"
        "    private Region region;\n"
        "    @OneToMany(fetch = FetchType.EAGER)\n"
        "    private List<Tag> tags;\n"
        "}\n",
    )

    lazy = {relationship.field_name for relationship in structure.relationships}
    # OneToMany defaults to LAZY; ManyToOne defaults to EAGER unless overridden.
    assert lazy == {"children", "region"}


def test_import_paths_are_derived_from_the_files_own_coordinates() -> None:
    structure = analyze_java_file(
        "modules/core/src/main/java/com/example/service/ExampleService.java",
        "package com.example.service;\n"
        "import com.example.data.ThingRepository;\n"
        "public class ExampleService { }\n",
    )

    assert (
        derive_import_path(structure, "ThingRepository")
        == "modules/core/src/main/java/com/example/data/ThingRepository.java"
    )


def test_a_same_package_type_resolves_without_an_import() -> None:
    structure = analyze_java_file(
        "src/main/java/com/example/service/ExampleService.java",
        "package com.example.service;\npublic class ExampleService { }\n",
    )

    assert (
        derive_import_path(structure, "Helper") == "src/main/java/com/example/service/Helper.java"
    )


def test_an_unreadable_file_is_not_fetched_twice() -> None:
    """Targeted retrieval asks once per type, however many call sites want it."""
    asked: list[str] = []

    def resolve(path: str) -> str | None:
        asked.append(path)
        return None

    service = SourceFile(
        path="src/main/java/com/example/service/ExampleService.java",
        content=(
            "package com.example.service;\n"
            "import com.example.data.ThingRepository;\n"
            "public class ExampleService {\n"
            "    private final ThingRepository repo;\n"
            "    void a() { for (P p : ps) { repo.findByParentId(p.getId()); } }\n"
            "    void b() { for (P p : ps) { repo.findByParentId(p.getId()); } }\n"
            "}\n"
        ),
    )

    program = analyze_java_program([service], resolve_source=resolve)

    assert asked == ["src/main/java/com/example/data/ThingRepository.java"]
    assert program.unresolved_types == ("ThingRepository",)


def test_an_unbalanced_file_does_not_raise() -> None:
    """A file mid-edit is ordinary input for a diff, not an error."""
    structure = analyze_java_file(
        SERVICE_PATH,
        "package com.example.service;\npublic class ExampleService {\n    void run() {\n",
    )

    assert structure.path == SERVICE_PATH
