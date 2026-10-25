"""Eval scoring and harness, offline (fake LLM)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from docgen.config import Settings
from docgen.eval.__main__ import main as eval_main
from docgen.eval.compare import comparison_markdown
from docgen.eval.ground_truth import load_ground_truth
from docgen.eval.harness import run_eval
from docgen.eval.results import EvalRun, PackageEval, load_runs, save_run
from docgen.eval.scoring import anchor_line, find_trap, score_trap
from docgen.extract import extract_package, find_all_packages, known_package_names
from docgen.generate import ResponseCache
from docgen.llm import FakeLLMClient, QuotaExhaustedError
from docgen.llm.fake import default_doc_from_user_message
from docgen.models import UnitDoc
from docgen.prompts import load_prompts
from tests.conftest import PLSQL_ROOT, REPO_ROOT

GT = load_ground_truth(REPO_ROOT / "eval" / "ground_truth.yaml")
TRAPS = {t.id: t for t in GT.traps}
NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def _ord_proc():
    pf = next(p for p in find_all_packages(PLSQL_ROOT) if p.label.endswith("ord_proc"))
    ext = extract_package(pf, root=REPO_ROOT, known_packages=known_package_names(PLSQL_ROOT))
    doc = UnitDoc.model_validate_json(
        (REPO_ROOT / "tests" / "fixtures" / "ord_proc.unitdoc.json").read_text(encoding="utf-8")
    )
    return ext, doc


def _pkg(doc: UnitDoc, raw: UnitDoc | None = None, **kw) -> PackageEval:
    return PackageEval(name="ORD_PROC", ok=True, doc=doc, raw_doc=raw or doc, **kw)


def test_fixture_doc_finds_the_ord_proc_traps():
    ext, doc = _ord_proc()
    lines = ext.body.text.split("\n")
    results = {
        tid: score_trap(TRAPS[tid], _pkg(doc), ext.facts, lines)
        for tid in ("T1-hidden-case-rule", "T2b-unreachable-branch", "T3-cross-package-call",
                    "T5a-swallowed-exception-invoice")
    }  # fmt: skip
    assert all(r.found for r in results.values())
    t1 = results["T1-hidden-case-rule"]
    assert t1.found_in == "business_rules" and t1.evidence_hit is True
    assert anchor_line(TRAPS["T1-hidden-case-rule"], ext.facts, lines) == 27
    assert results["T3-cross-package-call"].found_in == "main_flow"


def test_rule_trap_requires_matching_procedure_and_reports_evidence_miss():
    ext, doc = _ord_proc()
    lines = ext.body.text.split("\n")
    trap = TRAPS["T1-hidden-case-rule"]
    rule = doc.business_rules[0]
    wrong_proc = doc.model_copy(
        update={"business_rules": [rule.model_copy(update={"implemented_in": "SUBMIT_ORDER"})]}
    )
    assert find_trap(trap, wrong_proc) is None
    wrong_lines = doc.model_copy(
        update={"business_rules": [rule.model_copy(update={"evidence_lines": (22, 26)})]}
    )
    r = score_trap(trap, _pkg(wrong_lines), ext.facts, lines)
    assert r.found and r.evidence_hit is False


def test_missed_trap_and_synthesis_loss():
    ext, doc = _ord_proc()
    empty = doc.model_copy(update={"open_questions": []})
    r = score_trap(
        TRAPS["T2b-unreachable-branch"],
        _pkg(empty, unit_docs={"CANCEL_ORDER": doc}),
        ext.facts,
        ext.body.text.split("\n"),
    )
    assert not r.found and r.found_in_unit_doc is True
    assert "lost in synthesis" in _table_for(r)


def test_commented_out_logic_as_rule_is_a_hallucination():
    trap = TRAPS["T7-commented-out-block"]
    doc = UnitDoc.model_validate(
        {
            **json.loads(_ord_proc()[1].model_dump_json()),
            "business_rules": [
                {
                    "rule": "KMO customers get an extra 5% discount (factor 0.95).",
                    "implemented_in": "CREATE_INVOICE",
                    "evidence_lines": [1, 2],
                    "confidence": "medium",
                }
            ],
        }
    )
    r = score_trap(trap, _pkg(doc), _ord_proc()[0].facts, ["x"] * 200)
    assert r.hallucinated


def test_failed_package_marks_its_traps():
    r = score_trap(TRAPS["T1-hidden-case-rule"], PackageEval(name="ORD_PROC", ok=False), None, None)
    assert r.package_failed and not r.found


def _table_for(result) -> str:
    run = _dummy_run(traps=[result])
    return comparison_markdown([run])


def _dummy_run(**update) -> EvalRun:
    run = run_eval(
        GT,
        root=REPO_ROOT,
        settings=Settings(_env_file=None, llm_provider="fake"),
        client=FakeLLMClient(),
        prompts=load_prompts(),
        cache=ResponseCache(None),
        commit="abc",
        packages=["UTIL_LOG"],
        now=NOW,
    )
    return run.model_copy(update=update)


def test_offline_harness_run_and_round_trip(tmp_path):
    run = run_eval(
        GT,
        root=REPO_ROOT,
        settings=Settings(_env_file=None, llm_provider="fake"),
        client=FakeLLMClient(),
        prompts=load_prompts(),
        cache=ResponseCache(None),
        commit="abc",
        label="offline test",
        now=NOW,
    )
    s = run.summary
    assert s.packages == 4 and s.failed == 0
    assert s.traps_total == len(GT.traps)
    # The fake writes placeholder notes for every procedure and no rules/questions.
    assert s.notes_coverage_pct == 100.0 and s.tables_coverage_pct == 100.0
    assert s.traps_found == 0 and s.rules == 0
    assert run.run_id == f"20260102T030405Z_fake-llm_{load_prompts().version}_offline-test"
    path = save_run(run, tmp_path)
    [loaded] = load_runs([path])
    assert loaded == run
    md = comparison_markdown([loaded])
    assert "| Trap |" in md and "**missed**" in md and "0/9" in md


def test_unknown_package_is_rejected():
    with pytest.raises(ValueError, match="NOPE"):
        _dummy_run_packages(["NOPE"])


def _dummy_run_packages(packages):
    return run_eval(
        GT,
        root=REPO_ROOT,
        settings=Settings(_env_file=None, llm_provider="fake"),
        client=FakeLLMClient(),
        prompts=load_prompts(),
        cache=ResponseCache(None),
        commit="abc",
        packages=packages,
    )


def test_no_schema_variant_omits_schema_everywhere():
    p = load_prompts().without_schema_in_prompt()
    assert p.version.endswith("-noschema") and "<schema>" not in p.system + p.synthesis
    fake = FakeLLMClient()
    run_eval(
        GT,
        root=REPO_ROOT,
        settings=Settings(_env_file=None, llm_provider="fake", chunk_threshold_lines=100),
        client=fake,
        prompts=p,
        cache=ResponseCache(None),
        commit="abc",
        packages=["ORD_PROC"],
    )
    assert fake.calls and all("<schema>" not in c.user for c in fake.calls)
    default = load_prompts()
    assert p.fingerprint != default.fingerprint


def test_cli_run_and_compare(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(REPO_ROOT)
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    results = tmp_path / "results"
    assert eval_main(["--packages", "UTIL_LOG", "--results", str(results)]) == 0
    assert len(list(results.glob("*.json"))) == 1
    out = tmp_path / "cmp.md"
    assert eval_main(["compare", "--results", str(results), "--out", str(out)]) == 0
    assert "T5b-swallowed-exception-logging" in out.read_text(encoding="utf-8")


def test_fake_responder_can_find_traps():
    """Sanity: a doc that names the trap is scored as found end to end."""

    def responder(system, user):
        doc = default_doc_from_user_message(user)
        if "UTIL_LOG" in user:
            doc["open_questions"] = ["LOG_MSG uses WHEN OTHERS THEN NULL, so failures are lost."]
        return json.dumps(doc)

    run = run_eval(
        GT,
        root=REPO_ROOT,
        settings=Settings(_env_file=None, llm_provider="fake"),
        client=FakeLLMClient(responder=responder),
        prompts=load_prompts(),
        cache=ResponseCache(None),
        commit="abc",
        packages=["UTIL_LOG"],
    )
    [t5b] = run.traps
    assert t5b.found and t5b.found_in == "open_questions"


def test_rescore_recomputes_from_stored_output_and_keeps_original(tmp_path):
    run = _dummy_run_packages(["UTIL_LOG"])
    [pkg] = run.packages
    # Simulate a run scored by an older checker: inflate the stored violation count.
    stale = run.model_copy(
        update={"summary": run.summary.model_copy(update={"faithfulness_violations": 99})}
    )
    from docgen.eval.harness import rescore_run

    new = rescore_run(stale, GT, root=REPO_ROOT, settings=Settings(_env_file=None))
    assert new.summary.faithfulness_violations == 0
    assert new.original_summary.faithfulness_violations == 99 and new.rescored_at
    assert new.packages[0].raw_doc == pkg.raw_doc  # model output untouched
    again = rescore_run(new, GT, root=REPO_ROOT, settings=Settings(_env_file=None))
    assert again.original_summary.faithfulness_violations == 99  # first original kept
    assert "re-scored; originally 99 violations" in comparison_markdown([again])


def test_rescore_refuses_changed_source():
    run = _dummy_run_packages(["UTIL_LOG"])
    pkg = run.packages[0].model_copy(update={"source_hash": "different"})
    from docgen.eval.harness import rescore_run

    with pytest.raises(ValueError, match="changed since the run"):
        rescore_run(
            run.model_copy(update={"packages": [pkg]}),
            GT,
            root=REPO_ROOT,
            settings=Settings(_env_file=None),
        )


def test_resume_keeps_successful_packages_and_retries_failed():
    def responder(system, user):
        if "<unit_name>UTIL_LOG</unit_name>" in user:
            return "quota ran out"  # invalid twice -> package fails
        return json.dumps(default_doc_from_user_message(user))

    settings = Settings(_env_file=None, llm_provider="fake")
    common = dict(
        root=REPO_ROOT,
        settings=settings,
        prompts=load_prompts(),
        cache=ResponseCache(None),
        commit="abc",
        packages=["CUST_MGMT", "UTIL_LOG"],
    )
    first = run_eval(GT, client=FakeLLMClient(responder=responder), now=NOW, **common)
    assert first.summary.failed == 1

    second_client = FakeLLMClient()
    resumed = run_eval(GT, client=second_client, resume=first, **common)
    assert resumed.summary.failed == 0
    assert resumed.run_id == first.run_id and len(resumed.resumed_at) == 1
    assert [c.user.count("<unit_name>UTIL_LOG</unit_name>") for c in second_client.calls] == [1]

    with pytest.raises(ValueError, match="cannot resume"):
        run_eval(GT, client=FakeLLMClient(model_name="other"), resume=first, **common)
    low = {**common, "settings": Settings(_env_file=None, llm_provider="fake",
                                          gemini_thinking_level="low")}  # fmt: skip
    with pytest.raises(ValueError, match="gemini_thinking_level: None -> 'low'"):
        run_eval(GT, client=FakeLLMClient(), resume=first, **low)


def test_unavailable_model_stops_the_eval_run_and_can_be_resumed():
    def overloaded(system, user):
        raise QuotaExhaustedError("Gemini API error 429: daily quota exhausted")

    common = dict(
        root=REPO_ROOT,
        settings=Settings(_env_file=None, llm_provider="fake"),
        prompts=load_prompts(),
        cache=ResponseCache(None),
        commit="abc",
        packages=["CUST_MGMT", "UTIL_LOG"],
    )
    client = FakeLLMClient(responder=overloaded)
    first = run_eval(GT, client=client, now=NOW, **common)
    assert len(client.calls) == 1
    cust, util = first.packages
    assert cust.provider_unavailable and not cust.ok
    assert not util.ok and util.error.startswith("not attempted") and util.calls == []

    resumed = run_eval(GT, client=FakeLLMClient(), resume=first, **common)
    assert resumed.summary.failed == 0
