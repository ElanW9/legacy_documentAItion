"""Deterministic post-generation checks on a validated `UnitDoc`.

- faithfulness: every identifier-like token in the model's text must exist in
  SOURCE (the package body, comments included) or FACTS; procedure names the
  model links to must be real subprograms of the package.
- evidence: each business rule's line range must lie within the body, and
  inside the subprogram named in `implemented_in`.
- coverage: every subprogram has a procedure note; every extracted table
  appears on the rendered page.

Policy: items that cannot be rendered truthfully are dropped (a rule linked to
a non-existent procedure or citing lines outside the source; a note for an
unknown procedure). Everything else is kept and flagged, and every finding is
listed on the page under "Automated checks" and in the run report.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Literal

from docgen.models import PackageFacts, UnitDoc

CheckName = Literal["faithfulness", "evidence", "coverage"]

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_$#]*")
# Candidate identifiers in prose: dotted names, names with "_", or ALL-CAPS words.
_CANDIDATE_RE = re.compile(r"(?<![\w$#.])[A-Za-z][A-Za-z0-9_$#]*(?:\.[A-Za-z][A-Za-z0-9_$#]*)*")
_ALL_CAPS_RE = re.compile(r"^[A-Z][A-Z0-9]{2,}$")
# Upper-case words that are ordinary prose, not identifiers.
_PROSE_CAPS = frozenset(
    {"AND", "API", "CSV", "DRAFT", "FAQ", "HTML", "HTTP", "ISO", "JSON", "NOT", "PDF", "SQL",
     "THE", "URL", "UTC", "VAT", "XML", "PLSQL"}
)  # fmt: skip


@dataclass(frozen=True)
class Finding:
    check: CheckName
    action: Literal["flagged", "dropped"]
    location: str
    message: str

    def __str__(self) -> str:
        return f"[{self.check}] {self.location}: {self.message} ({self.action})"


@dataclass
class CheckResult:
    doc: UnitDoc  # after dropping items that failed hard checks
    findings: list[Finding] = field(default_factory=list)

    def count(self, check: CheckName) -> int:
        return sum(1 for f in self.findings if f.check == check)

    @property
    def dropped(self) -> int:
        return sum(1 for f in self.findings if f.action == "dropped")

    def quality_notes(self) -> list[str]:
        return [str(f) for f in self.findings]


def known_identifiers(facts: PackageFacts, body_text: str | None) -> set[str]:
    words: set[str] = set()
    for text in (body_text or "", facts.model_dump_json()):
        words.update(w.upper() for w in _WORD_RE.findall(text))
    return words


def _is_identifier_like(token: str) -> bool:
    if "." in token:  # "pkg.member", but not abbreviations such as "e.g"
        return all(len(p) >= 2 for p in token.split("."))
    return "_" in token or bool(_ALL_CAPS_RE.match(token))


def unknown_identifiers(text: str, known: set[str]) -> list[str]:
    out: list[str] = []
    for token in _CANDIDATE_RE.findall(text):
        if not _is_identifier_like(token) or token.upper() in _PROSE_CAPS:
            continue
        parts = [p.upper() for p in token.split(".")]
        if all(p in known for p in parts):
            continue
        if token not in out:
            out.append(token)
    return out


def _text_fields(doc: UnitDoc) -> Iterable[tuple[str, str]]:
    yield "purpose", doc.purpose
    for n in doc.procedure_notes:
        yield f"procedure_notes[{n.name}]", n.summary
    for i, r in enumerate(doc.business_rules):
        yield f"business_rules[{i}]", r.rule
    for s in doc.main_flow:
        yield f"main_flow[step {s.step}]", s.description
    for i, c in enumerate(doc.edge_cases):
        yield f"edge_cases[{i}]", c
    for i, q in enumerate(doc.open_questions):
        yield f"open_questions[{i}]", q


def run_checks(facts: PackageFacts, body_text: str | None, doc: UnitDoc) -> CheckResult:
    findings: list[Finding] = []
    subs = {s.name.upper(): s for s in facts.subprograms}
    known = known_identifiers(facts, body_text)
    line_count = facts.body_line_count

    # --- faithfulness: free text --------------------------------------------
    for location, text in _text_fields(doc):
        for ident in unknown_identifiers(text, known):
            findings.append(
                Finding(
                    "faithfulness",
                    "flagged",
                    location,
                    f"mentions '{ident}', which does not appear in the source or static analysis",
                )
            )

    # --- procedure notes ------------------------------------------------------
    notes = []
    for n in doc.procedure_notes:
        if n.name.upper() in subs:
            notes.append(n)
        else:
            findings.append(
                Finding(
                    "faithfulness",
                    "dropped",
                    f"procedure_notes[{n.name}]",
                    "note for a subprogram that does not exist in this package",
                )
            )

    # --- business rules: faithfulness + evidence ------------------------------
    rules = []
    for i, r in enumerate(doc.business_rules):
        loc = f"business_rules[{i}]"
        sub = subs.get(r.implemented_in.upper())
        start, end = r.evidence_lines
        if sub is None:
            findings.append(
                Finding(
                    "faithfulness",
                    "dropped",
                    loc,
                    f"implemented_in '{r.implemented_in}' is not a subprogram of {facts.name}",
                )
            )
            continue
        if end > line_count:
            findings.append(
                Finding(
                    "evidence",
                    "dropped",
                    loc,
                    f"evidence lines {start}-{end} are outside the body (1-{line_count})",
                )
            )
            continue
        if sub.start_line is None or sub.end_line is None:
            findings.append(Finding("evidence", "flagged", loc, f"{sub.name} has no body to cite"))
        elif not (sub.start_line <= start and end <= sub.end_line):
            findings.append(
                Finding(
                    "evidence",
                    "flagged",
                    loc,
                    f"evidence lines {start}-{end} are not inside {sub.name} "
                    f"(lines {sub.start_line}-{sub.end_line})",
                )
            )
        rules.append(r)

    # --- main flow: links must point at real subprograms ----------------------
    flow = []
    for s in doc.main_flow:
        if s.implemented_in is not None and s.implemented_in.upper() not in subs:
            findings.append(
                Finding(
                    "faithfulness",
                    "flagged",
                    f"main_flow[step {s.step}]",
                    f"implemented_in '{s.implemented_in}' is not a subprogram of {facts.name}; "
                    "link removed",
                )
            )
            s = s.model_copy(update={"implemented_in": None})
        flow.append(s)

    # --- coverage: every subprogram has a note --------------------------------
    noted = {n.name.upper() for n in notes}
    for s in facts.subprograms:
        if s.name.upper() not in noted:
            findings.append(
                Finding("coverage", "flagged", f"procedure_notes[{s.name}]", "no summary generated")
            )

    cleaned = doc.model_copy(
        update={"procedure_notes": notes, "business_rules": rules, "main_flow": flow}
    )
    return CheckResult(cleaned, findings)


def check_page_coverage(facts: PackageFacts, page_text: str) -> list[Finding]:
    """Every extracted table must appear on the rendered page."""
    upper = page_text.upper()
    return [
        Finding("coverage", "flagged", "data_touched", f"table {t.table} missing from page")
        for t in facts.tables
        if t.table.upper() not in upper
    ]
