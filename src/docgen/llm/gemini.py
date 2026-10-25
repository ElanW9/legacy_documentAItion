"""Gemini implementation of `LLMClient` (google-genai SDK).

Structured output: the request sets `response_mime_type="application/json"` and
`response_json_schema` (verified against google-genai 2.33.0 and the Gemini
structured-output docs). The model's JSON is still validated by the caller with
pydantic, because the API does not enforce every keyword (e.g. `minLength`).

Retries are ours, not the SDK's (the SDK does not retry unless `retry_options`
is set): 429 and 5xx/timeouts back off with full jitter, honouring the server's
RetryInfo delay. 503 ("model is overloaded") is retried at most
`llm_overload_max_retries` times, waiting at least `llm_overload_min_delay_s`
each time: overload spikes last minutes, and on the free tier every rejected
request still counts against the daily quota. A daily quota is not retried,
since waiting minutes would not help. Error messages carry the HTTP status and
Google's error message only; the API key is never part of a message.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from typing import Any

import httpx
from google import genai
from google.genai import errors, types
from pydantic import BaseModel

from docgen.config import Settings
from docgen.llm.base import (
    LLMError,
    LLMResponse,
    OverloadedError,
    QuotaExhaustedError,
    RateLimitError,
    TransientLLMError,
    make_classifier,
)
from docgen.llm.schema import inline_schema
from docgen.retry import RetryPolicy, call_with_retry

log = logging.getLogger(__name__)

_RETRY_DELAY_RE = re.compile(r"^(\d+(?:\.\d+)?)s$")


def _details(exc: errors.APIError) -> list[dict[str, Any]]:
    d = exc.details if isinstance(exc.details, dict) else {}
    inner = d.get("error", d)
    out = inner.get("details", []) if isinstance(inner, dict) else []
    return [x for x in out if isinstance(x, dict)]


def _retry_delay_s(exc: errors.APIError) -> float | None:
    for d in _details(exc):
        if str(d.get("@type", "")).endswith("google.rpc.RetryInfo"):
            m = _RETRY_DELAY_RE.match(str(d.get("retryDelay", "")))
            if m:
                return float(m.group(1))
    return None


def _is_daily_quota(exc: errors.APIError) -> bool:
    for d in _details(exc):
        for v in d.get("violations", []) or []:
            if isinstance(v, dict) and "PerDay" in str(v.get("quotaId", "")):
                return True
    return False


def translate_error(exc: Exception) -> Exception:
    """Map SDK/transport exceptions onto the provider-neutral LLM errors."""
    if isinstance(exc, errors.APIError):
        summary = f"Gemini API error {exc.code} {exc.status or ''}: {exc.message or ''}".strip()
        if exc.code == 429:
            if _is_daily_quota(exc):
                return QuotaExhaustedError(summary + " (daily quota exhausted; not retrying)")
            return RateLimitError(summary, retry_after_s=_retry_delay_s(exc))
        if exc.code == 503:
            return OverloadedError(summary)
        if exc.code in (408, 500, 502, 504):
            return TransientLLMError(summary)
        return LLMError(summary)
    if isinstance(exc, httpx.TimeoutException | httpx.NetworkError):
        return TransientLLMError(f"Gemini transport error: {type(exc).__name__}")
    return exc


class GeminiClient:
    def __init__(
        self,
        settings: Settings,
        *,
        client: Any | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        settings.require_llm()
        assert settings.gemini_model is not None and settings.gemini_api_key is not None
        self._model = settings.gemini_model
        self._temperature = settings.llm_temperature
        self._thinking_level = settings.gemini_thinking_level
        self._policy = RetryPolicy(
            max_retries=settings.llm_max_retries,
            base_s=settings.llm_backoff_base_s,
            cap_s=settings.llm_backoff_max_s,
        )
        self._classify = make_classifier(
            settings.llm_overload_min_delay_s, settings.llm_overload_max_retries
        )
        self._sleep = sleep
        self._client = client or genai.Client(
            api_key=settings.gemini_api_key.get_secret_value(),
            http_options=types.HttpOptions(timeout=int(settings.llm_timeout_s * 1000)),
        )

    @property
    def model(self) -> str:
        return self._model

    def _config(self, system: str, response_model: type[BaseModel]) -> types.GenerateContentConfig:
        kwargs: dict[str, Any] = {
            "system_instruction": system,
            "response_mime_type": "application/json",
            "response_json_schema": inline_schema(response_model),
        }
        if self._temperature is not None:
            kwargs["temperature"] = self._temperature
        if self._thinking_level is not None:
            kwargs["thinking_config"] = types.ThinkingConfig(
                thinking_level=types.ThinkingLevel(self._thinking_level.upper())
            )
        return types.GenerateContentConfig(**kwargs)

    def generate_json(
        self, *, system: str, user: str, response_model: type[BaseModel]
    ) -> LLMResponse:
        config = self._config(system, response_model)

        def once() -> Any:
            try:
                return self._client.models.generate_content(
                    model=self._model, contents=user, config=config
                )
            except Exception as exc:
                translated = translate_error(exc)
                if translated is exc:
                    raise
                raise translated from None  # SDK exception may carry request details

        started = time.monotonic()
        response, attempts = call_with_retry(
            once,
            classify=self._classify,
            policy=self._policy,
            what="Gemini call",
            sleep=self._sleep,
        )
        latency = time.monotonic() - started

        text = response.text
        if not text:
            reason = None
            if response.candidates:
                reason = response.candidates[0].finish_reason
            block = getattr(response.prompt_feedback, "block_reason", None)
            raise LLMError(f"Gemini returned no text (finish_reason={reason}, block={block})")
        usage = response.usage_metadata
        return LLMResponse(
            text=text,
            model=response.model_version or self._model,
            input_tokens=usage.prompt_token_count if usage else None,
            output_tokens=(
                (usage.candidates_token_count or 0) + (usage.thoughts_token_count or 0)
                if usage
                else None
            ),
            latency_s=latency,
            attempts=attempts,
        )
