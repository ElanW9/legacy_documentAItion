"""Convert a pydantic model into the JSON Schema subset Gemini's structured output accepts.

The Gemini docs list `$ref` only for recursive self-references ("#") and do not
mention `$defs`, so definitions are inlined. `title` keys are dropped (noise for
the model). Keywords Gemini ignores (e.g. `minLength`) are harmless and kept;
pydantic validation on our side enforces them regardless.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

_DROP = {"title", "$defs"}


def inline_schema(model: type[BaseModel]) -> dict[str, Any]:
    root = model.model_json_schema()
    defs: dict[str, Any] = root.get("$defs", {})

    def resolve(node: Any, stack: tuple[str, ...]) -> Any:
        if isinstance(node, list):
            return [resolve(n, stack) for n in node]
        if not isinstance(node, dict):
            return node
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            name = ref.removeprefix("#/$defs/")
            if name in stack:
                raise ValueError(f"recursive schema reference to {name} is not supported")
            merged = {**defs[name], **{k: v for k, v in node.items() if k != "$ref"}}
            return resolve(merged, (*stack, name))
        return {k: resolve(v, stack) for k, v in node.items() if k not in _DROP}

    return resolve(root, ())
