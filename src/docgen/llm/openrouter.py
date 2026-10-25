"""OpenRouter implementation of `LLMClient` (OpenAI-compatible chat completions).

Plain httpx, no SDK. Verified against the OpenRouter API docs (2026-10):

- `POST {base_url}/chat/completions` with `Authorization: Bearer <key>`.
- Structured output: `response_format: {type: json_schema, json_schema: {name,
  strict: true, schema}}`. Support differs per endpoint, so the request sets
  `provider.require_parameters: true`: OpenRouter then routes only to endpoints
  that honour the schema instead of silently ignoring it. The answer is still
  validated with pydantic by the caller.
- Errors are `{"error": {"code", "message", "metadata"}}`, and can arrive in a
  200 response (top level or in `choices[0].error`), so the body is checked
  even when the status is 200.
- 429: free models allow N requests per minute and per UTC day. The docs do not
  say how to tell the two apart; when the `X-RateLimit-Reset` header is more
  than `_DAILY_RESET_THRESHOLD_S` away, the limit is treated as the daily one
  (QuotaExhaustedError, not retried).
- 503 means "no available provider meets your routing requirements"; for a
  free model with one provider that is effectively overload, so it gets the
  same few, long retries as Gemini's 503. 502 (model down) and 408 are
  transient.

Error messages carry the HTTP status, OpenRouter's message and the provider
name only; the API key is only ever in the request header.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

import httpx
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

# A rate limit that resets further away than this is the daily one.
_DAILY_RESET_THRESHOLD_S = 15 * 60


def _float(value: str | None) -> float | None:
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


def _reset_in_s(headers: httpx.Headers, now: float) -> float | None:
    """Seconds until X-RateLimit-Reset (an epoch timestamp, ms or s), if present."""
    reset = _float(headers.get("x-ratelimit-reset"))
    if reset is None:
        return None
    if reset > 1e12:  # milliseconds
        reset /= 1000
    return max(0.0, reset - now)


def api_error(
    code: int, error: dict[str, Any], headers: httpx.Headers | None = None, *, now: float
) -> LLMError:
    """Map an OpenRouter error object onto the provider-neutral LLM errors."""
    metadata = error.get("metadata") if isinstance(error.get("metadata"), dict) else {}
    provider = metadata.get("provider_name") if metadata else None
    summary = f"OpenRouter API error {code}: {error.get('message') or ''}".strip()
    if provider:
        summary += f" (provider: {provider})"
    if code == 429:
        headers = headers or httpx.Headers()
        reset_in = _reset_in_s(headers, now)
        if reset_in is not None and reset_in > _DAILY_RESET_THRESHOLD_S:
            hours = reset_in / 3600
            return QuotaExhaustedError(
                f"{summary} (daily quota exhausted, resets in {hours:.1f}h; not retrying)"
            )
        retry_after = _float(headers.get("retry-after"))
        return RateLimitError(summary, retry_after_s=retry_after or reset_in)
    if code == 503:
        return OverloadedError(summary)
    if code in (408, 500, 502, 504):
        return TransientLLMError(summary)
    return LLMError(summary)


class OpenRouterClient:
    def __init__(
        self,
        settings: Settings,
        *,
        http: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        settings.require_llm()
        assert settings.openrouter_model is not None and settings.openrouter_api_key is not None
        self._model = settings.openrouter_model
        self._temperature = settings.llm_temperature
        self._url = settings.openrouter_base_url.rstrip("/") + "/chat/completions"
        self._policy = RetryPolicy(
            max_retries=settings.llm_max_retries,
            base_s=settings.llm_backoff_base_s,
            cap_s=settings.llm_backoff_max_s,
        )
        self._classify = make_classifier(
            settings.llm_overload_min_delay_s, settings.llm_overload_max_retries
        )
        self._sleep = sleep
        self._clock = clock
        self._http = http or httpx.Client(timeout=settings.llm_timeout_s)
        self._headers = {
            "Authorization": f"Bearer {settings.openrouter_api_key.get_secret_value()}",
            "Content-Type": "application/json",
        }

    @property
    def model(self) -> str:
        return self._model

    def _body(self, system: str, user: str, response_model: type[BaseModel]) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": response_model.__name__,
                    "strict": True,
                    "schema": inline_schema(response_model),
                },
            },
            "provider": {"require_parameters": True},
        }
        if self._temperature is not None:
            body["temperature"] = self._temperature
        return body

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = self._http.post(self._url, json=body, headers=self._headers)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise TransientLLMError(f"OpenRouter transport error: {type(exc).__name__}") from None
        try:
            data = resp.json()
        except ValueError:
            data = None
        if not isinstance(data, dict):
            if resp.is_success:
                raise TransientLLMError(f"OpenRouter returned a non-JSON {resp.status_code} body")
            data = {"error": {"message": resp.reason_phrase}}
        error = data.get("error")
        if isinstance(error, dict) or not resp.is_success:
            error = error if isinstance(error, dict) else {}
            code = error.get("code") if isinstance(error.get("code"), int) else resp.status_code
            raise api_error(code, error, resp.headers, now=self._clock())
        return data

    def _parse(self, data: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """Return (text, data) from a chat completion, raising for in-band errors."""
        choices = data.get("choices") or []
        choice = choices[0] if choices and isinstance(choices[0], dict) else {}
        if isinstance(choice.get("error"), dict):
            err = choice["error"]
            code = err.get("code") if isinstance(err.get("code"), int) else 502
            raise api_error(code, err, now=self._clock())
        finish = choice.get("finish_reason")
        text = (choice.get("message") or {}).get("content")
        if finish == "length":
            raise LLMError("OpenRouter answer was cut off (finish_reason=length)")
        if not text:
            native = choice.get("native_finish_reason")
            raise LLMError(f"OpenRouter returned no text (finish_reason={finish}/{native})")
        return text, data

    def generate_json(
        self, *, system: str, user: str, response_model: type[BaseModel]
    ) -> LLMResponse:
        body = self._body(system, user, response_model)
        started = time.monotonic()
        (text, data), attempts = call_with_retry(
            lambda: self._parse(self._post(body)),
            classify=self._classify,
            policy=self._policy,
            what="OpenRouter call",
            sleep=self._sleep,
        )
        latency = time.monotonic() - started
        usage = data.get("usage") or {}
        return LLMResponse(
            text=text,
            model=data.get("model") or self._model,
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
            latency_s=latency,
            attempts=attempts,
        )
