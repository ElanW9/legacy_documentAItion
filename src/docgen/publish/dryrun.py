"""Dry-run publisher: writes what would be published to a local directory."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from html import escape
from pathlib import Path

from docgen.publish.base import PageState, PublishResult

_PREVIEW = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>{title}</title>
<style>body{{font-family:sans-serif;max-width:1100px;margin:2em auto;padding:0 1em}}
table{{border-collapse:collapse}}td,th{{border:1px solid #ccc;padding:4px 8px;vertical-align:top}}
ac\:structured-macro{{display:block;border-left:4px solid #e2b203;padding:4px 8px;margin:4px 0}}
</style></head><body>
<p><em>Dry-run preview of Confluence storage format; macros are shown unstyled.</em></p>
<h1>{title}</h1>
{body}
</body></html>
"""


def _slug(title: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", title).strip("_").lower()


class DryRunPublisher:
    def __init__(self, out_dir: Path) -> None:
        self.out_dir = out_dir

    def get_page_state(self, title: str) -> PageState | None:
        return None  # dry runs never skip: always show what would be generated

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
        self.out_dir.mkdir(parents=True, exist_ok=True)
        slug = _slug(title)
        (self.out_dir / f"{slug}.storage.xml").write_text(body, encoding="utf-8")
        (self.out_dir / f"{slug}.html").write_text(
            _PREVIEW.format(title=escape(title), body=body), encoding="utf-8"
        )
        (self.out_dir / f"{slug}.meta.json").write_text(
            json.dumps(
                {"title": title, "labels": list(labels), "metadata": dict(metadata)}, indent=2
            ),
            encoding="utf-8",
        )
        return PublishResult(action="dry_run", title=title, url=str(self.out_dir / f"{slug}.html"))
