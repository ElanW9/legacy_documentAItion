from __future__ import annotations

import json

from docgen.checks import check_page_coverage, known_identifiers, run_checks, unknown_identifiers
from docgen.extract import extract_package, find_all_packages, known_package_names
from docgen.models import UnitDoc
from tests.conftest import PLSQL_ROOT, REPO_ROOT


def _ord_proc():
    pf = next(p for p in find_all_packages(PLSQL_ROOT) if p.label.endswith("ord_proc"))
    ext = extract_package(pf, root=REPO_ROOT, known_packages=known_package_names(PLSQL_ROOT))
    doc = UnitDoc.model_validate_json(
        (REPO_ROOT / "tests" / "fixtures" / "ord_proc.unitdoc.json").read_text(encoding="utf-8")
    )
    return ext.facts, ext.body.text, doc


def _with(doc: UnitDoc, **update) -> UnitDoc:
    return UnitDoc.model_validate({**json.loads(doc.model_dump_json()), **update})


def _rule(name="CALC_DISC_PCT", lines=(22, 31), text="Level S gets 5%."):
    return {"rule": text, "implemented_in": name, "evidence_lines": lines, "confidence": "high"}


def test_fixture_doc_passes_cleanly():
    facts, body, doc = _ord_proc()
    result = run_checks(facts, body, doc)
    assert result.findings == []
    assert result.doc == doc


def test_identifier_detection():
    facts, body, _ = _ord_proc()
    known = known_identifiers(facts, body)
    text = (
        "SUBMIT_ORDER calls bill_calc.create_invoice and writes ORD_HDR, e.g. for KMO. "
        "It also updates CST_LOYALTY_PTS via BILL_CALC.APPLY_COUPON. The VAT is not set."
    )
    # KMO only occurs in BILL_CALC's source, so it is unknown in ORD_PROC's context.
    assert unknown_identifiers(text, known) == ["KMO", "CST_LOYALTY_PTS", "BILL_CALC.APPLY_COUPON"]


def test_unknown_identifier_in_text_is_flagged_not_dropped():
    facts, body, doc = _ord_proc()
    doc = _with(doc, open_questions=["Why is ORD_MAGIC_TBL never purged?"])
    result = run_checks(facts, body, doc)
    [f] = result.findings
    assert (f.check, f.action, f.location) == ("faithfulness", "flagged", "open_questions[0]")
    assert "ORD_MAGIC_TBL" in f.message
    assert result.doc.open_questions == doc.open_questions


def test_rule_for_unknown_procedure_is_dropped():
    facts, body, doc = _ord_proc()
    doc = _with(doc, business_rules=[_rule(), _rule(name="APPLY_COUPON")])
    result = run_checks(facts, body, doc)
    assert [r.implemented_in for r in result.doc.business_rules] == ["CALC_DISC_PCT"]
    assert result.count("faithfulness") == 1 and result.dropped == 1


def test_evidence_outside_body_is_dropped_and_outside_procedure_flagged():
    facts, body, doc = _ord_proc()
    doc = _with(doc, business_rules=[_rule(lines=(500, 510)), _rule(lines=(80, 82))])
    result = run_checks(facts, body, doc)
    assert len(result.doc.business_rules) == 1
    msgs = [(f.check, f.action) for f in result.findings]
    assert msgs == [("evidence", "dropped"), ("evidence", "flagged")]
    assert "not inside CALC_DISC_PCT" in result.findings[1].message


def test_missing_notes_and_bogus_links():
    facts, body, doc = _ord_proc()
    doc = _with(
        doc,
        procedure_notes=[{"name": "NOT_A_PROC", "summary": "x"}],
        main_flow=[{"step": 1, "description": "Do it.", "implemented_in": "GHOST"}],
    )
    result = run_checks(facts, body, doc)
    coverage = [f for f in result.findings if f.check == "coverage"]
    assert len(coverage) == len(facts.subprograms)
    assert result.doc.procedure_notes == []
    assert result.doc.main_flow[0].implemented_in is None
    assert any("link removed" in f.message for f in result.findings)


def test_page_coverage_reports_missing_tables():
    facts, _, _ = _ord_proc()
    assert check_page_coverage(facts, " ".join(t.table for t in facts.tables)) == []
    missing = check_page_coverage(facts, "")
    assert len(missing) == len(facts.tables)
