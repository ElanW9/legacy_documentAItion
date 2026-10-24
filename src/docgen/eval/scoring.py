"""Deterministic scoring of generated docs against FACTS, SOURCE and the ground truth."""

from __future__ import annotations

import re
from collections.abc import Iterable

from docgen.checks import CheckResult
from docgen.eval.ground_truth import Trap
from docgen.eval.results import PackageEval, PackageMetrics, Summary, TrapResult
from docgen.models import PackageFacts, UnitDoc


def package_metrics(
    facts: PackageFacts, raw: UnitDoc, checked: CheckResult, page_text: str
) -> PackageMetrics:
    subs = {s.name.upper(): s for s in facts.subprograms}
    noted = {n.name.upper() for n in checked.doc.procedure_notes}
    upper_page = page_text.upper()
    valid = 0
    for r in raw.business_rules:
        sub = subs.get(r.implemented_in.upper())
        start, end = r.evidence_lines
        if (
            sub is not None
            and sub.start_line is not None
            and sub.end_line is not None
            and sub.start_line <= start
            and end <= sub.end_line
        ):
            valid += 1
    return PackageMetrics(
        subprograms=len(subs),
        subprograms_with_note=len(noted & set(subs)),
        tables=len(facts.tables),
        tables_on_page=sum(1 for t in facts.tables if t.table.upper() in upper_page),
        rules=len(raw.business_rules),
        rules_valid_evidence=valid,
        faithfulness_violations=checked.count("faithfulness"),
        dropped_items=checked.dropped,
        open_questions=len(checked.doc.open_questions),
    )


def anchor_line(trap: Trap, facts: PackageFacts, body_lines: list[str]) -> int | None:
    sub = next((s for s in facts.subprograms if s.name == trap.procedure), None)
    if sub is None or sub.start_line is None or sub.end_line is None:
        return None
    for n in range(sub.start_line, sub.end_line + 1):
        if re.search(trap.anchor, body_lines[n - 1]):
            return n
    return None


def _candidates(doc: UnitDoc, field: str, procedure: str) -> Iterable[tuple[str, object]]:
    """(text, item) pairs of `field` that may contain the trap."""
    if field == "business_rules":
        for r in doc.business_rules:
            if r.implemented_in.upper() == procedure.upper():
                yield r.rule, r
    elif field == "procedure_notes":
        for n in doc.procedure_notes:
            if n.name.upper() == procedure.upper():
                yield n.summary, n
    elif field == "main_flow":
        for s in doc.main_flow:
            yield s.description, s
    elif field == "open_questions":
        for q in doc.open_questions:
            yield q, q
    elif field == "edge_cases":
        for c in doc.edge_cases:
            yield c, c


def _matches(text: str, patterns: list[str]) -> bool:
    return all(re.search(p, text, re.IGNORECASE) for p in patterns)


def find_trap(trap: Trap, doc: UnitDoc) -> tuple[str, str, object] | None:
    for field in trap.expect_in:
        for text, item in _candidates(doc, field, trap.procedure):
            if _matches(text, trap.match_all):
                return field, text, item
    return None


def score_trap(
    trap: Trap,
    pkg: PackageEval | None,
    facts: PackageFacts | None,
    body_lines: list[str] | None,
) -> TrapResult:
    base = TrapResult(
        id=trap.id,
        type=trap.type,
        package=trap.package,
        procedure=trap.procedure,
        found=False,
    )
    if pkg is None or not pkg.ok or pkg.doc is None or facts is None or body_lines is None:
        return base.model_copy(update={"package_failed": True})

    hit = find_trap(trap, pkg.doc)
    update: dict = {}
    if hit is not None:
        field, text, item = hit
        update.update(found=True, found_in=field, matched_text=text)
        if field == "business_rules":
            line = anchor_line(trap, facts, body_lines)
            start, end = item.evidence_lines  # type: ignore[attr-defined]
            update["evidence_hit"] = line is not None and start <= line <= end
    if trap.forbid_in_rules and pkg.raw_doc is not None:
        update["hallucinated"] = any(
            r.implemented_in.upper() == trap.procedure.upper()
            and any(re.search(p, r.rule, re.IGNORECASE) for p in trap.forbid_in_rules)
            for r in pkg.raw_doc.business_rules
        )
    unit = pkg.unit_docs.get(trap.procedure)
    if unit is not None:
        update["found_in_unit_doc"] = find_trap(trap, unit) is not None
    return base.model_copy(update=update)


def _pct(num: int, den: int) -> float:
    return round(100.0 * num / den, 1) if den else 100.0


def summarise(packages: list[PackageEval], traps: list[TrapResult]) -> Summary:
    ms = [p.metrics for p in packages if p.ok]
    calls = [c for p in packages for c in p.calls]
    subs = sum(m.subprograms for m in ms)
    noted = sum(m.subprograms_with_note for m in ms)
    tables = sum(m.tables for m in ms)
    on_page = sum(m.tables_on_page for m in ms)
    rules = sum(m.rules for m in ms)
    rule_traps = [t for t in traps if t.evidence_hit is not None]
    return Summary(
        packages=len(packages),
        failed=sum(not p.ok for p in packages),
        coverage_pct=_pct(noted + on_page, subs + tables),
        notes_coverage_pct=_pct(noted, subs),
        tables_coverage_pct=_pct(on_page, tables),
        faithfulness_violations=sum(m.faithfulness_violations for m in ms),
        rules=rules,
        evidence_valid_pct=_pct(sum(m.rules_valid_evidence for m in ms), rules),
        traps_found=sum(t.found for t in traps),
        traps_total=len(traps),
        trap_evidence_hits=sum(bool(t.evidence_hit) for t in rule_traps),
        trap_evidence_total=len(rule_traps),
        hallucinations=sum(t.hallucinated for t in traps),
        llm_calls=sum(not c.cached for c in calls),
        validation_retries=sum(c.validation_retries for c in calls),
        tokens_in=sum(c.input_tokens or 0 for c in calls),
        tokens_out=sum(c.output_tokens or 0 for c in calls),
        latency_s=round(sum(c.latency_s for c in calls), 1),
    )
