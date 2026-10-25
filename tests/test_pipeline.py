"""End-to-end offline runs: fake LLM, dry-run / in-memory publishers, CLI."""

from __future__ import annotations

import json
import shutil
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

import pytest

from docgen.__main__ import main
from docgen.config import Settings
from docgen.generate import ResponseCache
from docgen.llm import FakeLLMClient, OverloadedError
from docgen.llm.fake import default_doc_from_user_message
from docgen.pipeline import all_packages, cache_key, run, select_packages
from docgen.prompts import load_prompts
from docgen.publish import DryRunPublisher, PageState, PublishResult
from docgen.render import BANNER_TEXT
from docgen.report import render_report
from tests.conftest import REPO_ROOT

PROMPTS = load_prompts()
NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
SETTINGS = Settings(_env_file=None, llm_provider="fake")


class MemoryPublisher:
    """Keeps published pages in memory, like Confluence would."""

    def __init__(self) -> None:
        self.pages: dict[str, dict] = {}

    def get_page_state(self, title: str) -> PageState | None:
        p = self.pages.get(title)
        return PageState(p["id"], title, p["version"], p["metadata"]) if p else None

    def publish(
        self,
        *,
        title: str,
        body: str,
        metadata: Mapping[str, str],
        labels: Sequence[str],
        existing: PageState | None,
        version_message: str,
    ) -> PublishResult:
        version = existing.version + 1 if existing else 1
        self.pages[title] = {
            "id": str(len(self.pages) + 1) if not existing else existing.page_id,
            "version": version,
            "metadata": dict(metadata),
            "body": body,
            "labels": list(labels),
        }
        return PublishResult("updated" if existing else "created", title, self.pages[title]["id"])


def _run(selection, publisher, client=None, **kw):
    return run(
        selection,
        root=REPO_ROOT,
        settings=SETTINGS,
        client=client or FakeLLMClient(),
        prompts=PROMPTS,
        publisher=publisher,
        cache=ResponseCache(None),
        commit="abc123",
        dry_run=False,
        now=NOW,
        **kw,
    )


def test_select_packages_pairs_siblings_and_reports_deleted_and_ignored():
    sel = select_packages(
        [
            "plsql/orders/ord_proc.pks",
            "plsql\\billing\\bill_calc.pkb",
            "plsql/gone/old_pkg.pkb",
            "scripts/deploy.sh",
            "README.md",
            "",
        ],
        root=REPO_ROOT,
        plsql_root=SETTINGS.plsql_root,
    )
    labels = sorted(p.label.replace("\\", "/").rsplit("/", 1)[-1] for p in sel.packages)
    assert labels == ["bill_calc", "ord_proc"]
    assert all(p.spec and p.body for p in sel.packages)  # unchanged sibling pulled in
    assert sel.deleted == ["plsql/gone/old_pkg.pkb"]
    assert sel.ignored == ["scripts/deploy.sh", "README.md"]


def test_full_run_publishes_every_package_then_skips_unchanged():
    pub = MemoryPublisher()
    sel = all_packages(root=REPO_ROOT, plsql_root=SETTINGS.plsql_root)
    first = _run(sel, pub)
    assert [o.status for o in first.outcomes] == ["published"] * len(sel.packages)
    assert len(pub.pages) == len(sel.packages)
    page = pub.pages["ORD_PROC (PL/SQL package)"]
    assert BANNER_TEXT in page["body"] and page["labels"] == ["ai-generated"]
    assert page["metadata"]["commit"] == "abc123"
    assert page["metadata"]["prompt_version"] == PROMPTS.version

    client = FakeLLMClient()
    second = _run(sel, pub, client=client)
    assert [o.status for o in second.outcomes] == ["skipped"] * len(sel.packages)
    assert client.calls == []

    forced = _run(sel, pub, force=True)
    assert {o.action for o in forced.outcomes} == {"updated"}


def test_cache_key_depends_on_model_prompts_and_source():
    base = dict(source_hash="h", prompts=PROMPTS, model="m", chunk_threshold=150)
    k = cache_key(**base)
    assert k != cache_key(**{**base, "model": "m2"})
    assert k != cache_key(**{**base, "source_hash": "h2"})
    assert k != cache_key(**{**base, "chunk_threshold": 10})
    edited = type(PROMPTS)(PROMPTS.version, PROMPTS.system + "!", PROMPTS.synthesis)
    assert k != cache_key(**{**base, "prompts": edited})


