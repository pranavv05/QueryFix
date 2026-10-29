"""Java structural contracts — what the N+1 stage reads instead of raw source.

Stage 7 asks questions no single query can answer: *is this repository call
executed once per element of something?* Answering it needs control flow, type
resolution, and call sites — none of which
:class:`~queryfix.models.query.ExtractedQuery` carries, and none of which
belong on it. A query is a query wherever it was written; a call site is a fact
about a program.

So the structural facts live here, in their own contracts, and the detector reads
these rather than Java text. That boundary is the point of the module. The
analyzer behind it is lexical today (see
:mod:`queryfix.pipeline.extract.java_structure` for what that can and cannot
see); replacing it with tree-sitter or the JavaParser sidecar CLAUDE.md plans for
means producing these same models from a real parse tree, and the detector does
not change. A detector written against Java text instead would have to be
rewritten with the parser.

Everything here is immutable, like every other stage contract, and everything
carries provenance: a structural claim a reviewer cannot locate in their own file
is a claim they cannot check.
"""

from __future__ import annotations

from enum import Enum

from pydantic import Field

from queryfix.models.base import Contract

__all__ = [
    "ArgumentDependency",
    "EntityRelationship",
    "InjectionKind",
    "IterationContext",
    "IterationKind",
    "JavaFileStructure",
    "JavaProgram",
    "LazyAssociationAccess",
    "MethodDeclaration",
    "RepositoryCallSite",
    "RepositoryDeclaration",
    "RepositoryHandle",
    "RepositoryMethodDeclaration",
    "RepositoryResolution",
    "SourceSpan",
    "TypeDeclaration",
]


class SourceSpan(Contract):
    """A half-open character range in one file, with its line coordinates.

    Both are kept because they answer different questions. Offsets are what
    containment is decided by — whether a call site sits inside a loop body is an
    offset comparison and nothing else. Lines are what a reviewer is shown.
    """

    start: int = Field(ge=0, description="First character offset, inclusive.")
    end: int = Field(ge=0, description="Last character offset, exclusive.")
    start_line: int = Field(ge=1, description="1-based line the span opens on.")
    end_line: int = Field(ge=1, description="1-based line the span closes on.")

    def contains(self, offset: int) -> bool:
        """Whether ``offset`` falls inside this span."""
        return self.start <= offset < self.end

    @property
    def length(self) -> int:
        """How many characters the span covers, for innermost-first ordering."""
        return self.end - self.start


class RepositoryResolution(str, Enum):
    """How confident we are that a type is a Spring Data repository at all.

    This is the difference between a fact and a convention, and it is kept as
    data rather than folded into a score because the two justify different
    claims to a reviewer. ``DECLARED`` means we read the interface and saw it
    extend a Spring Data base type. ``NAME_CONVENTION`` means we never saw the
    declaration — it lives outside the pull request and could not be retrieved —
    and are going on the type's name alone.

    A finding built on a convention must say so; CLAUDE.md's rule that an
    unverifiable claim is labelled rather than presented as established applies
    to the structural half of the evidence just as it does to the query half.
    """

    DECLARED = "declared"
    NAME_CONVENTION = "name-convention"


class InjectionKind(str, Enum):
    """How a repository reached the identifier a call site invokes it on."""

    FIELD = "field"
    CONSTRUCTOR_PARAMETER = "constructor-parameter"
    METHOD_PARAMETER = "method-parameter"
    LOCAL_VARIABLE = "local-variable"


class IterationKind(str, Enum):
    """The construct that makes a statement run more than once.

    Lambda kinds are split from loop kinds deliberately. A loop body provably
    repeats; a lambda body repeats only if whatever consumes it iterates, which
    is a claim about the receiver's *type*, not its syntax. See
    :mod:`queryfix.pipeline.extract.java_structure` for how that is decided and
    why ``Optional.map`` must not be read as a per-element operation.
    """

    FOR = "for"
    ENHANCED_FOR = "enhanced-for"
    WHILE = "while"
    DO_WHILE = "do-while"
    LAMBDA_FOR_EACH = "lambda-for-each"
    LAMBDA_STREAM = "lambda-stream"


class ArgumentDependency(str, Enum):
    """Whether the repeated call's arguments are derived from the element.

    The distinction between a query that varies per element and one that does not
    is the difference between the classic parent-key/child-query N+1 and a call
    that is merely repeated. Both are worth reporting; they are not equally
    strong, and conflating them is how a detector earns a reputation for noise.
    """

    LOOP_ELEMENT_ARGUMENT = "loop-element-argument"
    INDEPENDENT = "independent"
    NO_ARGUMENTS = "no-arguments"


