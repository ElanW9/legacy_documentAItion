from __future__ import annotations

import json
import re

import pytest

from docgen.extract import extract_package, find_all_packages, known_package_names
from docgen.generate import GenerationError, ResponseCache, generate_package_doc
from docgen.llm import FakeLLMClient, LLMError
from docgen.llm.fake import default_doc_from_user_message
from docgen.prompts import Prompts, load_prompts, number_lines
from tests.conftest import PLSQL_ROOT, REPO_ROOT

PROMPTS = load_prompts()


def _extracted(suffix: str):
    pf = next(p for p in find_all_packages(PLSQL_ROOT) if p.label.endswith(suffix))
    return extract_package(pf, root=REPO_ROOT, known_packages=known_package_names(PLSQL_ROOT))


def _gen(client, suffix="cust_mgmt", threshold=1000, cache=None):
    return generate_package_doc(
        _extracted(suffix),
        client,
        PROMPTS,
        chunk_threshold_lines=threshold,
        cache=cache or ResponseCache(None),
    )


def test_prompts_load_and_fingerprint_changes():
    assert (
        PROMPTS.version
        == (REPO_ROOT / "src" / "docgen" / "prompts" / "VERSION").read_text().strip()
    )
    assert "functional analysts" in PROMPTS.system
    edited = Prompts(PROMPTS.version, PROMPTS.system + " ", PROMPTS.synthesis)
    assert edited.fingerprint != PROMPTS.fingerprint


def test_number_lines_keeps_original_numbers():
    assert number_lines(["a", "b", "c", "d"], 2, 3) == "2 | b\n3 | c"
    assert number_lines([str(i) for i in range(12)], 9, 10).splitlines() == [" 9 | 8", "10 | 9"]


def test_small_package_is_one_call_with_tagged_message():
    fake = FakeLLMClient()
    result = _gen(fake)
    assert result.mode == "whole" and len(fake.calls) == 1
    user = fake.calls[0].user
    for tag in ("unit_name", "kind", "facts", "source", "schema"):
        assert f"<{tag}>" in user and f"</{tag}>" in user
    assert "<unit_name>CUST_MGMT</unit_name>" in user
    facts = json.loads(re.search(r"<facts>(.*)</facts>", user, re.S).group(1))
    assert "source_hash" not in facts
    assert fake.calls[0].system == PROMPTS.system
    assert {n.name for n in result.doc.procedure_notes} == {
        s.name for s in _extracted("cust_mgmt").facts.subprograms
    }


def test_large_package_is_chunked_then_synthesised():
    ext = _extracted("ord_proc")
    fake = FakeLLMClient()
    result = _gen(fake, "ord_proc", threshold=100)
    subs = [s for s in ext.facts.subprograms if s.start_line]
    assert result.mode == "chunked"
    assert len(fake.calls) == len(subs) + 1
    assert set(result.unit_docs) == {s.name for s in subs}
    assert fake.calls[-1].system == PROMPTS.synthesis
    assert "<unit_docs>" in fake.calls[-1].user and "<source>" not in fake.calls[-1].user

    cancel = next(s for s in subs if s.name == "CANCEL_ORDER")
    user = next(c.user for c in fake.calls if "ORD_PROC.CANCEL_ORDER" in c.user)
    source = re.search(r"<source>\n(.*)\n</source>", user, re.S).group(1)
    numbered = [int(m) for m in re.findall(r"^\s*(\d+) \| ", source, re.M)]
    first = min(s.start_line for s in subs)
    assert numbered[: first - 1] == list(range(1, first))  # package declarations
    assert numbered[first - 1 :] == list(range(cancel.start_line, cancel.end_line + 1))
    assert "omitted: other subprograms" in source
    lines = ext.body.text.split("\n")
    assert f"{cancel.start_line} | {lines[cancel.start_line - 1]}" in source


def test_invalid_output_is_retried_once_with_errors():
    fake = FakeLLMClient(responses=['{"purpose": 1}'])
    result = _gen(fake)
    assert len(fake.calls) == 2
    assert "<previous_answer_errors>" in fake.calls[1].user
    assert "source_complete" in fake.calls[1].user  # names the missing field
    assert result.calls[0].validation_retries == 1


def test_invalid_twice_fails_the_unit():
    fake = FakeLLMClient(responses=["not json", '{"still": "wrong"}'])
    with pytest.raises(GenerationError, match="failed schema validation twice") as exc:
        _gen(fake)
    assert exc.value.calls and exc.value.calls[0].validation_retries == 1


def test_llm_error_becomes_generation_error():
    def boom(system, user):
        raise LLMError("Gemini API error 400 INVALID_ARGUMENT")

    with pytest.raises(GenerationError, match="400"):
        _gen(FakeLLMClient(responder=boom))


def test_validated_answers_are_cached(tmp_path):
    cache = ResponseCache(tmp_path)
    first = FakeLLMClient()
    r1 = _gen(first, cache=cache)
    second = FakeLLMClient()
    r2 = _gen(second, cache=cache)
    assert len(first.calls) == 1 and second.calls == []
    assert r2.calls[0].cached and r2.doc == r1.doc
    # A different model is a different cache entry.
    third = FakeLLMClient(model_name="other")
    _gen(third, cache=cache)
    assert len(third.calls) == 1


def test_invalid_answers_are_not_cached(tmp_path):
    with pytest.raises(GenerationError):
        _gen(FakeLLMClient(responses=["x", "y"]), cache=ResponseCache(tmp_path))
    assert list(tmp_path.iterdir()) == []


def test_fake_default_doc_is_schema_valid():
    from docgen.models import UnitDoc

    UnitDoc.model_validate(default_doc_from_user_message("<facts>{}</facts>"))
