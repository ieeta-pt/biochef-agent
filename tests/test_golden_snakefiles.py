"""The whole Snakefile for each pattern that is hard to convert (#3).

The other converter tests assert a line of the output, so a change elsewhere in
the rule goes unnoticed. These compare every byte against a file in
tests/fixtures/golden, covering the five patterns the issue names. A fixed
filename has two entries, because the recipes write it in two ways.

A difference here is either a regression or a deliberate change of the emitted
text. In the second case the golden file changes in the same commit, so the
change shows up in review.
"""

from pathlib import Path

import pytest

import convert

GOLDEN = Path(__file__).parent / "fixtures" / "golden"


def _tool(tool_id, inputs, outputs, parameters=()):
    return {"id": tool_id, "name": tool_id, "bin": "t",
            "parameters": list(parameters),
            "io": {"inputs": inputs, "outputs": outputs}}


FILE_IN = {"name": "in", "types": ["T"], "mode": "file"}
STDOUT = {"name": "out", "types": ["T"], "mode": "stdout"}

BUNDLES = {
    "pair": _tool(
        "pair",
        [{"name": "a", "types": ["T"], "mode": "file", "flag": "-a"},
         {"name": "b", "types": ["T"], "mode": "file"}],
        [{"name": "out", "types": ["T"], "mode": "file", "flag": "-o"}]),
    "split": _tool("split", [FILE_IN], [STDOUT]),
    "piped": _tool(
        "piped",
        [{"name": "in", "types": ["T"], "mode": "stdin"}],
        [STDOUT]),
    "sub": _tool(
        "sub", [FILE_IN], [STDOUT],
        [{"name": "command", "type": "string", "default": "run",
          "required": True, "hidden": True},
         {"name": "level", "type": "integer", "flag": "-l"}]),
    "fixed": _tool(
        "fixed", [FILE_IN],
        [{"name": "out", "types": ["T"], "mode": "file", "flag": "-o",
          "filename": "result.txt"}]),
    # ivar.trim and five other ivar operations.
    "unflagged": _tool(
        "unflagged", [FILE_IN],
        [{"name": "out", "types": ["T"], "mode": "file",
          "filename": "result.txt"}]),
}


@pytest.fixture(autouse=True)
def stub_registry(monkeypatch):
    monkeypatch.setattr(
        convert, "fetch_tool",
        lambda tool_id, repo: BUNDLES[tool_id.rsplit("-", 1)[0]])
    convert.tools.clear()


def _input(node_id):
    return {"id": node_id, "type": "inputWorkflowNode", "data": {}}


def _output(node_id):
    return {"id": node_id, "type": "outputWorkflowNode", "data": {}}


def _node(node_id, params=None):
    return {"id": node_id, "type": "workflowNode",
            "data": {"label": node_id, "repo": "r", "outputs": {},
                     "paramValues": params or {}}}


def _edge(source, target, handle="in", source_handle="out"):
    return {"source": source, "sourceHandle": source_handle,
            "target": target, "targetHandle": handle}


WORKFLOWS = {
    "multi_input": {
        "nodes": [_input("in-1"), _input("in-2"), _node("pair-1"),
                  _output("out-1")],
        "edges": [_edge("in-1", "pair-1", "a"), _edge("in-2", "pair-1", "b"),
                  _edge("pair-1", "out-1")],
    },
    "fan_out": {
        "nodes": [_input("in-1"), _node("split-1"), _node("piped-1"),
                  _node("fixed-1"), _output("out-1"), _output("out-2")],
        "edges": [_edge("in-1", "split-1"), _edge("split-1", "piped-1"),
                  _edge("split-1", "fixed-1"), _edge("piped-1", "out-1"),
                  _edge("fixed-1", "out-2")],
    },
    "stdin_stdout": {
        "nodes": [_input("in-1"), _node("piped-1"), _output("out-1")],
        "edges": [_edge("in-1", "piped-1"), _edge("piped-1", "out-1")],
    },
    "hidden_parameter": {
        "nodes": [_input("in-1"),
                  _node("sub-1", {"level": {"enabled": True, "value": 3}}),
                  _output("out-1")],
        "edges": [_edge("in-1", "sub-1"), _edge("sub-1", "out-1")],
    },
    "fixed_filename": {
        "nodes": [_input("in-1"), _node("fixed-1"), _output("out-1")],
        "edges": [_edge("in-1", "fixed-1"), _edge("fixed-1", "out-1")],
    },
    "fixed_filename_no_flag": {
        "nodes": [_input("in-1"), _node("unflagged-1"), _output("out-1")],
        "edges": [_edge("in-1", "unflagged-1"), _edge("unflagged-1", "out-1")],
    },
}


@pytest.mark.parametrize("name", sorted(WORKFLOWS))
def test_the_snakefile_is_exactly_the_golden_one(name):
    emitted = convert.convert_to_snakemake(
        convert.parse_biochef_workflow(WORKFLOWS[name]))

    assert emitted == (GOLDEN / f"{name}.smk").read_text()
