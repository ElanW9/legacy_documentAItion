import json

from docgen.llm import FakeLLMClient, LLMClient
from docgen.models import UnitDoc


def test_fake_satisfies_protocol():
    assert isinstance(FakeLLMClient(), LLMClient)


def test_default_response_is_valid_and_covers_facts():
    facts = {"subprograms": [{"name": "A"}, {"name": "B"}]}
    user = f"<unit_name>PKG</unit_name><facts>{json.dumps(facts)}</facts>"
    resp = FakeLLMClient().generate_json(system="s", user=user, response_model=UnitDoc)
    doc = UnitDoc.model_validate_json(resp.text)
    assert [n.name for n in doc.procedure_notes] == ["A", "B"]


def test_scripted_responses_are_consumed_in_order_and_recorded():
    fake = FakeLLMClient.scripted(["not json", {"x": 1}])
    assert fake.generate_json(system="s", user="u1", response_model=UnitDoc).text == "not json"
    assert json.loads(fake.generate_json(system="s", user="u2", response_model=UnitDoc).text)
    assert [c.user for c in fake.calls] == ["u1", "u2"]
