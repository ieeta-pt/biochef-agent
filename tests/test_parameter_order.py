"""Parameter order and identity come from the recipe (#44, #49).

The commit before this one recorded the opposite: order came from whatever the
client serialised, an unflagged value was emitted ahead of every flag, and the
model had no declared type to branch on. Each test here is the closed form of
one of those.

The reference is the frontend, which has always done this correctly.
`RecipePanel.js:584` walks `toolDefinition.parameters` -- the recipe -- and
`:590` emits only the flag when the declared type is `flag`.
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


def test_order_comes_from_the_recipe_whatever_the_request_says():
    """#44. Two serialisations of one workflow are now one command."""
    declared = emit({k: ALL_THREE[k] for k in ("zeta", "mu", "alpha")})
    shuffled = emit({k: ALL_THREE[k] for k in ("alpha", "mu", "zeta")})
    reversed_ = emit({k: ALL_THREE[k] for k in ("mu", "alpha", "zeta")})

    assert declared == shuffled == reversed_

    # and it is the RECIPE's order, not sorted and not the payload's
    assert declared.index("-z") < declared.index("-u") < declared.index("-a")


def test_a_parameter_the_recipe_does_not_declare_is_ignored():
    """It cannot be emitted -- there is no flag or type for it -- and raising
    StopIteration from inside the parse named nothing useful."""
    command = emit({"not-declared": {"enabled": True, "value": "x"},
                    "zeta": {"enabled": True, "value": "Z"}})

    assert "x" not in command
    assert "-z Z" in command


def test_a_disabled_parameter_is_still_omitted():
    command = emit({"zeta": {"enabled": False, "value": "Z"},
                    "mu": {"enabled": True, "value": "M"}})

    assert "-z" not in command
    assert "-u M" in command


# --------------------------------------------------------------------------
# what the declared type buys


def test_a_flag_parameter_contributes_only_its_flag():
    """#33's neighbour, and what RecipePanel.js:590 has always done.

    469 of the catalogue's 1107 parameters are declared `flag`. Emitting a value
    for one hands the tool an argument it does not take.
    """
    bundle = dict(BUNDLE)
    bundle["parameters"] = [{"name": "verbose", "type": "flag", "flag": "-v"}]

    command = emit({"verbose": {"enabled": True, "value": True}}, bundle)

    assert "-v" in command
    assert "True" not in command, "a flag parameter must not carry a value"


def test_an_unflagged_value_keeps_its_declared_position():
    """#49 was wrong, and this records why.

    That issue argued an unflagged parameter value should be held back after the
    flags, by analogy with the getopt rule that applies to filenames. It does not
    apply here. Every unflagged parameter in the catalogue is a hidden
    subcommand declared first -- `command: consensus` in
    `bcftools consensus -c ...` -- and moving it after the flags breaks the
    invocation outright. The frontend has always emitted these in place.
    """
    bundle = dict(BUNDLE)
    bundle["parameters"] = [{"name": "command", "type": "string", "hidden": True},
                            {"name": "zeta", "type": "string", "flag": "-z"}]

    command = emit({"command": {"enabled": True, "value": "consensus"},
                    "zeta": {"enabled": True, "value": "Z"}}, bundle)

    assert command.index("consensus") < command.index("-z"), \
        "a subcommand must lead, not trail"
    assert command.startswith("./t consensus -z Z")


def test_the_model_carries_the_declared_type():
    assert list(Param.model_fields) == ["name", "value", "flag", "type"]


def test_a_document_written_before_the_type_existed_still_validates():
    """Why the bump is 1.1.0 and not 2.0.0: the field is additive."""
    from intermediate import Workflow

    document = Workflow.model_validate({
        "schemaVersion": "1.0.0",
        "nodes": [{"id": "t", "bin": "t",
                   "parameters": {"p": {"name": "p", "value": "v", "flag": "-p"}}}],
    })

    assert document.nodes[0].parameters["p"].type is None
