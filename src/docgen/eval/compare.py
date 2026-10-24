"""Markdown tables comparing eval runs (summary metrics and per-trap results)."""

from __future__ import annotations

from docgen.eval.results import EvalRun


def _name(r: EvalRun) -> str:
    return f"{r.run_id}"


def _rescored(r: EvalRun) -> str:
    o = r.original_summary
    if o is None or r.rescored_at is None:
        return ""
    return (
        f" (re-scored; originally {o.faithfulness_violations} violations, "
        f"{o.traps_found}/{o.traps_total} traps)"
    )


def summary_table(runs: list[EvalRun]) -> str:
    head = (
        "| Run | Model | Prompt | Coverage | Faithfulness violations | Evidence valid "
        "| Traps found | Trap evidence | Hallucinations | Failed pkgs | Val. retries "
        "| LLM calls | Tokens in/out | LLM time |\n"
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"
    )
    rows = []
    for r in runs:
        s = r.summary
        rows.append(
            f"| `{_name(r)}`{_rescored(r)} | `{r.model}` | `{r.prompt_version}` "
            f"| {s.coverage_pct:.1f}% "
            f"| {s.faithfulness_violations} | {s.evidence_valid_pct:.1f}% ({s.rules} rules) "
            f"| {s.traps_found}/{s.traps_total} | {s.trap_evidence_hits}/{s.trap_evidence_total} "
            f"| {s.hallucinations} | {s.failed}/{s.packages} | {s.validation_retries} "
            f"| {s.llm_calls} | {s.tokens_in}/{s.tokens_out} | {s.latency_s:.0f}s |"
        )
    return "\n".join([head, *rows])


def trap_table(runs: list[EvalRun]) -> str:
    ids: list[str] = []
    for r in runs:
        ids += [t.id for t in r.traps if t.id not in ids]
    head = "| Trap | " + " | ".join(f"`{_name(r)}`" for r in runs) + " |"
    sep = "|---|" + "---|" * len(runs)
    rows = []
    for trap_id in ids:
        cells = []
        for r in runs:
            t = next((t for t in r.traps if t.id == trap_id), None)
            if t is None:
                cells.append("n/a")
            elif t.package_failed:
                cells.append("pkg failed")
            elif t.found:
                cell = f"found ({t.found_in})"
                if t.evidence_hit is False:
                    cell += ", evidence misses anchor"
                cells.append(cell)
            else:
                cell = "**missed**"
                if t.found_in_unit_doc:
                    cell += " (lost in synthesis)"
                cells.append(cell)
            if t is not None and t.hallucinated:
                cells[-1] += ", **hallucinated rule**"
        rows.append(f"| {trap_id} | " + " | ".join(cells) + " |")
    return "\n".join([head, sep, *rows])


def comparison_markdown(runs: list[EvalRun]) -> str:
    if not runs:
        return "No eval runs found."
    return "\n\n".join(
        [
            "## Summary",
            summary_table(runs),
            "## Traps",
            trap_table(runs),
            "Coverage = procedures with a note + tables on the page, over all procedures and "
            "tables. Evidence valid = model rules whose lines lie inside the cited procedure. "
            "Trap evidence = trap rules whose evidence covers the trap's anchor line.",
        ]
    )
