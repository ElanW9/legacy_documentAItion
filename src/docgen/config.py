"""Application settings.

Loaded from real environment variables (CI) and, for local development, from a
`.env` file. Every field is optional at the type level because different
commands need different subsets (a `--dry-run` needs no Confluence credentials,
the fake LLM needs no API key). Each command calls `require_*()` at startup,
which fails fast with the full list of missing variables before any work is
done. Secret values are `SecretStr` and are never included in messages.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class SettingsError(RuntimeError):
    """Configuration is missing or invalid. Message never contains secret values."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    # --- LLM -----------------------------------------------------------------
    llm_provider: Literal["gemini", "openrouter", "fake"] = "gemini"
    gemini_api_key: SecretStr | None = None
    gemini_model: str | None = None
    # Second provider (OpenAI-compatible API), for comparison and as a fallback.
    openrouter_api_key: SecretStr | None = None
    openrouter_model: str | None = None
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    llm_max_retries: int = Field(default=5, ge=0)
    llm_backoff_base_s: float = Field(default=2.0, gt=0)
    llm_backoff_max_s: float = Field(default=60.0, gt=0)
    # 503 "model is overloaded": spikes last minutes and, on a free tier, every
    # rejected request still counts against the daily quota. Few, long waits.
    llm_overload_min_delay_s: float = Field(default=90.0, ge=0)
    llm_overload_max_retries: int = Field(default=2, ge=0)
    llm_timeout_s: float = Field(default=180.0, gt=0)
    # None = model default. Google recommends the default (1.0) for Gemini 3 models.
    llm_temperature: float | None = Field(default=None, ge=0, le=2)
    gemini_thinking_level: Literal["minimal", "low", "medium", "high"] | None = None
    # Packages whose body exceeds this many lines are documented per procedure.
    chunk_threshold_lines: int = Field(default=150, ge=1)
    # Local cache of validated LLM answers, keyed by a hash of the full request.
    cache_dir: Path = Path(".cache/docgen")

    # --- Confluence ----------------------------------------------------------
    publish_mode: Literal["rest", "mcp"] = "rest"
    atlassian_email: SecretStr | None = None
    atlassian_token: SecretStr | None = None
    confluence_site: str | None = None  # e.g. "example.atlassian.net"
    confluence_space: str | None = None  # space key
    confluence_parent_id: str | None = None
    confluence_timeout_s: float = Field(default=30.0, gt=0)

    # --- Repository ----------------------------------------------------------
    plsql_root: Path = Path("plsql")
    # Optional, e.g. "https://github.com/org/repo/blob"; enables evidence links.
    source_base_url: str | None = None

    @field_validator("confluence_site")
    @classmethod
    def _normalise_site(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        v = v.strip().removeprefix("https://").removeprefix("http://").rstrip("/")
        return v.removesuffix("/wiki")

    @field_validator("llm_provider", "publish_mode", "gemini_thinking_level", mode="before")
    @classmethod
    def _lowercase_choice(cls, v: object) -> object:
        return v.strip().lower() if isinstance(v, str) else v

    @field_validator(
        "gemini_model",
        "openrouter_model",
        "confluence_space",
        "confluence_parent_id",
        "source_base_url",
    )
    @classmethod
    def _blank_is_none(cls, v: str | None) -> str | None:
        return v.strip() if v and v.strip() else None

    @model_validator(mode="before")
    @classmethod
    def _drop_blank_values(cls, data: object) -> object:
        """`KEY=` (as in .env.example) means "not set": fall back to the default."""
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if not (isinstance(v, str) and not v.strip())}
        return data

    # ------------------------------------------------------------------ checks
    def _require(self, purpose: str, names: list[str]) -> None:
        missing = [n.upper() for n in names if getattr(self, n) is None]
        if missing:
            raise SettingsError(
                f"Missing required configuration for {purpose}: {', '.join(missing)}. "
                "Set them as environment variables (CI) or in .env (local); "
                "see .env.example."
            )

    def require_llm(self) -> None:
        if self.llm_provider == "gemini":
            self._require("the Gemini LLM", ["gemini_api_key", "gemini_model"])
        elif self.llm_provider == "openrouter":
            self._require("the OpenRouter LLM", ["openrouter_api_key", "openrouter_model"])

    def require_publish(self) -> None:
        self._require(
            f"publishing to Confluence (PUBLISH_MODE={self.publish_mode})",
            [
                "atlassian_email",
                "atlassian_token",
                "confluence_site",
                "confluence_space",
                "confluence_parent_id",
            ],
        )

    @classmethod
    def load(cls) -> Settings:
        """Construct from the environment / .env; invalid values raise SettingsError.

        pydantic's own message includes the offending input value, so it is
        rebuilt here from field names and reasons only.
        """
        try:
            return cls()
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in e['loc']).upper()}: {e['msg']}"
                for e in exc.errors(include_url=False, include_input=False)
            )
            raise SettingsError(f"Invalid configuration: {problems}") from None

    @property
    def confluence_base_url(self) -> str:
        if self.confluence_site is None:
            raise SettingsError("CONFLUENCE_SITE is not set")
        return f"https://{self.confluence_site}/wiki"
