"""Markdown run report (written to a file and, in CI, to $GITHUB_STEP_SUMMARY).

Contains only run metadata, counts and error messages; never secret values.
"""

from __future__ import annotations

from docgen.pipeline import PackageOutcome, RunResult


def _cell(text: object) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _tokens(o: PackageOutcome) -> tuple[int, int]:
    return (
        sum(c.input_tokens or 0 for c in o.calls),
        sum(c.output_tokens or 0 for c in o.calls),
    )


def _status(o: PackageOutcome) -> str:
    if o.status == "published":
        return f"{o.action}"
    return {
        "skipped": "skipped (unchanged)",
        "failed": "**FAILED**",
        "not_attempted": "**NOT ATTEMPTED**",
    }[o.status]


def render_report(r: RunResult) -> str:
    out: list[str] = ["# docgen run report", ""]
    duration = (r.finished - r.started).total_seconds() if r.finished else 0.0
    out += [
        f"- Mode: {'dry run (nothing published)' if r.dry_run else 'publish'}",
        f"- Commit: `{r.commit}`",
        f"- Model: `{r.model}`",
        f"- Prompt version: `{r.prompt_version}`",
        f"- Started (UTC): {r.started.strftime('%Y-%m-%d %H:%M:%S')}, duration {duration:.1f}s",
        "",
    ]
    if not r.outcomes:
        out += ["No PL/SQL packages to process.", ""]
    else:
        published = sum(o.status == "published" for o in r.outcomes)
        skipped = sum(o.status == "skipped" for o in r.outcomes)
        not_attempted = sum(o.status == "not_attempted" for o in r.outcomes)
        tin = sum(_tokens(o)[0] for o in r.outcomes)
        tout = sum(_tokens(o)[1] for o in r.outcomes)
        calls = [c for o in r.outcomes for c in o.calls]
        live = [c for c in calls if not c.cached]
        out += [
            f"**{len(r.outcomes)} package(s): {published} published, {skipped} skipped, "
            f"{len(r.failed) - not_attempted} failed, {not_attempted} not attempted.** "
            f"LLM calls: {len(live)} live, "
            f"{len(calls) - len(live)} cached; tokens in/out {tin}/{tout}; "
            f"LLM time {sum(c.latency_s for c in live):.1f}s.",
            "",
            "| Package | Result | Mode | Rules | Open questions | Faithfulness | Evidence "
            "| Coverage | Dropped | LLM calls | Tokens in/out | Latency | Page |",
            "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
        ]
        for o in r.outcomes:
            ch = o.checks
            tin, tout = _tokens(o)
            doc = ch.doc if ch else None
            row = [
                f"`{o.name or o.label}`",
                _status(o),
                o.mode or "",
                len(doc.business_rules) if doc else "",
                len(doc.open_questions) if doc else "",
                ch.count("faithfulness") if ch else "",
                ch.count("evidence") if ch else "",
                ch.count("coverage") if ch else "",
                ch.dropped if ch else "",
                len(o.calls),
                f"{tin}/{tout}" if o.calls else "",
                f"{sum(c.latency_s for c in o.calls):.1f}s" if o.calls else "",
                o.url or "",
            ]
            out.append("| " + " | ".join(_cell(c) for c in row) + " |")
        out.append("")

    if r.failed:
        out += ["## Failures", ""]
        for o in r.failed:
            out += [f"### `{o.name or o.label}`", "", "```", o.error or "unknown error", "```", ""]

    with_findings = [o for o in r.outcomes if o.checks and o.checks.findings]
    if with_findings:
        out += ["## Check findings", ""]
        for o in with_findings:
            assert o.checks is not None
            out += [f"### `{o.name or o.label}`", ""]
            out += [f"- {_cell(f)}" for f in o.checks.findings]
            out.append("")

    if r.deleted:
        out += ["## Deleted packages", ""]
        out += ["Pages for these files were left in place; archive them manually:", ""]
        out += [f"- `{p}`" for p in r.deleted]
        out.append("")
    if r.ignored:
        out += [f"_Ignored {len(r.ignored)} changed non-package file(s)._", ""]
    return "\n".join(out)
