"""CLI: `python -m docgen run --files changed.txt --report report.md [--dry-run]`.

Exit codes: 0 = all packages succeeded, 1 = at least one package failed,
2 = configuration or usage error (nothing was processed).
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from docgen.config import Settings, SettingsError
from docgen.generate import ResponseCache
from docgen.llm import make_llm_client
from docgen.pipeline import all_packages, detect_commit, run, select_packages
from docgen.prompts import default_prompts
from docgen.publish import PublishError, make_publisher
from docgen.report import render_report

log = logging.getLogger("docgen")


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m docgen")
    sub = p.add_subparsers(dest="command", required=True)
    r = sub.add_parser("run", help="document changed packages and publish them")
    src = r.add_mutually_exclusive_group(required=True)
    src.add_argument("--files", type=Path, help="file with changed paths, one per line")
    src.add_argument("--all", action="store_true", help="process every package under PLSQL_ROOT")
    r.add_argument("--report", type=Path, required=True, help="write the Markdown report here")
    r.add_argument("--dry-run", action="store_true", help="write pages to --out, do not publish")
    r.add_argument("--out", type=Path, default=Path("out"), help="dry-run output directory")
    r.add_argument("--force", action="store_true", help="regenerate even if the page is current")
    r.add_argument("--no-cache", action="store_true", help="do not use the local LLM cache")
    r.add_argument("--commit", help="commit SHA to show on pages (default: GITHUB_SHA or HEAD)")
    r.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    # Request-level logs from HTTP libraries add noise, never needed for the report.
    for noisy in ("httpx", "httpcore", "google_genai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    root = Path.cwd().resolve()
    try:
        settings = Settings.load()
        settings.require_llm()
        if not args.dry_run:
            settings.require_publish()
        client = make_llm_client(settings)
        publisher = make_publisher(settings, dry_run=args.dry_run, out_dir=args.out)
    except (SettingsError, PublishError) as exc:
        log.error("%s", exc)
        return 2

    if args.all:
        selection = all_packages(root=root, plsql_root=settings.plsql_root)
    else:
        changed = args.files.read_text(encoding="utf-8").splitlines()
        selection = select_packages(changed, root=root, plsql_root=settings.plsql_root)

    result = run(
        selection,
        root=root,
        settings=settings,
        client=client,
        prompts=default_prompts(),
        publisher=publisher,
        cache=ResponseCache(None if args.no_cache else settings.cache_dir),
        commit=detect_commit(args.commit),
        dry_run=args.dry_run,
        force=args.force,
    )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(render_report(result), encoding="utf-8")
    log.info(
        "%d package(s), %d failed; report written to %s",
        len(result.outcomes),
        len(result.failed),
        args.report,
    )
    return 1 if result.failed else 0


if __name__ == "__main__":
    sys.exit(main())
