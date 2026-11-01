"""How a parameter reaches the command line.

lastz takes its options as --format=general and rejects --format= followed by
general as a separate word. The recipe declares the flag as "--format=", and
the browser joins the value to it; the agent must build the same command, or a
workflow that runs in the browser fails here.
"""

import json

import pytest

import convert


BUNDLE = {
    "id": "t", "name": "t", "bin": "t",
    "io": {"inputs": [{"name": "in", "types": ["T"], "mode": "file"}],
           "outputs": [{"name": "out", "types": ["T"], "mode": "stdout"}]},
    "parameters": [
        {"name": "format", "type": "string", "flag": "--format="},
        {"name": "preset", "type": "string", "flag": "-x"},
    ],
}


@pytest.fixture(autouse=True)
def stub_registry(monkeypatch):
    monkeypatch.setattr(convert, "fetch_tool", lambda tool_id, repo: BUNDLE)
    convert.tools.clear()


def emit(param_values):
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


def test_a_flag_ending_in_equals_takes_its_value_in_the_same_argument():
    command = emit({"format": {"enabled": True, "value": "general"}})

    assert command.startswith("./t --format=general ")


def test_any_other_flag_takes_its_value_as_the_next_argument():
    command = emit({"preset": {"enabled": True, "value": "asm5"}})

    assert command.startswith("./t -x asm5 ")


def test_the_joined_argument_is_still_one_shell_word():
    command = emit({"format": {"enabled": True, "value": "a b;id"}})

    assert command.startswith("./t '--format=a b;id' ")


def test_an_empty_value_drops_the_flag_too():
    command = emit({"format": {"enabled": True, "value": ""}})

    assert "--format" not in command
