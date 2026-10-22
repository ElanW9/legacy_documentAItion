"""Pydantic models.

FACTS (this section) are produced deterministically by the extract stage and
are authoritative: the renderer shows them verbatim and the LLM is told never to
restate or contradict them.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SqlOperation = Literal["SELECT", "INSERT", "UPDATE", "DELETE", "MERGE"]
ParamMode = Literal["IN", "OUT", "IN OUT"]
SubprogramKind = Literal["PROCEDURE", "FUNCTION"]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Parameter(_Frozen):
    name: str
    mode: ParamMode
    type: str
    default: str | None = None


class TableAccess(_Frozen):
    """A table (or view/synonym; static analysis cannot tell) touched by SQL."""

    table: str
    operations: list[SqlOperation]
    aliases: list[str] = Field(default_factory=list)
    lines: list[int]


class Call(_Frozen):
    """A call to a member of another package."""

    package: str
    member: str
    kind: Literal["repo", "builtin"]
    lines: list[int]

    @property
    def target(self) -> str:
        return f"{self.package}.{self.member}"


class RaisedError(_Frozen):
    code: int
    message: str
    line: int


class Subprogram(_Frozen):
    name: str
    kind: SubprogramKind
    visibility: Literal["public", "private"]
    parameters: list[Parameter] = Field(default_factory=list)
    return_type: str | None = None
    spec_line: int | None = None
    start_line: int | None = None
    end_line: int | None = None
    tables: list[TableAccess] = Field(default_factory=list)
    calls: list[Call] = Field(default_factory=list)
    internal_calls: list[str] = Field(default_factory=list)
    referenced_by: list[str] = Field(default_factory=list)
    sequences: list[str] = Field(default_factory=list)
    raises: list[RaisedError] = Field(default_factory=list)
    dynamic_sql: bool = False


class GlobalDecl(_Frozen):
    """A package-level variable or constant."""

    name: str
    type: str
    constant: bool
    value: str | None = None
    line: int
    location: Literal["spec", "body"]


class PackageFacts(_Frozen):
    name: str
    spec_path: str | None
    body_path: str | None
    source_hash: str
    spec_line_count: int
    body_line_count: int
    globals: list[GlobalDecl] = Field(default_factory=list)
    subprograms: list[Subprogram] = Field(default_factory=list)
    tables: list[TableAccess] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    def subprogram_names(self) -> list[str]:
        return [s.name for s in self.subprograms]