class TypeDeclaration(Contract):
    """One class, interface, enum, or record declaration and its body extent."""

    name: str = Field(min_length=1, description="Simple name, without type parameters.")
    kind: str = Field(description='One of "class", "interface", "enum", "record".')
    line: int = Field(ge=1)
    span: SourceSpan = Field(description="The declaration body, brace to brace.")
    supertypes: tuple[str, ...] = Field(
        default=(),
        description="Simple names in the extends/implements clause, type arguments "
        "stripped. Retained so repository-ness can be resolved transitively.",
    )
    supertype_arguments: tuple[str, ...] = Field(
        default=(),
        description="Type arguments of the first parameterised supertype — the "
        "`Order` in `JpaRepository<Order, Long>`, which is how a repository names "
        "the entity, and therefore the table, it reads.",
    )
    annotations: tuple[str, ...] = Field(default=())


class MethodDeclaration(Contract):
    """One method or constructor declaration.

    ``body`` is None for an abstract or interface method — which is the normal
    case for a Spring Data repository method, where the declaration *is* the
    query and there is nothing to execute.
    """

    name: str = Field(min_length=1)
    line: int = Field(ge=1)
    declaring_type: str | None = Field(default=None)
    parameters_span: SourceSpan
    body: SourceSpan | None = Field(default=None)
    annotations: tuple[str, ...] = Field(default=())


class RepositoryMethodDeclaration(Contract):
    """A method on a repository interface — the query a call site resolves to."""

    name: str = Field(min_length=1)
    line: int = Field(ge=1)
    repository_type: str = Field(min_length=1)
    file: str = Field(min_length=1)
    query_id: str | None = Field(
        default=None,
        description="The ExtractedQuery this declaration produced, when extraction "
        "produced one. None for a method inherited from a Spring Data base "
        "interface, which has no declaration text of its own to extract.",
    )
    annotations: tuple[str, ...] = Field(default=())


class RepositoryDeclaration(Contract):
    """A repository interface, and how we decided it was one."""

    type_name: str = Field(min_length=1)
    file: str = Field(min_length=1)
    line: int = Field(ge=1)
    resolution: RepositoryResolution
    supertypes: tuple[str, ...] = Field(default=())
    entity_type: str | None = Field(
        default=None,
        description="The entity this repository manages, from its base interface's "
        "first type argument. None for a repository whose base is unparameterised.",
    )
    methods: tuple[RepositoryMethodDeclaration, ...] = Field(default=())


class RepositoryHandle(Contract):
    """An identifier that holds a repository, and where it got one."""

    identifier: str = Field(min_length=1)
    repository_type: str = Field(min_length=1)
    injection: InjectionKind
    line: int = Field(ge=1)
    scope: SourceSpan | None = Field(
        default=None,
        description="Where the identifier is in scope. None for a field, which is "
        "in scope throughout its declaring type.",
    )


class IterationContext(Contract):
    """A construct whose body runs once per element, and its body's extent."""

    kind: IterationKind
    span: SourceSpan = Field(description="The repeated body, not the header.")
    element_identifier: str | None = Field(
        default=None,
        description="The loop variable or lambda parameter, when the construct "
        "binds one. None for a classic `for` or a `while`.",
    )
    element_type: str | None = Field(
        default=None,
        description="Declared type of the element, when the construct states one. "
        "Only an enhanced `for` does; a lambda parameter is usually inferred.",
    )
    iterable_text: str | None = Field(
        default=None,
        description="The expression being iterated, as written, for evidence.",
    )


