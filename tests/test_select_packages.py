"""scripts/select-packages.sh (CI package selection) against a throwaway git repo."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "select-packages.sh"
BASH, GIT = shutil.which("bash"), shutil.which("git")

pytestmark = pytest.mark.skipif(not (BASH and GIT), reason="needs bash and git")

ZEROS = "0" * 40
GIT_ENV = {
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        [GIT, *args], cwd=repo, env={**os.environ, **GIT_ENV}, capture_output=True, text=True
    )
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def _commit(repo: Path, files: dict[str, str]) -> str:
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    _git(repo, "add", "--", *files)
    _git(repo, "commit", "-q", "-m", "change")
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, str, str]:
    """A repo with two commits; the second changes one package body and the README."""
    _git(tmp_path, "init", "-q")
    first = _commit(tmp_path, {"plsql/a.pkb": "1\n", "plsql/b.pkb": "1\n", "README.md": "1\n"})
    second = _commit(tmp_path, {"plsql/a.pkb": "2\n", "README.md": "2\n"})
    return tmp_path, first, second


def _select(repo: Path, tmp_path: Path, **env: str) -> tuple[int, str, str, str]:
    """Run the script; returns (exit code, stdout, GITHUB_OUTPUT text, changed.txt text)."""
    output = tmp_path / "github_output"
    output.write_text("")
    keep = {k: v for k, v in os.environ.items() if k in ("PATH", "SYSTEMROOT", "TEMP", "TMP")}
    out = subprocess.run(
        [BASH, SCRIPT.as_posix()],
        cwd=repo,
        env={**keep, **GIT_ENV, "GITHUB_OUTPUT": output.as_posix(), **env},
        capture_output=True,
        text=True,
    )
    changed = repo / "changed.txt"
    return (
        out.returncode,
        out.stdout.strip(),
        output.read_text(),
        changed.read_text() if changed.exists() else "",
    )


def test_push_diffs_against_the_previous_push(repo, tmp_path):
    root, first, second = repo
    code, args, gh_out, changed = _select(
        root, tmp_path, EVENT_NAME="push", BEFORE=first, HEAD_SHA=second
    )
    assert (code, args) == (0, "--files changed.txt")
    assert gh_out == "args=--files changed.txt\n"
    assert changed.split() == ["README.md", "plsql/a.pkb"]


@pytest.mark.parametrize("before", ["", ZEROS, "deadbeef" * 5])
def test_push_without_a_known_previous_commit_checks_all_pages(repo, tmp_path, before):
    root, _, second = repo
    code, args, gh_out, changed = _select(
        root, tmp_path, EVENT_NAME="push", BEFORE=before, HEAD_SHA=second
    )
    assert (code, args, gh_out, changed) == (0, "--all", "args=--all\n", "")


def test_pull_request_diffs_against_the_base(repo, tmp_path):
    root, first, _ = repo
    code, args, _, changed = _select(root, tmp_path, EVENT_NAME="pull_request", BASE_SHA=first)
    assert (code, args) == (0, "--files changed.txt")
    assert changed.split() == ["README.md", "plsql/a.pkb"]


def test_pull_request_with_an_unknown_base_fails(repo, tmp_path):
    root, _, _ = repo
    code, _, gh_out, _ = _select(root, tmp_path, EVENT_NAME="pull_request", BASE_SHA=ZEROS)
    assert code != 0 and gh_out == ""


@pytest.mark.parametrize(("full_run", "expected"), [("", "--all"), ("false", "--all"),
                                                    ("true", "--all --force")])  # fmt: skip
def test_manual_run(repo, tmp_path, full_run, expected):
    root, _, _ = repo
    code, args, gh_out, _ = _select(
        root, tmp_path, EVENT_NAME="workflow_dispatch", FULL_RUN=full_run
    )
    assert (code, args, gh_out) == (0, expected, f"args={expected}\n")


def test_unknown_event_fails(repo, tmp_path):
    root, _, _ = repo
    code, _, gh_out, _ = _select(root, tmp_path, EVENT_NAME="schedule")
    assert code != 0 and gh_out == ""
