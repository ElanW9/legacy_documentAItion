from __future__ import annotations

import pytest
from pydantic import SecretStr

from docgen.config import Settings, SettingsError

ENV_VARS = [
    "GEMINI_API_KEY",
    "GEMINI_MODEL",
    "OPENROUTER_API_KEY",
    "OPENROUTER_MODEL",
    "LLM_PROVIDER",
    "ATLASSIAN_EMAIL",
    "ATLASSIAN_TOKEN",
    "CONFLUENCE_SITE",
    "CONFLUENCE_SPACE",
    "CONFLUENCE_PARENT_ID",
    "PUBLISH_MODE",
]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def make(**kw) -> Settings:
    return Settings(_env_file=None, **kw)


def test_constructible_without_env_file():
    s = make()
    assert s.publish_mode == "rest" and s.gemini_api_key is None


def test_reads_environment_variables(monkeypatch):
    monkeypatch.setenv("GEMINI_MODEL", "some-model")
    monkeypatch.setenv("CONFLUENCE_SITE", "https://example.atlassian.net/wiki/")
    s = make()
    assert s.gemini_model == "some-model"
    assert s.confluence_site == "example.atlassian.net"
    assert s.confluence_base_url == "https://example.atlassian.net/wiki"


def test_require_llm_lists_all_missing():
    with pytest.raises(SettingsError) as exc:
        make().require_llm()
    assert "GEMINI_API_KEY" in str(exc.value) and "GEMINI_MODEL" in str(exc.value)


def test_fake_provider_needs_no_key():
    make(llm_provider="fake").require_llm()


def test_require_publish_reports_missing_without_leaking_present_secrets():
    s = make(atlassian_token="super-secret-token-value", confluence_site="x.atlassian.net")
    with pytest.raises(SettingsError) as exc:
        s.require_publish()
    msg = str(exc.value)
    assert "ATLASSIAN_EMAIL" in msg and "CONFLUENCE_SPACE" in msg
    assert "ATLASSIAN_TOKEN" not in msg
    assert "super-secret-token-value" not in msg


def test_secrets_are_masked_in_repr_and_dump():
    s = make(gemini_api_key="AIza-not-a-real-key")
    assert isinstance(s.gemini_api_key, SecretStr)
    assert "AIza-not-a-real-key" not in repr(s)
    assert "AIza-not-a-real-key" not in str(s.model_dump())


def test_blank_values_count_as_missing():
    s = make(gemini_api_key="  ", gemini_model="")
    assert s.gemini_api_key is None and s.gemini_model is None


def test_env_example_copied_verbatim_is_valid(tmp_path, repo_root):
    env = tmp_path / ".env"
    env.write_text((repo_root / ".env.example").read_text(encoding="utf-8"), encoding="utf-8")
    s = Settings(_env_file=env)
    assert s.llm_provider == "gemini" and s.publish_mode == "rest"
    assert s.gemini_api_key is None and s.confluence_site is None


def test_env_example_has_no_values(repo_root):
    for line in (repo_root / ".env.example").read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            key, _, value = line.partition("=")
            assert value == "", f"{key} must be empty in .env.example"


def test_choice_values_are_case_insensitive(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "GEMINI")
    monkeypatch.setenv("PUBLISH_MODE", " Rest ")
    s = make()
    assert (s.llm_provider, s.publish_mode) == ("gemini", "rest")


def test_load_reports_invalid_values_without_echoing_them(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # no .env here
    monkeypatch.setenv("LLM_PROVIDER", "secret-looking-value")
    with pytest.raises(SettingsError) as exc:
        Settings.load()
    assert "LLM_PROVIDER" in str(exc.value)
    assert "secret-looking-value" not in str(exc.value)