class RepositoryCallSite(Contract):
    """A resolved repository method invocation, with its repetition context.

    ``iteration`` is ordered outermost-first, so its length is the nesting depth
    and its last element is the construct that most directly repeats the call.
    An empty tuple means the call was found outside any repeating construct,
    which is the overwhelmingly common and entirely healthy case.
    """

    file: str = Field(min_length=1)
    line: int = Field(ge=1)
    span: SourceSpan
    receiver_text: str = Field(description="The receiver as written, e.g. `this.repo`.")
    receiver_identifier: str = Field(min_length=1)
    repository_type: str = Field(min_length=1)
    resolution: RepositoryResolution
    injection: InjectionKind
    method_name: str = Field(min_length=1)
    arguments_text: str = Field(default="")
    enclosing_method: str | None = Field(default=None)
    enclosing_type: str | None = Field(default=None)
    iteration: tuple[IterationContext, ...] = Field(default=())
    dependency: ArgumentDependency = Field(default=ArgumentDependency.INDEPENDENT)
    dependency_detail: str | None = Field(default=None)

    @property
    def loop_depth(self) -> int:
        """How many repeating constructs enclose this call."""
        return len(self.iteration)

    @property
    def innermost_iteration(self) -> IterationContext | None:
        """The construct that most directly repeats this call, if any."""
        return self.iteration[-1] if self.iteration else None


class EntityRelationship(Contract):
    """A lazily-fetched JPA association, which a getter may load on access."""

    entity_type: str = Field(min_length=1)
    field_name: str = Field(min_length=1)
    accessor_name: str = Field(min_length=1, description="The derived getter, e.g. `getOrders`.")
    association: str = Field(description="`@OneToMany`, `@ManyToOne`, …, without the `@`.")
    file: str = Field(min_length=1)
    line: int = Field(ge=1)


class LazyAssociationAccess(Contract):
    """A lazy association dereferenced inside a repeating construct.

    Weaker evidence than a repository call by construction: a getter is an
    ordinary method call, and whether it issues SQL depends on the persistence
    context at run time — whether the association was already initialized by a
    fetch join, an entity graph, or a previous touch. The detector reports it as
    a possibility and says so.
    """

    file: str = Field(min_length=1)
    line: int = Field(ge=1)
    span: SourceSpan
    receiver_identifier: str = Field(min_length=1)
    entity_type: str = Field(min_length=1)
    accessor_name: str = Field(min_length=1)
    relationship: EntityRelationship
    enclosing_method: str | None = Field(default=None)
    enclosing_type: str | None = Field(default=None)
    iteration: tuple[IterationContext, ...] = Field(default=())

    @property
    def loop_depth(self) -> int:
        """How many repeating constructs enclose this access."""
        return len(self.iteration)


class JavaFileStructure(Contract):
    """Everything the structural analyzer could establish about one Java file."""

    path: str = Field(min_length=1)
    package: str | None = Field(default=None)
    imports: tuple[str, ...] = Field(default=())
    types: tuple[TypeDeclaration, ...] = Field(default=())
    methods: tuple[MethodDeclaration, ...] = Field(default=())
    repositories: tuple[RepositoryDeclaration, ...] = Field(default=())
    handles: tuple[RepositoryHandle, ...] = Field(default=())
    call_sites: tuple[RepositoryCallSite, ...] = Field(default=())
    iterations: tuple[IterationContext, ...] = Field(default=())
    relationships: tuple[EntityRelationship, ...] = Field(default=())
    lazy_accesses: tuple[LazyAssociationAccess, ...] = Field(default=())
    tables: dict[str, str] = Field(
        default_factory=dict,
        description="Entity simple name to its `@Table(name = …)` mapping.",
    )


class JavaProgram(Contract):
    """Every analyzed file in one run, plus the indexes that span them.

    N+1 is not a property of a file. The loop lives in a service, the method it
    calls is declared on a repository interface in another file, and the entity
    whose association is being walked is in a third — so the unit of analysis has
    to be the set of files, not each one alone. This is that set, already indexed,
    so the detector does dictionary lookups rather than rescanning.

    ``unresolved_types`` records receivers that looked like repositories and could
    not be confirmed as such, which is how a run says what it could not see rather
    than silently treating absence of evidence as evidence of absence.
    """

    files: dict[str, JavaFileStructure] = Field(default_factory=dict)
    repositories: dict[str, RepositoryDeclaration] = Field(
        default_factory=dict, description="Simple type name to its declaration."
    )
    entities: dict[str, tuple[EntityRelationship, ...]] = Field(
        default_factory=dict, description="Entity simple name to its lazy associations."
    )
    tables: dict[str, str] = Field(
        default_factory=dict,
        description="Entity simple name to the table it maps to, read from "
        "`@Table(name = …)`. The only place QueryFix learns a real table name "
        "rather than deriving one from a class name.",
    )
    unresolved_types: tuple[str, ...] = Field(
        default=(),
        description="Types used as repository-shaped receivers whose declaration "
        "was never seen. Their call sites fall back to name-convention resolution.",
    )
