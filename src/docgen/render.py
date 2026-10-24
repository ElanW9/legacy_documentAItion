"""Deterministic renderer: FACTS + validated UnitDoc -> Confluence storage format.

The LLM never produces markup. Every value is escaped here, and every section
states whether it comes from static analysis (FACTS, authoritative) or from the
model (AI-generated, needs review). Same inputs always give byte-identical
output; the timestamp is an input, not read from the clock.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from html import escape

from docgen.models import PackageFacts, Subprogram, UnitDoc

BANNER_TEXT = "DRAFT, AI-generated, needs functional review"
LABELS = ("ai-generated",)
# Bump when the page layout changes, so unchanged packages are re-rendered.
RENDER_VERSION = "1"


@dataclass(frozen=True)
class PageMeta:
    commit_sha: str
    model: str
    prompt_version: str
    generated_at: datetime
    source_base_url: str | None = None  # e.g. https://github.com/org/repo/blob


def page_title(package_name: str) -> str:
    return f"{package_name} (PL/SQL package)"


# ----------------------------------------------------------------------- helpers


def _e(text: object) -> str:
    return escape(str(text), quote=True)


def _code(text: object) -> str:
    return f"<code>{_e(text)}</code>"


def _macro(name: str, body: str = "", **params: str) -> str:
    ps = "".join(
        f'<ac:parameter ac:name="{_e(k)}">{_e(v)}</ac:parameter>' for k, v in params.items()
    )
    rich = f"<ac:rich-text-body>{body}</ac:rich-text-body>" if body else ""
    return f'<ac:structured-macro ac:name="{_e(name)}">{ps}{rich}</ac:structured-macro>'


def _table(headers: Sequence[str], rows: Iterable[Sequence[str]]) -> str:
    """Cells in `rows` must already be escaped markup."""
    head = "".join(f"<th>{_e(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>" for row in rows)
    return f"<table><tbody><tr>{head}</tr>{body}</tbody></table>"


def _ul(items: Iterable[str]) -> str:
    """Items must already be escaped markup."""
    lis = "".join(f"<li>{i}</li>" for i in items)
    return f"<ul>{lis}</ul>" if lis else ""


def _none(text: str = "None.") -> str:
    return f"<p><em>{_e(text)}</em></p>"


def _source(tag: str) -> str:
    return f"<p><em>Source: {_e(tag)}</em></p>"


def _lines_ref(meta: PageMeta, path: str | None, start: int, end: int) -> str:
    label = f"lines {start}-{end}" if end != start else f"line {start}"
    if meta.source_base_url and path:
        anchor = f"#L{start}-L{end}" if end != start else f"#L{start}"
        href = f"{meta.source_base_url.rstrip('/')}/{meta.commit_sha}/{path}{anchor}"
        return f'<a href="{_e(href)}">{_e(label)}</a>'
    return _e(label)


def _signature(sub: Subprogram) -> str:
    if not sub.parameters:
        return _none("(no parameters)")
    rows = "".join(
        f"<li>{_code(p.name)} {_e(p.mode)} {_code(p.type)}"
        + (f" default {_code(p.default)}" if p.default is not None else "")
        + "</li>"
        for p in sub.parameters
    )
    return f"<ul>{rows}</ul>"


# ---------------------------------------------------------------------- sections


def _header(facts: PackageFacts, doc: UnitDoc, meta: PageMeta) -> str:
    parts = [
        "<p>"
        + _macro("status", colour="Yellow", title="DRAFT")
        + " "
        + _macro("status", colour="Blue", title="AI-GENERATED")
        + "</p>",
        _macro(
            "warning",
            f"<p><strong>{_e(BANNER_TEXT)}</strong></p>"
            "<p>Sections marked <em>AI-generated</em> were written by a language model from the "
            "source code and may be wrong or incomplete. Sections marked <em>static analysis</em> "
            "are extracted deterministically from the code. This page is regenerated "
            "automatically; manual edits will be overwritten.</p>",
            title="Generated documentation",
        ),
    ]
    if not doc.source_complete:
        parts.append(
            _macro(
                "note",
                "<p>The model reported that the source looked truncated or incomplete. "
                "Treat this page as partial.</p>",
                title="Source may be incomplete",
            )
        )
    rows = [
        ("Package", _code(facts.name)),
        ("Spec", _code(facts.spec_path) if facts.spec_path else "<em>missing</em>"),
        ("Body", _code(facts.body_path) if facts.body_path else "<em>missing</em>"),
        ("Commit", _code(meta.commit_sha)),
        ("Model", _code(meta.model)),
        ("Prompt version", _code(meta.prompt_version)),
        ("Generated at (UTC)", _e(meta.generated_at.strftime("%Y-%m-%d %H:%M:%S"))),
        ("Source hash", _code(facts.source_hash[:16])),
    ]
    parts.append(_table(["Field", "Value"], rows))
    return "".join(parts)


def _purpose(doc: UnitDoc) -> str:
    body = f"<p>{_e(doc.purpose)}</p>" if doc.purpose.strip() else _none("Not determined.")
    return "<h2>Purpose</h2>" + _source("AI-generated") + body


def _business_rules(facts: PackageFacts, doc: UnitDoc, meta: PageMeta) -> str:
    out = "<h2>Business rules</h2>" + _source("AI-generated, with evidence in the package body")
    if not doc.business_rules:
        return out + _none("No business rules identified.")
    rows = [
        (
            _e(r.rule),
            _code(r.implemented_in),
            _lines_ref(meta, facts.body_path, *r.evidence_lines),
            _e(r.confidence),
        )
        for r in doc.business_rules
    ]
    return out + _table(["Rule", "Implemented in", "Evidence", "Confidence"], rows)


def _interface(facts: PackageFacts, doc: UnitDoc) -> str:
    notes = {n.name.upper(): n.summary for n in doc.procedure_notes}

    def rows(subs: list[Subprogram]) -> list[tuple[str, ...]]:
        return [
            (
                _code(s.name),
                _e(s.kind),
                _signature(s),
                _code(s.return_type) if s.return_type else "",
                _e(notes.get(s.name.upper(), "")) or "<em>no summary</em>",
            )
            for s in subs
        ]

    headers = ["Name", "Kind", "Parameters", "Returns", "Summary (AI-generated)"]
    public = [s for s in facts.subprograms if s.visibility == "public"]
    private = [s for s in facts.subprograms if s.visibility == "private"]
    out = "<h2>Public interface</h2>" + _source("static analysis; summaries AI-generated")
    out += _table(headers, rows(public)) if public else _none("No public subprograms.")
    if private:
        out += "<h3>Internal subprograms (not in spec)</h3>" + _table(headers, rows(private))
    return out


def _data_touched(facts: PackageFacts) -> str:
    out = "<h2>Data touched</h2>" + _source("static analysis")
    if not facts.tables:
        return out + _none("No static SQL table access found.")
    rows = []
    for t in facts.tables:
        users = [
            f"{_code(s.name)} ({_e(', '.join(st.operations))})"
            for s in facts.subprograms
            for st in s.tables
            if st.table == t.table
        ]
        rows.append((_code(t.table), _e(", ".join(t.operations)), ", ".join(users)))
    note = ""
    if any(s.dynamic_sql for s in facts.subprograms):
        note = _none("Some subprograms use dynamic SQL; tables accessed that way are not listed.")
    return out + _table(["Table", "Operations", "Used in"], rows) + note


def _dependencies(facts: PackageFacts) -> str:
    out = "<h2>Dependencies</h2>" + _source("static analysis")
    rows = [
        (
            _code(f"{c.package}.{c.member}"),
            _e(c.kind),
            _code(s.name),
            _e(", ".join(map(str, c.lines))),
        )
        for s in facts.subprograms
        for c in s.calls
    ]
    if not rows:
        return out + _none("No calls to other packages found.")
    rows.sort(key=lambda r: (r[0], r[2]))
    return out + _table(["Calls", "Kind", "Called from", "Line(s)"], rows)


def _main_flow(doc: UnitDoc) -> str:
    out = "<h2>Main flow</h2>" + _source("AI-generated")
    if not doc.main_flow:
        return out + _none("No main flow described.")
    items = "".join(
        f"<li>{_e(s.description)}"
        + (f" ({_code(s.implemented_in)})" if s.implemented_in else "")
        + "</li>"
        for s in sorted(doc.main_flow, key=lambda s: s.step)
    )
    return out + f"<ol>{items}</ol>"


def _errors(facts: PackageFacts, doc: UnitDoc) -> str:
    out = "<h2>Error handling and edge cases</h2>"
    raises = [(s, r) for s in facts.subprograms for r in s.raises]
    out += "<h3>Raised application errors</h3>" + _source("static analysis")
    if raises:
        rows = [(_code(r.code), _code(r.message), _code(s.name), _e(r.line)) for s, r in raises]
        out += _table(["Code", "Message expression", "Raised in", "Line"], rows)
    else:
        out += _none("No RAISE_APPLICATION_ERROR calls found.")
    out += "<h3>Edge cases</h3>" + _source("AI-generated")
    out += _ul(_e(c) for c in doc.edge_cases) or _none()
    return out


def _open_questions(facts: PackageFacts, doc: UnitDoc) -> str:
    out = "<h2>Open questions</h2>"
    out += "<p>Items the generator could not resolve from the code. Please answer or correct.</p>"
    out += "<h3>From the model</h3>" + _source("AI-generated")
    out += _ul(_e(q) for q in doc.open_questions) or _none()
    out += "<h3>From static analysis</h3>" + _source("static analysis")
    out += _ul(_e(w) for w in facts.warnings) or _none()
    return out


def render_page(
    facts: PackageFacts,
    doc: UnitDoc,
    meta: PageMeta,
    *,
    quality_notes: Sequence[str] = (),
) -> str:
    """Render one package page in Confluence storage format."""
    parts = [
        _header(facts, doc, meta),
        _purpose(doc),
        _business_rules(facts, doc, meta),
        _interface(facts, doc),
        _data_touched(facts),
        _dependencies(facts),
        _main_flow(doc),
        _errors(facts, doc),
        _open_questions(facts, doc),
    ]
    if quality_notes:
        parts.append(
            "<h2>Automated checks</h2>"
            + _source("post-generation checks")
            + _ul(_e(n) for n in quality_notes)
        )
    return "".join(parts)
