"""Eval CLI.

    python -m docgen.eval [run] [--packages ORD_PROC ...] [--label L] [--no-schema-in-prompt]
    python -m docgen.eval compare [RESULT.json ...]
    python -m docgen.eval rescore [RESULT.json ...]

`run` calls the configured (live) model, saves JSON to eval/results/ and prints
a table comparing it with earlier runs. `compare` only reads saved results;
`rescore` re-applies the current checks to saved model output (no LLM calls).
The local LLM cache is off by default so every run measures the model.
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from pathlib import Path

from docgen.config import Settings, SettingsError
from docgen.eval.compare import comparison_markdown
from docgen.eval.ground_truth import load_ground_truth
from docgen.eval.harness import rescore_run, run_eval
from docgen.eval.results import load_runs, save_run
from docgen.generate import ResponseCache
from docgen.llm import make_llm_client
from docgen.pipeline import detect_commit
from docgen.prompts import PROMPTS_DIR, load_prompts

log = logging.getLogger("docgen.eval")

RESULTS_DIR = Path("eval/results")
GROUND_TRUTH = Path("eval/ground_truth.yaml")


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m docgen.eval")
    sub = p.add_subparsers(dest="command")
    r = sub.add_parser("run", help="generate docs with the live model and score them (default)")
    r.add_argument("--packages", nargs="*", help="package names (default: all)")
    r.add_argument("--label", help="free-text label added to the run id")
    r.add_argument("--prompts", type=Path, default=PROMPTS_DIR, help="prompt directory")
    r.add_argument(
        "--no-schema-in-prompt",
        action="store_true",
        help="variant: omit <schema> from the user message (still enforced via the API)",
    )
    r.add_argument("--use-cache", action="store_true", help="reuse cached LLM answers")
    r.add_argument(
        "--resume",
        type=Path,
        help="complete an earlier run (result JSON): keep its successful packages, "
        "generate only failed ones; requires the same model and prompts",
    )
    r.add_argument("--results", type=Path, default=RESULTS_DIR)
    r.add_argument("--ground-truth", type=Path, default=GROUND_TRUTH)
    r.add_argument("-v", "--verbose", action="store_true")
    rs = sub.add_parser("rescore", help="re-score saved runs with the current checks")
    rs.add_argument("files", nargs="*", type=Path, help="result files (default: all in --results)")
    rs.add_argument("--results", type=Path, default=RESULTS_DIR)
    rs.add_argument("--ground-truth", type=Path, default=GROUND_TRUTH)
    c = sub.add_parser("compare", help="print the comparison table of saved runs")
    c.add_argument("files", nargs="*", type=Path, help="result files (default: all in --results)")
    c.add_argument("--results", type=Path, default=RESULTS_DIR)
    c.add_argument("--out", type=Path, help="also write the Markdown to this file")
    return p


def _commit_for_eval() -> str:
    """HEAD, marked "+dirty" when code or prompts have uncommitted changes."""
    commit = detect_commit()
    try:
        out = subprocess.run(
            ["git", "status", "--porcelain", "--", "src", "prompts", "eval/ground_truth.yaml"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return commit
    return f"{commit}+dirty" if out.stdout.strip() else commit


def _results_files(directory: Path) -> list[Path]:
    return sorted(directory.glob("*.json"))


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0].startswith("-"):
        argv.insert(0, "run")
    args = _parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "verbose", False) else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    for noisy in ("httpx", "httpcore", "google_genai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if args.command == "compare":
        runs = load_runs(args.files or _results_files(args.results))
        md = comparison_markdown(runs)
        if args.out:
            args.out.write_text(md + "\n", encoding="utf-8")
        print(md)
        return 0

    if args.command == "rescore":
        settings = Settings(_env_file=None)  # scoring needs no credentials
        gt = load_ground_truth(args.ground_truth)
        for path in args.files or _results_files(args.results):
            [old] = load_runs([path])
            new = rescore_run(old, gt, root=Path.cwd().resolve(), settings=settings)
            save_run(new, path.parent)
            log.info("rescored %s", path)
        print(comparison_markdown(load_runs(_results_files(args.results))))
        return 0

    try:
        settings = Settings.load()
        client = make_llm_client(settings)
    except SettingsError as exc:
        log.error("%s", exc)
        return 2
    prompts = load_prompts(args.prompts)
    if args.no_schema_in_prompt:
        prompts = prompts.without_schema_in_prompt()

    run = run_eval(
        load_ground_truth(args.ground_truth),
        root=Path.cwd().resolve(),
        settings=settings,
        client=client,
        prompts=prompts,
        cache=ResponseCache(settings.cache_dir if args.use_cache else None),
        commit=_commit_for_eval(),
        label=args.label,
        packages=args.packages,
        resume=load_runs([args.resume])[0] if args.resume else None,
    )
    path = save_run(run, args.results)
    log.info("saved %s", path)
    print(comparison_markdown(load_runs(_results_files(args.results))))
    return 1 if run.summary.failed else 0


if __name__ == "__main__":
    sys.exit(main())
