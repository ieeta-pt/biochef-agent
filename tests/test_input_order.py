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


def test_input_order_comes_from_the_recipe_whatever_the_edges_say():
    """#31. The same graph, edges listed either way round, is one command."""
    _, _, declared = emit([edge("a", "first"), edge("b", "second")])
    _, _, reversed_ = emit([edge("b", "second"), edge("a", "first")])

    assert declared == reversed_
    # and it is the DECLARED order: "first" is declared first, and "a" fills it
    assert declared == ['i_0="a-out",', 'i_1="b-out",']


def test_one_source_can_feed_two_inputs():
    """#32. Two edges from one source into two declared inputs used to key on
    the same file name, so the second overwrote the first and the tool was
    invoked an argument short."""
    node, shell, bindings = emit([edge("a", "first"), edge("a", "second")],
                                 sources=("a",))

    assert len(node.inputs) == 2, "one of the two inputs was dropped"
    assert list(node.inputs) == ["first", "second"]
    assert bindings == ['i_0="a-out",', 'i_1="a-out",']
    assert shell.count("{input.i_") == 2


def test_inputs_are_keyed_by_the_declared_input_they_fill():
    """The decision both fixes follow from.

    The file name is unchanged -- still f"{source}-{source_handle}", because
    that is what the upstream node writes. Only the key and the order changed.
    """
    node, _, _ = emit([edge("a", "first"), edge("b", "second")])

    assert list(node.inputs) == ["first", "second"]
    assert node.inputs["first"].file == "a-out"
    assert node.inputs["second"].file == "b-out"


def test_a_declared_input_nothing_is_wired_to_is_skipped():
    node, _, bindings = emit([edge("a", "second")], sources=("a",))

    assert list(node.inputs) == ["second"]
    assert bindings == ['i_0="a-out",']


def test_outputs_keep_their_file_derived_key():
    """Unchanged on purpose: main.py splits the handle back off it, and
    re-keying them would be a second, unrelated break of the same contract."""
    node, _, _ = emit([edge("a", "first")], sources=("a",))

    assert list(node.outputs) == ["t-1-out"]
