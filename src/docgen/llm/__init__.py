"""LLM clients behind a provider-neutral interface."""

from docgen.llm.base import LLMClient, LLMError, LLMResponse, RateLimitError, TransientLLMError
from docgen.llm.fake import FakeLLMClient

__all__ = [
    "FakeLLMClient",
    "LLMClient",
    "LLMError",
    "LLMResponse",
    "RateLimitError",
    "TransientLLMError",
]
