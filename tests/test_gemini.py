"""GeminiClient against a stub of the google-genai client (no network)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from google.genai import errors

from docgen.config import Settings
from docgen.llm.base import LLMError, RateLimitError
from docgen.llm.gemini import GeminiClient
from docgen.llm.schema import inline_schema
from docgen.models import UnitDoc

API_KEY = "fake-gemini-key-for-tests"


class StubModels:
    def __init__(self, outcomes: list) -> None:
        self.outcomes = outcomes
        self.calls: list[dict] = []

    def generate_content(self, *, model, contents, config):
        self.calls.append({"model": model, "contents": contents, "config": config})
        out = self.outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


def _response(text: str | None, *, prompt_tokens=100, out_tokens=20, thoughts=5):
    return SimpleNamespace(
        text=text,
        model_version="test-model-001",
        candidates=[SimpleNamespace(finish_reason="MAX_TOKENS")],
        prompt_feedback=None,
        usage_metadata=SimpleNamespace(
            prompt_token_count=prompt_tokens,
            candidates_token_count=out_tokens,
            thoughts_token_count=thoughts,
        ),
    )


def _api_error(code: int, status: str, details: list | None = None) -> errors.APIError:
    body = {"error": {"code": code, "status": status, "message": "quota", "details": details or []}}
    return errors.APIError(code, body)


def _client(outcomes: list, sleeps: list[float] | None = None, **settings) -> GeminiClient:
    s = Settings(
        _env_file=None,
        gemini_api_key=API_KEY,
        gemini_model="test-model",
        llm_backoff_base_s=1.0,
        **settings,
    )
    stub = SimpleNamespace(models=StubModels(outcomes))
    sleep = sleeps.append if sleeps is not None else (lambda _s: None)
    return GeminiClient(s, client=stub, sleep=sleep)


def _stub(c: GeminiClient) -> StubModels:
    return c._client.models  # type: ignore[attr-defined]


def test_requests_structured_json_and_reports_usage():
    c = _client([_response('{"ok": true}')])
    r = c.generate_json(system="SYS", user="USER", response_model=UnitDoc)
    call = _stub(c).calls[0]
    cfg = call["config"]
    assert call["model"] == "test-model" and call["contents"] == "USER"
    assert cfg.system_instruction == "SYS"
    assert cfg.response_mime_type == "application/json"
    assert cfg.response_json_schema == inline_schema(UnitDoc)
    assert cfg.temperature is None  # model default unless configured
    assert (r.text, r.input_tokens, r.output_tokens, r.attempts) == ('{"ok": true}', 100, 25, 1)


def test_optional_generation_settings_are_passed():
    c = _client([_response("{}")], llm_temperature=0.5, gemini_thinking_level="low")
    c.generate_json(system="s", user="u", response_model=UnitDoc)
    cfg = _stub(c).calls[0]["config"]
    assert cfg.temperature == 0.5
    assert cfg.thinking_config.thinking_level.value == "LOW"


def test_429_is_retried_honouring_retry_info():
    retry_info = {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "7s"}
    sleeps: list[float] = []
    c = _client([_api_error(429, "RESOURCE_EXHAUSTED", [retry_info]), _response("{}")], sleeps)
    r = c.generate_json(system="s", user="u", response_model=UnitDoc)
    assert r.attempts == 2
    assert sleeps and sleeps[0] >= 7.0


def test_5xx_is_retried_then_gives_up():
    errs = [_api_error(503, "UNAVAILABLE") for _ in range(3)]
    c = _client(errs, [], llm_max_retries=2)
    with pytest.raises(LLMError, match="503"):
        c.generate_json(system="s", user="u", response_model=UnitDoc)
    assert len(_stub(c).calls) == 3


def test_daily_quota_is_not_retried():
    quota = {
        "@type": "type.googleapis.com/google.rpc.QuotaFailure",
        "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}],
    }
    c = _client([_api_error(429, "RESOURCE_EXHAUSTED", [quota])], [])
    with pytest.raises(LLMError, match="daily quota") as exc:
        c.generate_json(system="s", user="u", response_model=UnitDoc)
    assert not isinstance(exc.value, RateLimitError)
    assert len(_stub(c).calls) == 1


def test_client_errors_are_not_retried_and_do_not_leak_key():
    c = _client([_api_error(400, "INVALID_ARGUMENT")], [])
    with pytest.raises(LLMError) as exc:
        c.generate_json(system="s", user="u", response_model=UnitDoc)
    assert "400" in str(exc.value) and API_KEY not in str(exc.value)
    assert exc.value.__cause__ is None and exc.value.__suppress_context__


def test_empty_response_is_an_error():
    c = _client([_response(None)])
    with pytest.raises(LLMError, match="finish_reason=MAX_TOKENS"):
        c.generate_json(system="s", user="u", response_model=UnitDoc)


def test_requires_key_and_model():
    with pytest.raises(Exception, match="GEMINI_API_KEY"):
        GeminiClient(Settings(_env_file=None, gemini_model="m"), client=object())


def test_inline_schema_has_no_refs_or_titles():
    schema = inline_schema(UnitDoc)
    text = str(schema)
    assert "$ref" not in text and "$defs" not in text and "'title'" not in text
    rule = schema["properties"]["business_rules"]["items"]
    assert rule["properties"]["evidence_lines"]["prefixItems"] == [
        {"type": "integer"},
        {"type": "integer"},
    ]
    assert rule["additionalProperties"] is False
