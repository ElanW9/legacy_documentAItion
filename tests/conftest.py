from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PLSQL_ROOT = REPO_ROOT / "plsql"
GOLDEN_DIR = Path(__file__).resolve().parent / "golden"


@pytest.fixture
def repo_root() -> Path:
    return REPO_ROOT
