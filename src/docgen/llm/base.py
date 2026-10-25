"""Provider-neutral LLM interface.

The pipeline only depends on `LLMClient`. A client takes a system prompt (the
instructions), a user message (the data) and the pydantic model the answer must
conform to, and returns raw text plus usage metadata. Validation against the
model is done by the caller, so every provider is held to the same contract
even if its own structured-output support is weaker.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from pydantic import BaseModel

from docgen.retry import RetryDecision


class LLMError(RuntimeError):
    """A non-retryable LLM failure (or retries exhausted). Never contains secrets."""


class RateLimitError(LLMError):
    """HTTP 429 / quota exhausted. Retryable."""

    def __init__(self, message: str, retry_after_s: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


class TransientLLMError(LLMError):
    """5xx / timeout. Retryable."""


class OverloadedError(TransientLLMError):
    """HTTP 503: the model is overloaded. Retryable, but only after a longer wait."""


class QuotaExhaustedError(LLMError):
    """A daily (or longer) quota is used up. Not retryable within a run."""


def provider_unavailable(exc: BaseException) -> bool:
    """True when further calls in this run would most likely fail the same way.

    On a free tier every rejected request still counts against the daily
    quota (observed with Gemini), so a run stops calling the model instead of
    burning the remaining quota on the next packages.
    """
    return isinstance(exc, OverloadedError | RateLimitError | QuotaExhaustedError)


def make_classifier(
    overload_min_delay_s: float, overload_max_retries: int
) -> Callable[[Exception], RetryDecision]:
    """Retry policy for provider clients: 429 honours the server's
    delay, 503 gets few retries with long waits, other transient errors back off."""

    def classify(exc: Exception) -> RetryDecision:
        if isinstance(exc, RateLimitError):
            return RetryDecision(True, exc.retry_after_s)
        if isinstance(exc, OverloadedError):
            return RetryDecision(
                True, min_delay_s=overload_min_delay_s, max_retries=overload_max_retries
            )
        return RetryDecision(isinstance(exc, TransientLLMError))

    return classify


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
