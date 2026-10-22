"""Publishers: Confluence REST (default), MCP, and a local dry-run writer."""

from __future__ import annotations

from pathlib import Path

from docgen.config import Settings
from docgen.publish.base import PROPERTY_KEY, PageState, Publisher, PublishError, PublishResult
from docgen.publish.dryrun import DryRunPublisher


def make_publisher(settings: Settings, *, dry_run: bool, out_dir: Path) -> Publisher:
    if dry_run:
        return DryRunPublisher(out_dir)
    if settings.publish_mode == "rest":
        from docgen.publish.rest import RestPublisher

        return RestPublisher(settings)
    raise PublishError("PUBLISH_MODE=mcp is not implemented yet")


__all__ = [
    "PROPERTY_KEY",
    "DryRunPublisher",
    "PageState",
    "PublishError",
    "PublishResult",
    "Publisher",
    "make_publisher",
]
