"""Where input order and identity come from today (#31, #32).

Recorded before anything changes.

Inputs are keyed by the file that feeds them -- f"{source}-{source_handle}" --
and walked in edge order. Two consequences follow from that one decision, and
they are the two issues.
"""

import json

import pytest

import convert

BUNDLE = {
    "id": "t", "name": "t", "bin": "t",
    "io": {
        # Declared order: first, then second. A recipe means this order.
        "inputs": [
            {"name": "first", "types": ["T"], "mode": "file"},
            {"name": "second", "types": ["T"], "mode": "file"},
        ],
        "outputs": [{"name": "out", "types": ["T"], "mode": "stdout"}],
    },
    "parameters": [],
}


@pytest.fixture(autouse=True)
def stub_registry(monkeypatch):
    monkeypatch.setattr(convert, "fetch_tool", lambda tool_id, repo: BUNDLE)
    convert.tools.clear()


def emit(edges, sources=("a", "b")):
    nodes = [{"id": s, "type": "inputWorkflowNode",
              "data": {"outputs": {"out": {"kind": "text", "data": "x"}}}}
             for s in sources]
    nodes += [
        {"id": "t-1", "type": "workflowNode",
         "data": {"label": "t", "repo": "r", "paramValues": {}, "outputs": {}}},
        {"id": "output-1", "type": "outputWorkflowNode", "data": {}},
    ]
    edges = list(edges) + [{"source": "t-1", "sourceHandle": "out",
                            "target": "output-1", "targetHandle": "in"}]
    document = convert.parse_biochef_workflow({"nodes": nodes, "edges": edges})
    text = convert.convert_to_snakemake(document)
    lines = [l.strip() for l in text.splitlines()]
    shell = json.loads(lines[lines.index("shell:") + 1])
    # The shell line only ever holds placeholders -- {input.i_0:q} -- so which
    # FILE each one is bound to is the thing that differs, and that is in the
    # input: section. Asserting on the shell line alone would compare two
    # identical strings and call a real difference no difference.
    # Anchored after the rule, because "rule all:" has an input: section of its
    # own and it comes first.
    rule_at = next(i for i, l in enumerate(lines) if l.startswith("rule t_1:"))
    start = lines.index("input:", rule_at) + 1
    bindings = lines[start:lines.index("output:", start)]
    return document.nodes[0], shell, bindings


def edge(source, target_handle):
    return {"source": source, "sourceHandle": "out",
            "target": "t-1", "targetHandle": target_handle}


def test_input_order_follows_the_edges_not_the_declaration():
    """#31. The same graph, with the edges listed the other way round, is a
    different command."""
    _, _, declared = emit([edge("a", "first"), edge("b", "second")])
    _, _, reversed_ = emit([edge("b", "second"), edge("a", "first")])

    assert declared != reversed_, "order no longer follows edge order"
    assert declared == ['i_0="a-out",', 'i_1="b-out",']
    assert reversed_ == ['i_0="b-out",', 'i_1="a-out",']


def test_one_source_feeding_two_inputs_loses_one():
    """#32. Both connections key on the same file name, so the second
    overwrites the first and the tool is invoked with one argument short."""
    node, shell, _ = emit([edge("a", "first"), edge("a", "second")], sources=("a",))

    assert len(node.inputs) == 1, "the collision no longer happens"
    assert list(node.inputs) == ["a-out"]
    assert shell.count("{input.i_") == 1, "only one input reached the command"


def test_inputs_are_keyed_by_the_file_not_by_the_declared_input():
    """Which is the decision both issues follow from."""
    node, _, _ = emit([edge("a", "first"), edge("b", "second")])

    assert sorted(node.inputs) == ["a-out", "b-out"]
    assert "first" not in node.inputs and "second" not in node.inputs
