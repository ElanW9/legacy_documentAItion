"""The run: changed files -> packages -> extract -> generate -> check -> render -> publish.

Packages are processed sequentially (free-tier rate limits) and isolated: a
failure in one package is recorded and the run continues. The caller exits
non-zero if any package failed.

Skipping: each page stores a cache key (source hash + prompt fingerprint +
model + renderer version + chunking threshold) in a content property. If the
existing page has the same key, the package is skipped without an LLM call.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html import unescape
from pathlib import Path
from typing import Literal

from docgen.checks import CheckResult, check_page_coverage, run_checks
from docgen.config import Settings
from docgen.extract import (
    PackageFiles,
    extract_package,
    find_all_packages,
    known_package_names,
    pair_package_files,
)
from docgen.extract.package import PLSQL_SUFFIXES
from docgen.generate import (
    GenerationError,
    LLMCallRecord,
    Mode,
    ResponseCache,
    generate_package_doc,
)
from docgen.llm.base import LLMClient
from docgen.prompts import Prompts
from docgen.publish import Publisher
from docgen.render import LABELS, RENDER_VERSION, PageMeta, page_title, render_page

log = logging.getLogger(__name__)

Status = Literal["published", "skipped", "failed"]


@dataclass
class PackageOutcome:
    label: str
    name: str | None = None
    status: Status = "failed"
    action: str | None = None  # created / updated / dry_run
    url: str | None = None
    mode: Mode | None = None
    checks: CheckResult | None = None
    calls: list[LLMCallRecord] = field(default_factory=list)
    error: str | None = None


@dataclass
class RunResult:
    commit: str
    model: str
    prompt_version: str
    dry_run: bool
    started: datetime
    finished: datetime | None = None
    outcomes: list[PackageOutcome] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    ignored: list[str] = field(default_factory=list)

    @property
    def failed(self) -> list[PackageOutcome]:
        return [o for o in self.outcomes if o.status == "failed"]


def detect_commit(explicit: str | None = None) -> str:
    if explicit:
        return explicit
    if sha := os.environ.get("GITHUB_SHA"):
        return sha
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True, timeout=10
        )
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def cache_key(*, source_hash: str, prompts: Prompts, model: str, chunk_threshold: int) -> str:
    h = hashlib.sha256()
    for part in (source_hash, prompts.fingerprint, model, RENDER_VERSION, str(chunk_threshold)):
        h.update(part.encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()


@dataclass(frozen=True)
class Selection:
    packages: list[PackageFiles]
    deleted: list[str]  # PL/SQL paths that no longer exist (page left in place)
    ignored: list[str]  # changed paths that are not PL/SQL package files


def select_packages(changed: Iterable[str], *, root: Path, plsql_root: Path) -> Selection:
    """Map a list of changed paths (relative to `root`) onto package file pairs."""
    plsql_abs = (root / plsql_root).resolve()
    candidates: list[Path] = []
    deleted: list[str] = []
    ignored: list[str] = []
    for raw in changed:
        rel = raw.strip().replace("\\", "/")
        if not rel or rel.startswith("#"):
            continue
        path = (root / rel).resolve()
        if path.suffix.lower() not in PLSQL_SUFFIXES or not path.is_relative_to(plsql_abs):
            ignored.append(rel)
            continue
        candidates.append(path)
    pairs = pair_package_files(candidates)
    present = {p for pf in pairs for p in (pf.spec, pf.body) if p}
    for path in candidates:
        if path not in present and not path.exists():
            deleted.append(path.relative_to(root.resolve()).as_posix())
    return Selection(pairs, sorted(set(deleted)), ignored)


def all_packages(*, root: Path, plsql_root: Path) -> Selection:
    return Selection(find_all_packages(root / plsql_root), [], [])


def _rel_label(files: PackageFiles, root: Path) -> str:
    label = Path(files.label).resolve()
    try:
        return label.relative_to(root.resolve()).as_posix()
    except ValueError:
        return label.as_posix()


def process_package(
    files: PackageFiles,
    *,
    root: Path,
    settings: Settings,
    client: LLMClient,
    prompts: Prompts,
    publisher: Publisher,
    cache: ResponseCache,
    known: set[str],
    commit: str,
    force: bool,
    now: datetime,
) -> PackageOutcome:
    outcome = PackageOutcome(label=_rel_label(files, root))
    try:
        extracted = extract_package(files, root=root, known_packages=known)
        facts = extracted.facts
        outcome.name = facts.name
        title = page_title(facts.name)
        key = cache_key(
            source_hash=facts.source_hash,
            prompts=prompts,
            model=client.model,
            chunk_threshold=settings.chunk_threshold_lines,
        )
        existing = publisher.get_page_state(title)
        if existing is not None and existing.cache_key == key and not force:
            outcome.status, outcome.url = "skipped", None
            log.info(
                "%s: unchanged (cache key matches page %s); skipped", facts.name, existing.page_id
            )
            return outcome

        gen = generate_package_doc(
            extracted,
            client,
            prompts,
            chunk_threshold_lines=settings.chunk_threshold_lines,
            cache=cache,
        )
        outcome.mode, outcome.calls = gen.mode, gen.calls
        body_text = extracted.body.text if extracted.body else None
        checked = run_checks(facts, body_text, gen.doc)

        meta = PageMeta(
            commit_sha=commit,
            model=client.model,
            prompt_version=prompts.version,
            generated_at=now,
            source_base_url=settings.source_base_url,
        )
        page = render_page(facts, checked.doc, meta)
        checked.findings.extend(check_page_coverage(facts, _visible_text(page)))
        outcome.checks = checked
        page = render_page(facts, checked.doc, meta, quality_notes=checked.quality_notes())

        result = publisher.publish(
            title=title,
            body=page,
            metadata={
                "cache_key": key,
                "source_hash": facts.source_hash,
                "commit": commit,
                "model": client.model,
                "prompt_version": prompts.version,
                "generated_at": now.isoformat(timespec="seconds"),
            },
            labels=list(LABELS),
            existing=existing,
            version_message=f"docgen {commit[:12]} ({client.model}, prompt {prompts.version})",
        )
        outcome.status, outcome.action, outcome.url = "published", result.action, result.url
    except GenerationError as exc:
        outcome.calls = outcome.calls or exc.calls
        outcome.error = str(exc)
        log.error("%s: generation failed: %s", outcome.label, exc)
    except Exception as exc:  # isolate: one bad package must not stop the run
        outcome.error = f"{type(exc).__name__}: {exc}"
        log.error("%s: failed: %s", outcome.label, outcome.error)
        log.debug("traceback", exc_info=True)
    return outcome


def _visible_text(xhtml: str) -> str:
    return unescape(re.sub(r"<[^>]+>", " ", xhtml))


def run(
    selection: Selection,
    *,
    root: Path,
    settings: Settings,
    client: LLMClient,
    prompts: Prompts,
    publisher: Publisher,
    cache: ResponseCache,
    commit: str,
    dry_run: bool,
    force: bool = False,
    now: datetime | None = None,
) -> RunResult:
    now = now or datetime.now(UTC)
    result = RunResult(
        commit=commit,
        model=client.model,
        prompt_version=prompts.version,
        dry_run=dry_run,
        started=now,
        deleted=selection.deleted,
        ignored=selection.ignored,
    )
    known = known_package_names(root / settings.plsql_root)
    for files in selection.packages:
        result.outcomes.append(
            process_package(
                files,
                root=root,
                settings=settings,
                client=client,
                prompts=prompts,
                publisher=publisher,
                cache=cache,
                known=known,
                commit=commit,
                force=force,
                now=now,
            )
        )
    result.finished = datetime.now(UTC)
    return result


__all__ = [
    "PackageOutcome",
    "RunResult",
    "Selection",
    "all_packages",
    "cache_key",
    "detect_commit",
    "run",
    "select_packages",
]
