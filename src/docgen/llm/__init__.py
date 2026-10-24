"""LLM clients behind a provider-neutral interface."""

from docgen.config import Settings
from docgen.llm.base import LLMClient, LLMError, LLMResponse, RateLimitError, TransientLLMError
from docgen.llm.fake import FakeLLMClient


def make_llm_client(settings: Settings) -> LLMClient:
    """Build the configured client. Fails fast (SettingsError) on missing config."""
    settings.require_llm()
    if settings.llm_provider == "fake":
        return FakeLLMClient()
    from docgen.llm.gemini import GeminiClient  # keep the SDK import off the offline path

    return GeminiClient(settings)


__all__ = [
    "FakeLLMClient",
    "LLMClient",
    "LLMError",
    "LLMResponse",
    "RateLimitError",
    "TransientLLMError",
    "make_llm_client",
]