def test_one_failing_package_does_not_stop_the_run():
    def responder(system, user):
        if "<unit_name>CUST_MGMT</unit_name>" in user:
            return "this is not json"
        return json.dumps(default_doc_from_user_message(user))

    pub = MemoryPublisher()
    sel = all_packages(root=REPO_ROOT, plsql_root=SETTINGS.plsql_root)
    result = _run(sel, pub, client=FakeLLMClient(responder=responder))
    failed = [o.name for o in result.failed]
    assert failed == ["CUST_MGMT"]
    assert "CUST_MGMT (PL/SQL package)" not in pub.pages
    assert len(pub.pages) == len(sel.packages) - 1

    report = render_report(result)
    assert "**FAILED**" in report and "## Failures" in report
    assert "failed schema validation twice" in report


def test_unavailable_model_stops_calls_for_the_rest_of_the_run():
    pub = MemoryPublisher()
    sel = all_packages(root=REPO_ROOT, plsql_root=SETTINGS.plsql_root)
    _run(sel, pub)  # every page exists and is up to date
    changed = sel.packages[1].body
    assert changed is not None

    def overloaded(system, user):
        raise OverloadedError("Gemini API error 503 UNAVAILABLE: high demand")

    client = FakeLLMClient(responder=overloaded)
    result = _run(sel, pub, client=client, force=True)
    statuses = [o.status for o in result.outcomes]
    assert statuses[0] == "failed" and result.outcomes[0].provider_unavailable
    assert statuses[1:] == ["not_attempted"] * (len(sel.packages) - 1)
    assert len(client.calls) == 1  # no requests after the first failure

    # Unchanged packages are still skipped (no model needed); changed ones wait.
    no_force = _run(sel, pub, client=FakeLLMClient(responder=overloaded))
    assert {o.status for o in no_force.outcomes} == {"skipped"}

    report = render_report(result)
    assert "1 failed, 3 not attempted" in report and "**NOT ATTEMPTED**" in report


def test_check_findings_reach_page_and_report():
    def responder(system, user):
        doc = default_doc_from_user_message(user)
        doc["open_questions"] = ["Is HALLUCINATED_TABLE still used?"]
        return json.dumps(doc)

    pub = MemoryPublisher()
    sel = select_packages(
        ["plsql/util/util_log.pkb"], root=REPO_ROOT, plsql_root=SETTINGS.plsql_root
    )
    result = _run(sel, pub, client=FakeLLMClient(responder=responder))
    [outcome] = result.outcomes
    assert outcome.checks is not None and outcome.checks.count("faithfulness") == 1
    assert "HALLUCINATED_TABLE" in pub.pages["UTIL_LOG (PL/SQL package)"]["body"]
    assert "Automated checks" in pub.pages["UTIL_LOG (PL/SQL package)"]["body"]
    report = render_report(result)
    assert "## Check findings" in report and "HALLUCINATED_TABLE" in report


def test_report_never_contains_secrets():
    secret = "super-secret-value-123"
    settings = Settings(
        _env_file=None, llm_provider="fake", gemini_api_key=secret, atlassian_token=secret
    )
    sel = all_packages(root=REPO_ROOT, plsql_root=settings.plsql_root)
    result = run(
        sel,
        root=REPO_ROOT,
        settings=settings,
        client=FakeLLMClient(),
        prompts=PROMPTS,
        publisher=MemoryPublisher(),
        cache=ResponseCache(None),
        commit="abc",
        dry_run=False,
        now=NOW,
    )
    assert secret not in render_report(result)


# --------------------------------------------------------------------------- CLI


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    """A copy of the PL/SQL tree in a temp dir, so the CLI runs without a .env."""
    shutil.copytree(REPO_ROOT / "plsql", tmp_path / "plsql")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_cli_dry_run_with_fake_llm(workdir, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    (workdir / "changed.txt").write_text("plsql/orders/ord_proc.pkb\nREADME.md\n")
    code = main(
        [
            "run",
            "--files",
            "changed.txt",
            "--report",
            "report.md",
            "--dry-run",
            "--commit",
            "deadbeef",
        ]
    )
    assert code == 0
    report = (workdir / "report.md").read_text(encoding="utf-8")
    assert "dry run" in report and "`ORD_PROC`" in report and "deadbeef" in report
    assert (workdir / "out" / "ord_proc_pl_sql_package.storage.xml").exists()
    assert list((workdir / ".cache" / "docgen").glob("*.json"))


def test_cli_fails_fast_on_missing_config(workdir, capsys):
    code = main(["run", "--all", "--report", "report.md", "--dry-run"])
    assert code == 2
    assert not (workdir / "report.md").exists()


def test_cli_publish_mode_requires_confluence_settings(workdir, monkeypatch, caplog):
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    assert main(["run", "--all", "--report", "report.md"]) == 2
    assert "ATLASSIAN_TOKEN" in caplog.text


def test_dry_run_publisher_never_skips(tmp_path):
    assert DryRunPublisher(tmp_path).get_page_state("x") is None
