"""The intermediate model, and the properties the dataclasses could not offer.

The commit before this one recorded what was there previously: four plain
dataclasses, no version on any of them, and a representation that raised
`TypeError: Object of type IOMode is not JSON serializable` the moment anything
tried to write it down. Each test here is the closed form of one of those
findings.
"""

import json
from pathlib import Path

import pydantic
import pytest

import convert
from intermediate import SCHEMA_VERSION, IO, IOMode, Node, Param, Workflow
from tests.fixtures.tools import BUNDLES

CONTRACT = Path(__file__).resolve().parent.parent / "contracts" / "intermediate.schema.json"


@pytest.fixture(autouse=True)
def stub_registry(monkeypatch):
    monkeypatch.setattr(
        convert, "fetch_tool", lambda tool_id, repo: BUNDLES[tool_id.split("-")[0]]
    )


def a_workflow():
    return convert.parse_biochef_workflow(
        {
            "nodes": [
                {"id": "input-1", "type": "inputWorkflowNode",
                 "data": {"outputs": {"out": {"kind": "text", "data": "x"}}}},
                {"id": "tn93.distance-1", "type": "workflowNode",
                 "data": {"label": "t", "repo": "r", "paramValues": {}, "outputs": {}}},
                {"id": "output-1", "type": "outputWorkflowNode", "data": {}},
            ],
            "edges": [
                {"source": "input-1", "sourceHandle": "out",
                 "target": "tn93.distance-1", "targetHandle": "in"},
                {"source": "tn93.distance-1", "sourceHandle": "out",
                 "target": "output-1", "targetHandle": "in"},
            ],
        }
    )


def test_the_document_carries_its_schema_version():
    assert a_workflow().schemaVersion == SCHEMA_VERSION
    assert json.loads(a_workflow().to_json())["schemaVersion"] == SCHEMA_VERSION


def test_the_representation_can_now_be_written_down():
    """The finding this PR exists to close.

    The same call that raised TypeError against the dataclasses now produces a
    document, and reading it back gives an equal one.
    """
    workflow = a_workflow()
    text = workflow.to_json()

    json.loads(text)  # would raise if it were not valid JSON
    assert Workflow.from_json(text) == workflow


def test_a_document_is_written_deterministically():
    """Two runs of one workflow give byte-identical files, so a diff is meaningful."""
    assert a_workflow().to_json() == a_workflow().to_json()
    assert a_workflow().to_json().endswith("\n")


def test_parameter_values_keep_the_type_the_editor_sent():
    """Deliberately not coerced to str.

    A recipe default of 2 arrives as JSON 2, and the emitter then raises
    TypeError for 37 of the 176 catalogue operations (#37). Coercing here would
    hide that rather than fix it, and would make this change something other
    than behaviour preserving.
    """
    node = Node(id="t", bin="t", parameters={
        "i": Param(name="i", value=2),
        "f": Param(name="f", value=1.5),
        "b": Param(name="b", value=True),
        "s": Param(name="s", value="NW"),
    })
    back = Workflow.from_json(Workflow(nodes=[node]).to_json()).nodes[0].parameters

    assert back["i"].value == 2 and isinstance(back["i"].value, int)
    assert back["f"].value == 1.5 and isinstance(back["f"].value, float)
    assert back["b"].value is True
    assert back["s"].value == "NW"


def test_an_unknown_field_is_rejected():
    """extra="forbid", so a document from a newer Agent is refused, not half read."""
    with pytest.raises(pydantic.ValidationError, match="bogus"):
        Workflow.model_validate({"schemaVersion": SCHEMA_VERSION, "nodes": [], "bogus": 1})


def test_an_invalid_mode_is_rejected():
    with pytest.raises(pydantic.ValidationError, match="mode"):
        Workflow.model_validate(
            {"nodes": [{"id": "x", "bin": "y", "inputs": {"a": {"mode": "nonsense"}}}]}
        )


def test_none_is_not_accepted_where_a_string_is_declared():
    """Why build_io defaults with `or ""`.

    A recipe that omits "filename" made .get return None, which the dataclass
    stored happily. The model rejects it, so the parse has to be explicit about
    the empty case.
    """
    with pytest.raises(pydantic.ValidationError):
        IO(file="x", mode=IOMode.FILE, hardcoded_file=None)


def test_the_checked_in_contract_matches_the_models():
    """Guards against the schema file and the code drifting apart.

    A contract that no longer describes the thing it names is worse than no
    contract, because it is believed.
    """
    on_disk = json.loads(CONTRACT.read_text())
    generated = Workflow.model_json_schema()

    assert on_disk["$defs"] == generated["$defs"], "contracts/ is stale: regenerate it"
    assert on_disk["properties"] == generated["properties"]
    assert on_disk["title"] and on_disk["$id"] and on_disk["$schema"]
