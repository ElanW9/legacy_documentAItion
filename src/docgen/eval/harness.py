"""Run the generate stage over the seeded packages and score the result.

Nothing is published. The same extract -> generate -> checks -> render path as
`docgen run` is used, so the eval measures what would actually be published.
Scoring is separate from generation: a saved run can be re-scored with newer
checks from its stored model output, without calling the model again.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

from docgen.checks import check_page_coverage, run_checks, visible_text
from docgen.config import Settings
from docgen.eval.ground_truth import GroundTruth
from docgen.eval.results import CallStats, EvalRun, FindingRecord, PackageEval, TrapResult
from docgen.eval.scoring import package_metrics, score_trap, summarise
from docgen.extract import ExtractedPackage, extract_package, find_all_packages, known_package_names
from docgen.generate import GenerationError, LLMCallRecord, ResponseCache, generate_package_doc
from docgen.llm.base import LLMClient
from docgen.prompts import Prompts
from docgen.render import PageMeta, render_page

log = logging.getLogger(__name__)

# Page metadata does not affect scoring; fixed so scoring is reproducible.
_EVAL_META = PageMeta("eval", "eval", "eval", datetime(2000, 1, 1, tzinfo=UTC))


def _calls(records: list[LLMCallRecord]) -> list[CallStats]:
    return [
        CallStats(
            unit=r.unit,
            purpose=r.purpose,
            input_tokens=r.input_tokens,
            output_tokens=r.output_tokens,
            latency_s=round(r.latency_s, 2),
            attempts=r.attempts,
            validation_retries=r.validation_retries,
            cached=r.cached,
        )
        for r in records
    ]


def generate_for_eval(
    ext: ExtractedPackage,
    *,
    client: LLMClient,
    prompts: Prompts,
    settings: Settings,
    cache: ResponseCache,
) -> PackageEval:
    facts = ext.facts
    try:
        gen = generate_package_doc(
            ext,
            client,
            prompts,
            chunk_threshold_lines=settings.chunk_threshold_lines,
            cache=cache,
        )
    except GenerationError as exc:
        log.error("%s: %s", facts.name, exc)
        return PackageEval(
            name=facts.name,
            ok=False,
            error=str(exc),
            source_hash=facts.source_hash,
            calls=_calls(exc.calls),
        )
    return PackageEval(
        name=facts.name,
        ok=True,
        mode=gen.mode,
        source_hash=facts.source_hash,
        calls=_calls(gen.calls),
        raw_doc=gen.doc,
        unit_docs=gen.unit_docs,
    )


def score_package(ext: ExtractedPackage, pkg: PackageEval) -> PackageEval:
    """(Re)compute checks and metrics from the stored model output; no LLM calls."""
    if not pkg.ok or pkg.raw_doc is None:
        return pkg
    facts = ext.facts
    checked = run_checks(facts, ext.body.text if ext.body else None, pkg.raw_doc)
    page = visible_text(render_page(facts, checked.doc, _EVAL_META))
    checked.findings.extend(check_page_coverage(facts, page))
    return pkg.model_copy(
        update={
            "metrics": package_metrics(facts, pkg.raw_doc, checked, page),
            "findings": [
                FindingRecord(
                    check=f.check, action=f.action, location=f.location, message=f.message
                )
                for f in checked.findings
            ],
            "doc": checked.doc,
        }
    )


def _extract_all(root: Path, settings: Settings) -> dict[str, ExtractedPackage]:
    plsql = root / settings.plsql_root
    known = known_package_names(plsql)
    return {
        e.facts.name: e
        for e in (
            extract_package(pf, root=root, known_packages=known) for pf in find_all_packages(plsql)
        )
    }


def _score_traps(
    gt: GroundTruth, results: dict[str, PackageEval], extracted: dict[str, ExtractedPackage]
) -> list[TrapResult]:
    traps = []
    for trap in gt.traps:
        if trap.package not in results:
            continue
        ext = extracted[trap.package]
        body_lines = ext.body.text.split("\n") if ext.body else None
        traps.append(score_trap(trap, results[trap.package], ext.facts, body_lines))
    return traps


def run_eval(
    gt: GroundTruth,
    *,
    root: Path,
    settings: Settings,
    client: LLMClient,
    prompts: Prompts,
    cache: ResponseCache,
    commit: str,
    label: str | None = None,
    packages: list[str] | None = None,
    now: datetime | None = None,
    resume: EvalRun | None = None,
) -> EvalRun:
    """Generate and score. With `resume`, successful packages of that earlier run
    are kept (same model, prompts and source required) and only failed or
    missing packages are generated, e.g. after the daily quota ran out."""
    now = now or datetime.now(UTC)
    extracted = _extract_all(root, settings)
    reusable: dict[str, PackageEval] = {}
    if resume is not None:
        if (resume.model, resume.prompt_fingerprint) != (client.model, prompts.fingerprint[:16]):
            raise ValueError(
                f"cannot resume {resume.run_id}: it used model {resume.model} and prompt "
                f"{resume.prompt_version} ({resume.prompt_fingerprint}); now {client.model} and "
                f"{prompts.version} ({prompts.fingerprint[:16]})"
            )
        for pkg in resume.packages:
            ext = extracted.get(pkg.name)
            if pkg.ok and ext is not None and pkg.source_hash == ext.facts.source_hash:
                reusable[pkg.name] = pkg
        packages = packages or [p.name for p in resume.packages]
    wanted = [p.upper() for p in packages] if packages else sorted(extracted)
    unknown = sorted(set(wanted) - set(extracted))
    if unknown:
        raise ValueError(f"unknown package(s): {', '.join(unknown)}")

    results: dict[str, PackageEval] = {}
    for name in wanted:
        if name in reusable:
            log.info("%s: reusing result from %s", name, resume.run_id if resume else "")
            results[name] = score_package(extracted[name], reusable[name])
            continue
        log.info("evaluating %s", name)
        generated = generate_for_eval(
            extracted[name], client=client, prompts=prompts, settings=settings, cache=cache
        )
        results[name] = score_package(extracted[name], generated)

    traps = _score_traps(gt, results, extracted)
    pkgs = list(results.values())
    run_id = f"{now.strftime('%Y%m%dT%H%M%SZ')}_{_slug(client.model)}_{_slug(prompts.version)}"
    if label:
        run_id += f"_{_slug(label)}"
    if resume is not None:
        run_id, label = resume.run_id, resume.label
    return EvalRun(
        run_id=run_id,
        created_at=resume.created_at if resume else now,
        resumed_at=[*resume.resumed_at, now] if resume else [],
        label=label,
        commit=commit
        if resume is None or resume.commit == commit
        else f"{resume.commit}, {commit}",
        model=client.model,
        prompt_version=prompts.version,
        prompt_fingerprint=prompts.fingerprint[:16],
        settings={
            "chunk_threshold_lines": settings.chunk_threshold_lines,
            "llm_temperature": settings.llm_temperature,
            "gemini_thinking_level": settings.gemini_thinking_level,
            "schema_in_prompt": prompts.schema_in_prompt,
            "cache_used": cache.directory is not None,
        },
        ground_truth_version=gt.version,
        summary=summarise(pkgs, traps),
        traps=traps,
        packages=pkgs,
    )


def rescore_run(run: EvalRun, gt: GroundTruth, *, root: Path, settings: Settings) -> EvalRun:
    """Apply the current checks and scoring to a saved run's model output.

    The source must be unchanged since the run (verified by hash). The first
    rescore keeps the original summary, so calibration changes stay visible.
    """
    extracted = _extract_all(root, settings)
    results: dict[str, PackageEval] = {}
    for pkg in run.packages:
        ext = extracted.get(pkg.name)
        if ext is None:
            raise ValueError(f"{run.run_id}: package {pkg.name} no longer exists")
        if pkg.source_hash and pkg.source_hash != ext.facts.source_hash:
            raise ValueError(f"{run.run_id}: source of {pkg.name} changed since the run")
        results[pkg.name] = score_package(ext, pkg)
    traps = _score_traps(gt, results, extracted)
    pkgs = list(results.values())
    return run.model_copy(
        update={
            "packages": pkgs,
            "traps": traps,
            "summary": summarise(pkgs, traps),
            "original_summary": run.original_summary or run.summary,
            "ground_truth_version": gt.version,
            "rescored_at": datetime.now(UTC),
        }
    )


def _slug(text: str) -> str:
    return "".join(c if c.isalnum() or c in "-." else "-" for c in text).strip("-")
