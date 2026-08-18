"""Where parameter order and identity come from today.

Issue #44 asks to verify first: does the agent take parameter order from the
request or from the recipe? These record the answers before anything changes.

The short answers: from the request, so two identical workflows can produce
different commands; and the model does not carry the declared type at all, so
the emitter cannot tell a flag from a value.
"""

import json

import pytest

import convert
from intermediate import Param


BUNDLE = {
    "id": "t", "name": "t", "bin": "t",
    "io": {"inputs": [{"name": "in", "types": ["T"], "mode": "file"}],
           "outputs": [{"name": "out", "types": ["T"], "mode": "stdout"}]},
    # Declared deliberately out of alphabetical order, so declaration order and
    # sorted order cannot be confused for one another.
    "parameters": [
        {"name": "zeta", "type": "string", "flag": "-z"},
        {"name": "mu", "type": "string", "flag": "-u"},
        {"name": "alpha", "type": "string", "flag": "-a"},
    ],
}


@pytest.fixture(autouse=True)
def stub_registry(monkeypatch):
    monkeypatch.setattr(convert, "fetch_tool", lambda tool_id, repo: BUNDLE)
    convert.tools.clear()


def emit(param_values, bundle=None):
    if bundle is not None:
        convert.fetch_tool = lambda tool_id, repo: bundle
        convert.tools.clear()
    export = {
        "nodes": [
            {"id": "input-1", "type": "inputWorkflowNode",
             "data": {"outputs": {"out": {"kind": "text", "data": "x"}}}},
            {"id": "t-1", "type": "workflowNode",
             "data": {"label": "t", "repo": "r", "outputs": {},
                      "paramValues": param_values}},
            {"id": "output-1", "type": "outputWorkflowNode", "data": {}},
        ],
        "edges": [
            {"source": "input-1", "sourceHandle": "out",
             "target": "t-1", "targetHandle": "in"},
            {"source": "t-1", "sourceHandle": "out",
             "target": "output-1", "targetHandle": "in"},
        ],
    }
    text = convert.convert_to_snakemake(convert.parse_biochef_workflow(export))
    lines = text.splitlines()
    return json.loads(lines[[l.strip() for l in lines].index("shell:") + 1].strip())


ALL_THREE = {"zeta": {"enabled": True, "value": "Z"},
             "mu": {"enabled": True, "value": "M"},
             "alpha": {"enabled": True, "value": "A"}}


def test_order_comes_from_the_request_not_the_recipe():
    """#44. The same workflow, serialised differently, is a different command."""
    declared = emit({k: ALL_THREE[k] for k in ("zeta", "mu", "alpha")})
    shuffled = emit({k: ALL_THREE[k] for k in ("alpha", "mu", "zeta")})

    assert declared != shuffled, "order no longer follows the payload"
    assert declared.index("-z") < declared.index("-a")
    assert shuffled.index("-a") < shuffled.index("-z")


def test_an_unflagged_value_is_emitted_before_the_flags():
    """#49. A bare value ahead of a flag stops a getopt-style parser scanning."""
    bundle = dict(BUNDLE)
    bundle["parameters"] = [{"name": "positional", "type": "string"},
                            {"name": "zeta", "type": "string", "flag": "-z"}]

    command = emit({"positional": {"enabled": True, "value": "POSVAL"},
                    "zeta": {"enabled": True, "value": "Z"}}, bundle)

    assert command.index("POSVAL") < command.index("-z"), \
        "the unflagged value no longer precedes the flags"


def test_the_model_does_not_carry_the_declared_type():
    """Which is why the emitter cannot tell a flag parameter from a value one.

    RecipePanel.js:590 pushes only `param.flag` when the type is "flag". The
    agent has no equivalent because there is nothing to branch on.
    """
    assert list(Param.model_fields) == ["name", "value", "flag"]


def test_a_parameter_the_recipe_does_not_declare_raises_deep_in_the_parse():
    """Recorded, not endorsed: it surfaces as StopIteration rather than a 4xx."""
    with pytest.raises(StopIteration):
        emit({"not-declared": {"enabled": True, "value": "x"}})
