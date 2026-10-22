"""Golden-file tests: FACTS for every package in plsql/ must match tests/golden/.

Regenerate after an intentional extractor change with:
    UPDATE_GOLDEN=1 pytest tests/test_golden_facts.py
and review the diff before committing.
"""

from __future__ import annotations

import json
import os

import pytest

from docgen.extract import extract_package, find_all_packages, known_package_names
from tests.conftest import GOLDEN_DIR, PLSQL_ROOT, REPO_ROOT

PACKAGES = find_all_packages(PLSQL_ROOT)


def test_seed_repo_is_present():
    assert len(PACKAGES) >= 4


@pytest.mark.parametrize("files", PACKAGES, ids=lambda pf: pf.label.rsplit("/", 1)[-1])
def test_facts_match_golden(files):
    facts = extract_package(
        files, root=REPO_ROOT, known_packages=known_package_names(PLSQL_ROOT)
    ).facts
    actual = json.dumps(facts.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    golden = GOLDEN_DIR / f"{facts.name.lower()}.facts.json"
    if os.environ.get("UPDATE_GOLDEN") == "1":
        golden.write_text(actual, encoding="utf-8", newline="\n")
    assert golden.exists(), f"missing golden file {golden.name}; run with UPDATE_GOLDEN=1"
    assert actual == golden.read_text(encoding="utf-8")
