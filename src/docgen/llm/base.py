"""Provider-neutral LLM interface.

The pipeline only depends on `LLMClient`. A client takes a system prompt (the
instructions), a user message (the data) and the pydantic model the answer must
conform to, and returns raw text plus usage metadata. Validation against the
model is done by the caller, so every provider is held to the same contract
even if its own structured-output support is weaker.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from pydantic import BaseModel


class LLMError(RuntimeError):
    """A non-retryable LLM failure (or retries exhausted). Never contains secrets."""


class RateLimitError(LLMError):
    """HTTP 429 / quota exhausted. Retryable."""

    def __init__(self, message: str, retry_after_s: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


class TransientLLMError(LLMError):
    """5xx / timeout. Retryable."""


@dataclass(frozen=True)
class LLMResponse:
    text: str
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_s: float = 0.0
    attempts: int = 1


@runtime_checkable
class LLMClient(Protocol):
    @property
    def model(self) -> str: ...

    def generate_json(
        self, *, system: str, user: str, response_model: type[BaseModel]
    ) -> LLMResponse:
        """Return the model's JSON answer as text. Must not validate or repair it."""
        ...
