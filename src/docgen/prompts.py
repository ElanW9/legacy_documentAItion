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
from dataclasses import dataclass, replace
from functools import cache
from pathlib import Path

from pydantic import BaseModel

PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"


@dataclass(frozen=True)
class Prompts:
    version: str
    system: str
    synthesis: str
    # The schema is always enforced via the provider's structured-output API.
    # This controls whether it is *also* repeated in the user message (<schema>).
    schema_in_prompt: bool = True

    @property
    def fingerprint(self) -> str:
        h = hashlib.sha256()
        for part in (self.version, self.system, self.synthesis, str(self.schema_in_prompt)):
            h.update(part.encode("utf-8"))
            h.update(b"\0")
        return h.hexdigest()

    def without_schema_in_prompt(self) -> Prompts:
        """Eval variant: drop <schema> from the user message and its mention in the prompts."""

        def strip(text: str) -> str:
            return "\n".join(ln for ln in text.split("\n") if "<schema>" not in ln)

        return replace(
            self,
            version=f"{self.version}-noschema",
            system=strip(self.system),
            synthesis=strip(self.synthesis),
            schema_in_prompt=False,
        )


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


def _schema_block(model: type[BaseModel] | None) -> str:
    return f"\n<schema>\n{schema_json(model)}\n</schema>" if model is not None else ""


def unit_message(
    *, unit_name: str, kind: str, facts: dict, source: str, model: type[BaseModel] | None
) -> str:
    """`model=None` omits the <schema> block."""
    return (
        f"<unit_name>{unit_name}</unit_name>\n"
        f"<kind>{kind}</kind>\n"
        f"<facts>\n{json.dumps(facts, indent=1, sort_keys=True)}\n</facts>\n"
        f"<source>\n{source}\n</source>" + _schema_block(model)
    )


def synthesis_message(
    *, unit_name: str, facts: dict, unit_docs: list[dict], model: type[BaseModel] | None
) -> str:
    return (
        f"<unit_name>{unit_name}</unit_name>\n"
        "<kind>package</kind>\n"
        f"<facts>\n{json.dumps(facts, indent=1, sort_keys=True)}\n</facts>\n"
        f"<unit_docs>\n{json.dumps(unit_docs, indent=1)}\n</unit_docs>" + _schema_block(model)
    )
