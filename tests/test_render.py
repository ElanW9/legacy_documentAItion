from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from dataclasses import replace
from datetime import UTC, datetime

from docgen.extract import extract_package, find_all_packages, known_package_names
from docgen.models import BusinessRule, UnitDoc
from docgen.render import BANNER_TEXT, PageMeta, page_title, render_page
from tests.conftest import GOLDEN_DIR, PLSQL_ROOT, REPO_ROOT

META = PageMeta(
    commit_sha="0123456789abcdef0123456789abcdef01234567",
    model="fake-llm",
    prompt_version="v1",
    generated_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
    source_base_url="https://github.com/example/repo/blob",
)


def _ord_proc():
    pf = next(p for p in find_all_packages(PLSQL_ROOT) if p.label.endswith("ord_proc"))
    known = known_package_names(PLSQL_ROOT)
    facts = extract_package(pf, root=REPO_ROOT, known_packages=known).facts
    fixture = REPO_ROOT / "tests" / "fixtures" / "ord_proc.unitdoc.json"
    doc = UnitDoc.model_validate_json(fixture.read_text(encoding="utf-8"))
    return facts, doc


def _parse(xhtml: str) -> ET.Element:
    """Storage format uses ac:/ri: prefixes without declarations; declare them to parse."""
    wrapped = (
        '<root xmlns:ac="http://atlassian.com/content" xmlns:ri="http://atlassian.com/resource">'
        + xhtml
        + "</root>"
    )
    return ET.fromstring(wrapped)


def test_render_matches_golden():
    facts, doc = _ord_proc()
    actual = render_page(facts, doc, META)
    golden = GOLDEN_DIR / "ord_proc.storage.xml"
    if os.environ.get("UPDATE_GOLDEN") == "1":
        golden.write_text(actual, encoding="utf-8", newline="\n")
    assert actual == golden.read_text(encoding="utf-8")


def test_render_is_well_formed_and_has_all_sections():
    facts, doc = _ord_proc()
    root = _parse(render_page(facts, doc, META))
    headings = [h.text for h in root.iter("h2")]
    assert headings == [
        "Purpose",
        "Business rules",
        "Public interface",
        "Data touched",
        "Dependencies",
        "Main flow",
        "Error handling and edge cases",
        "Open questions",
    ]
    text = "".join(root.itertext())
    assert BANNER_TEXT in text
    for table in facts.tables:  # every extracted table appears on the page
        assert table.table in text


def test_render_escapes_model_output():
    facts, doc = _ord_proc()
    evil = BusinessRule(
        rule='<script>alert(1)</script> & "quotes"',
        implemented_in="CALC_DISC_PCT",
        evidence_lines=(24, 25),
        confidence="low",
    )
    doc = doc.model_copy(update={"business_rules": [evil], "open_questions": ["a < b & c"]})
    xhtml = render_page(facts, doc, META)
    assert "<script>" not in xhtml
    assert "&lt;script&gt;" in xhtml and "a &lt; b &amp; c" in xhtml
    _parse(xhtml)


def test_evidence_links_point_to_commit_and_lines():
    facts, doc = _ord_proc()
    xhtml = render_page(facts, doc, META)
    expected = (
        'href="https://github.com/example/repo/blob/0123456789abcdef0123456789abcdef01234567/'
        'plsql/orders/ord_proc.pkb#L26-L28"'
    )
    assert expected in xhtml
    assert "href=" not in render_page(facts, doc, replace(META, source_base_url=None))


def test_render_is_deterministic_and_handles_empty_doc():
    facts, doc = _ord_proc()
    assert render_page(facts, doc, META) == render_page(facts, doc, META)
    empty = UnitDoc(
        source_complete=False,
        purpose="",
        procedure_notes=[],
        business_rules=[],
        main_flow=[],
        edge_cases=[],
        open_questions=[],
    )
    xhtml = render_page(facts, empty, META, quality_notes=["2 identifiers dropped"])
    _parse(xhtml)
    assert "Source may be incomplete" in xhtml
    assert "No business rules identified." in xhtml
    assert "Automated checks" in xhtml


def test_static_warnings_shown_and_title():
    facts, doc = _ord_proc()
    assert "ARCHIVE_ORDERS uses dynamic SQL" in render_page(facts, doc, META)
    assert page_title("ORD_PROC") == "ORD_PROC (PL/SQL package)"
