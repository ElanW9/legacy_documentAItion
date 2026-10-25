"""OpenRouterClient against httpx.MockTransport (no network)."""

from __future__ import annotations

import json

import httpx
import pytest

from docgen.config import Settings
from docgen.llm import make_llm_client
from docgen.llm.base import (
    LLMError,
    OverloadedError,
    QuotaExhaustedError,
    RateLimitError,
    TransientLLMError,
)
from docgen.llm.openrouter import OpenRouterClient
from docgen.llm.schema import inline_schema
from docgen.models import UnitDoc

API_KEY = "fake-openrouter-key-for-tests"
NOW = 1_800_000_000.0


def _completion(text: str | None = '{"ok": true}', *, finish="stop", **extra) -> dict:
    return {
        "id": "gen-1",
        "model": "vendor/model-20260101:free",
        "choices": [
            {"message": {"role": "assistant", "content": text}, "finish_reason": finish, **extra}
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 25, "total_tokens": 125},
    }


def _error(code: int, message: str = "boom", **metadata) -> dict:
    err: dict = {"code": code, "message": message}
    if metadata:
        err["metadata"] = metadata
    return {"error": err}


def _client(
    outcomes: list, sleeps: list[float] | None = None, **settings
) -> tuple[OpenRouterClient, list[httpx.Request]]:
    """`outcomes`: httpx.Response, an exception to raise, or (status, json, headers)."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        out = outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        status, body, headers = out if len(out) == 3 else (*out, {})
        return httpx.Response(status, json=body, headers=headers)

    s = Settings(
        _env_file=None,
        llm_provider="openrouter",
        openrouter_api_key=API_KEY,
        openrouter_model="vendor/model:free",
        llm_backoff_base_s=1.0,
        **settings,
    )
    sleep = sleeps.append if sleeps is not None else (lambda _s: None)
    client = OpenRouterClient(
        s,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=sleep,
        clock=lambda: NOW,
    )
    return client, requests


def _call(c: OpenRouterClient):
    return c.generate_json(system="SYS", user="USER", response_model=UnitDoc)


def test_requests_strict_schema_and_reports_usage():
    c, reqs = _client([(200, _completion())])
    r = _call(c)
    req = reqs[0]
    body = json.loads(req.content)
    assert str(req.url) == "https://openrouter.ai/api/v1/chat/completions"
    assert req.headers["authorization"] == f"Bearer {API_KEY}"
    assert body["model"] == "vendor/model:free"
    assert body["messages"] == [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "USER"},
    ]
    fmt = body["response_format"]
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"] == {
        "name": "UnitDoc",
        "strict": True,
        "schema": inline_schema(UnitDoc),
    }
    # Only endpoints that honour the schema; never a silent fallback to plain text.
    assert body["provider"] == {"require_parameters": True}
    assert "temperature" not in body  # model default unless configured
    assert (r.text, r.input_tokens, r.output_tokens, r.attempts) == ('{"ok": true}', 100, 25, 1)
    assert r.model == "vendor/model-20260101:free"


def test_temperature_is_passed_when_configured():
    c, reqs = _client([(200, _completion())], llm_temperature=0.2)
    _call(c)
    assert json.loads(reqs[0].content)["temperature"] == 0.2


def test_factory_builds_openrouter_client_and_requires_its_settings():
    s = Settings(_env_file=None, llm_provider="openrouter")
    with pytest.raises(Exception, match=r"OPENROUTER_API_KEY.*OPENROUTER_MODEL"):
        make_llm_client(s)
    s = Settings(
        _env_file=None, llm_provider="openrouter", openrouter_api_key=API_KEY, openrouter_model="m"
    )
    assert isinstance(make_llm_client(s), OpenRouterClient)


def test_per_minute_429_is_retried_honouring_retry_after():
    sleeps: list[float] = []
    c, _ = _client(
        [(429, _error(429, "Rate limit exceeded"), {"retry-after": "7"}), (200, _completion())],
        sleeps,
    )
    assert _call(c).attempts == 2
    assert sleeps and sleeps[0] >= 7.0


def test_429_with_a_near_reset_waits_until_the_reset():
    sleeps: list[float] = []
    reset_ms = str(int((NOW + 20) * 1000))
    c, _ = _client(
        [(429, _error(429), {"x-ratelimit-reset": reset_ms}), (200, _completion())], sleeps
    )
    _call(c)
    assert sleeps[0] >= 20.0


def test_daily_quota_429_is_not_retried():
    reset_ms = str(int((NOW + 5 * 3600) * 1000))
    c, reqs = _client([(429, _error(429, "Rate limit exceeded"), {"x-ratelimit-reset": reset_ms})])
    with pytest.raises(QuotaExhaustedError, match=r"daily quota exhausted, resets in 5\.0h"):
        _call(c)
    assert len(reqs) == 1


def test_503_no_provider_is_treated_as_overload():
    sleeps: list[float] = []
    errs = [(503, _error(503, "No available provider")) for _ in range(3)]
    c, reqs = _client(errs, sleeps, llm_overload_min_delay_s=45, llm_overload_max_retries=2)
    with pytest.raises(OverloadedError, match="503"):
        _call(c)
    assert len(reqs) == 3 and all(s >= 45 for s in sleeps)


def test_502_and_timeouts_are_transient_and_retried():
    c, _ = _client(
        [
            (502, _error(502, "upstream down", provider_name="Nvidia")),
            httpx.ReadTimeout("slow"),
            (200, _completion()),
        ]
    )
    assert _call(c).attempts == 3


def test_error_inside_a_200_body_is_detected():
    c, _ = _client(
        [(200, _error(502, "provider failed", provider_name="Nvidia"))], llm_max_retries=0
    )
    with pytest.raises(TransientLLMError, match=r"502: provider failed \(provider: Nvidia\)"):
        _call(c)


def test_error_inside_the_choice_is_retried():
    bad = _completion("{partial", finish="error", error={"code": 502, "message": "cut"})
    c, _ = _client([(200, bad), (200, _completion())])
    assert _call(c).attempts == 2


def test_client_errors_are_not_retried():
    c, reqs = _client([(400, _error(400, "invalid schema"))])
    with pytest.raises(LLMError, match="400: invalid schema") as exc:
        _call(c)
    assert not isinstance(exc.value, TransientLLMError | RateLimitError)
    assert len(reqs) == 1


def test_truncated_answer_is_an_error():
    c, _ = _client([(200, _completion('{"purpose": "x', finish="length"))])
    with pytest.raises(LLMError, match="cut off"):
        _call(c)


def test_empty_answer_is_an_error():
    c, _ = _client([(200, _completion(None, finish="stop"))])
    with pytest.raises(LLMError, match="no text"):
        _call(c)


def test_api_key_never_appears_in_error_messages():
    c, _ = _client([(401, _error(401, "No auth credentials found"))])
    with pytest.raises(LLMError) as exc:
        _call(c)
    assert API_KEY not in str(exc.value) and API_KEY not in repr(exc.value)
