"""Prompt loading and user-message construction.

Prompts live in `prompts/` at the repository root so they can be reviewed and
diffed like code. `PROMPT_VERSION` is the human-readable version that goes on
every page and into eval results; the fingerprint (a hash of the prompt files)
goes into the cache key so an edited prompt is never served from cache even if
someone forgets to bump the version.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from pydantic import BaseModel

PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"


@dataclass(frozen=True)
class Prompts:
    version: str
    system: str
    synthesis: str

    @property
    def fingerprint(self) -> str:
        h = hashlib.sha256()
        for part in (self.version, self.system, self.synthesis):
            h.update(part.encode("utf-8"))
            h.update(b"\0")
        return h.hexdigest()


def load_prompts(directory: Path = PROMPTS_DIR) -> Prompts:
    def read(name: str) -> str:
        return (directory / name).read_text(encoding="utf-8").replace("\r\n", "\n").strip()

    return Prompts(
        version=read("VERSION"),
        system=read("system.md"),
        synthesis=read("synthesis.md"),
    )


@cache
def default_prompts() -> Prompts:
    return load_prompts()


def number_lines(lines: list[str], start: int, end: int) -> str:
    """Lines `start..end` (1-based, inclusive) prefixed with their original numbers."""
    width = len(str(end))
    return "\n".join(f"{n:>{width}} | {lines[n - 1]}" for n in range(start, end + 1))


def schema_json(model: type[BaseModel]) -> str:
    return json.dumps(model.model_json_schema(), indent=1, sort_keys=True)


def unit_message(
    *, unit_name: str, kind: str, facts: dict, source: str, model: type[BaseModel]
) -> str:
    return (
        f"<unit_name>{unit_name}</unit_name>\n"
        f"<kind>{kind}</kind>\n"
        f"<facts>\n{json.dumps(facts, indent=1, sort_keys=True)}\n</facts>\n"
        f"<source>\n{source}\n</source>\n"
        f"<schema>\n{schema_json(model)}\n</schema>"
    )


def synthesis_message(
    *, unit_name: str, facts: dict, unit_docs: list[dict], model: type[BaseModel]
) -> str:
    return (
        f"<unit_name>{unit_name}</unit_name>\n"
        "<kind>package</kind>\n"
        f"<facts>\n{json.dumps(facts, indent=1, sort_keys=True)}\n</facts>\n"
        f"<unit_docs>\n{json.dumps(unit_docs, indent=1)}\n</unit_docs>\n"
        f"<schema>\n{schema_json(model)}\n</schema>"
    )
