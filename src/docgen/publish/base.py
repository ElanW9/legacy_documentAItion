"""Publisher interface shared by the REST, MCP and dry-run implementations."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

# Content property holding docgen metadata (cache key, commit, ...) on each page.
PROPERTY_KEY = "docgen-metadata"


class PublishError(RuntimeError):
    """Publishing failed. Messages never include credentials."""


@dataclass(frozen=True)
class PageState:
    """What docgen needs to know about an already-published page."""

    page_id: str
    title: str
    version: int
    metadata: Mapping[str, str]  # empty if the page has no docgen property
    property_id: str | None = None
    property_version: int | None = None

    @property
    def cache_key(self) -> str | None:
        return self.metadata.get("cache_key")


@dataclass(frozen=True)
class PublishResult:
    action: Literal["created", "updated", "dry_run"]
    title: str
    page_id: str | None = None
    url: str | None = None


class Publisher(Protocol):
    def get_page_state(self, title: str) -> PageState | None:
        """Return the existing page under the configured parent, or None."""
        ...

    def publish(
        self,
        *,
        title: str,
        body: str,
        metadata: Mapping[str, str],
        labels: Sequence[str],
        existing: PageState | None,
        version_message: str,
    ) -> PublishResult:
        """Create or update the page idempotently. Metadata is written last."""
        ...
