from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from docgen.models import UnitDoc
from tests.conftest import REPO_ROOT

FIXTURE = REPO_ROOT / "tests" / "fixtures" / "ord_proc.unitdoc.json"


def _doc() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_fixture_is_valid():
    UnitDoc.model_validate(_doc())


@pytest.mark.parametrize("field", ["business_rules", "open_questions", "main_flow"])
def test_null_lists_are_rejected(field):
    d = _doc()
    d[field] = None
    with pytest.raises(ValidationError):
        UnitDoc.model_validate(d)


def test_missing_list_is_rejected():
    d = _doc()
    del d["edge_cases"]
    with pytest.raises(ValidationError):
        UnitDoc.model_validate(d)


@pytest.mark.parametrize("lines", [[5, 3], [0, 2], [1], [1, 2, 3]])
def test_bad_evidence_lines(lines):
    d = _doc()
    d["business_rules"][0]["evidence_lines"] = lines
    with pytest.raises(ValidationError):
        UnitDoc.model_validate(d)


def test_extra_fields_and_bad_confidence_rejected():
    d = _doc()
    d["markup"] = "<b>x</b>"
    with pytest.raises(ValidationError):
        UnitDoc.model_validate(d)
    d = _doc()
    d["business_rules"][0]["confidence"] = "certain"
    with pytest.raises(ValidationError):
        UnitDoc.model_validate(d)
