"""Ground-truth trap definitions (eval/ground_truth.yaml)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

DocField = Literal["business_rules", "open_questions", "edge_cases", "main_flow", "procedure_notes"]


class Trap(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    type: str
    package: str
    procedure: str
    anchor: str
    description: str
    expect_in: list[DocField] = Field(min_length=1)
    match_all: list[str] = Field(min_length=1)
    forbid_in_rules: list[str] = Field(default_factory=list)

    @field_validator("anchor", "match_all", "forbid_in_rules")
    @classmethod
    def _regexes_compile(cls, v: str | list[str]) -> str | list[str]:
        for pattern in [v] if isinstance(v, str) else v:
            re.compile(pattern)
        return v


class GroundTruth(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: int
    traps: list[Trap]


def load_ground_truth(path: Path) -> GroundTruth:
    return GroundTruth.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
