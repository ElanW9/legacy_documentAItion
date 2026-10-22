"""The ground truth must stay anchored to the actual seeded code."""

from __future__ import annotations

import re

import pytest

from docgen.eval.ground_truth import load_ground_truth
from docgen.extract import extract_package, find_all_packages, known_package_names
from tests.conftest import PLSQL_ROOT, REPO_ROOT

GT = load_ground_truth(REPO_ROOT / "eval" / "ground_truth.yaml")
EXTRACTED = {
    e.facts.name: e
    for e in (
        extract_package(pf, root=REPO_ROOT, known_packages=known_package_names(PLSQL_ROOT))
        for pf in find_all_packages(PLSQL_ROOT)
    )
}

REQUIRED_TYPES = {
    "hidden_business_rule",
    "dead_code",
    "cross_package_call",
    "magic_number",
    "swallowed_exception",
    "cryptic_naming",
    "commented_out_logic",
}


def test_all_required_trap_types_are_seeded():
    assert {t.type for t in GT.traps} >= REQUIRED_TYPES


def test_trap_ids_unique():
    ids = [t.id for t in GT.traps]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("trap", GT.traps, ids=lambda t: t.id)
def test_anchor_matches_exactly_one_line_in_procedure(trap):
    pkg = EXTRACTED[trap.package]
    sub = next(s for s in pkg.facts.subprograms if s.name == trap.procedure)
    assert pkg.body is not None and sub.start_line and sub.end_line
    lines = pkg.body.lines[sub.start_line - 1 : sub.end_line]
    hits = [ln for ln in lines if re.search(trap.anchor, ln)]
    assert len(hits) == 1, f"{trap.id}: anchor matched {len(hits)} lines"
