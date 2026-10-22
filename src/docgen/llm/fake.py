"""Deterministic offline LLM client for tests and `--dry-run` demos."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from pydantic import BaseModel

from docgen.llm.base import LLMResponse

Responder = Callable[[str, str], str]

_FACTS_RE = re.compile(r"<facts>\s*(.*?)\s*</facts>", re.DOTALL)
_UNIT_RE = re.compile(r"<unit_name>\s*(.*?)\s*</unit_name>", re.DOTALL)


@dataclass
class FakeCall:
    system: str
    user: str
    response_model: str


@dataclass
class FakeLLMClient:
    """Returns scripted responses.

    - `responses`: consumed in order (useful to script invalid-then-valid).
    - `responder`: called with (system, user) when no scripted response is left.
    - default: a minimal *valid* doc derived from the <facts> in the user message,
      so the full pipeline can run offline.
    """

    responses: list[str] = field(default_factory=list)
    responder: Responder | None = None
    model_name: str = "fake-llm"
    calls: list[FakeCall] = field(default_factory=list)

    @classmethod
    def scripted(cls, responses: Iterable[str | dict]) -> FakeLLMClient:
        return cls(responses=[r if isinstance(r, str) else json.dumps(r) for r in responses])

    @property
    def model(self) -> str:
        return self.model_name

    def generate_json(
        self, *, system: str, user: str, response_model: type[BaseModel]
    ) -> LLMResponse:
        self.calls.append(FakeCall(system, user, response_model.__name__))
        if self.responses:
            text = self.responses.pop(0)
        elif self.responder is not None:
            text = self.responder(system, user)
        else:
            text = json.dumps(default_doc_from_user_message(user))
        return LLMResponse(
            text=text,
            model=self.model_name,
            input_tokens=len(system + user) // 4,
            output_tokens=len(text) // 4,
            latency_s=0.0,
        )


def default_doc_from_user_message(user: str) -> dict:
    """Build a schema-valid placeholder doc from the FACTS embedded in `user`."""
    facts_match = _FACTS_RE.search(user)
    unit_match = _UNIT_RE.search(user)
    facts = json.loads(facts_match.group(1)) if facts_match else {}
    unit = unit_match.group(1) if unit_match else "UNKNOWN"
    subs = facts.get("subprograms", [])
    return {
        "source_complete": True,
        "purpose": f"[fake] Placeholder purpose for {unit}.",
        "procedure_notes": [
            {"name": s["name"], "summary": f"[fake] Placeholder summary for {s['name']}."}
            for s in subs
        ],
        "business_rules": [],
        "main_flow": [],
        "edge_cases": [],
        "open_questions": [],
    }
