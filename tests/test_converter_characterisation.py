"""What the converter produces today.

Written before any change, so that the refactor in this PR can be shown to
preserve behaviour rather than merely claimed to. Every expectation here was
recorded from the current implementation, including the ones that look wrong.

Two expectations gained `:q` when the shell quoting went in (#35). That is the
one deliberate change to emitted text since these were recorded: the field
reference is now quoted by Snakemake at expansion, so a path containing a space
stays one argument. The argv the tool receives is unchanged for every name in
the catalogue -- see tests/test_shell_quoting.py, which checks that against a
real snakemake run rather than against the emitted string.
"""

import pytest

import convert
from tests.fixtures.tools import BUNDLES


@pytest.fixture(autouse=True)
def stub_registry(monkeypatch):
    # fetch_tool does a registry pull and a file copy. The conversion under test
    # needs only the bundle, so it is supplied directly.
    monkeypatch.setattr(
        convert, "fetch_tool", lambda tool_id, repo: BUNDLES[tool_id.split("-")[0]]
    )


def workflow(tool_id, in_handle, out_handle, params=None):
    """An editor export: one input node, one tool, one output node."""
    return {
        "nodes": [
            {"id": "input-1", "type": "inputWorkflowNode",
             "data": {"outputs": {"out": {"kind": "text", "data": "x"}}}},
            {"id": f"{tool_id}-1", "type": "workflowNode",
             "data": {"label": "t", "repo": "r", "paramValues": params or {}, "outputs": {}}},
            {"id": "output-1", "type": "outputWorkflowNode", "data": {}},
        ],
        "edges": [
            {"source": "input-1", "sourceHandle": "out",
             "target": f"{tool_id}-1", "targetHandle": in_handle},
            {"source": f"{tool_id}-1", "sourceHandle": out_handle,
             "target": "output-1", "targetHandle": "in"},
        ],
    }


def shell_line(text):
    return [l.strip() for l in text.splitlines() if l.strip().startswith("./")][0]


def test_positional_input_with_flagged_output():
    sm = convert.convert_to_snakemake(
        convert.parse_biochef_workflow(workflow("tn93.distance", "in", "out"))
    )
    assert shell_line(sm) == "./tn93 -o {output.o_0:q} {input.i_0:q}"
    assert 'o_0="tn93.distance-1-out",' in sm
    assert 'rule all:' in sm


def test_stdout_output_is_redirected_last():
    sm = convert.convert_to_snakemake(
        convert.parse_biochef_workflow(workflow("edlib.align", "queries", "out"))
    )
    assert shell_line(sm) == "./edlib-aligner {input.i_0:q} > {output.o_0:q}"


def test_enabled_parameter_reaches_the_command():
    sm = convert.convert_to_snakemake(
        convert.parse_biochef_workflow(
            workflow("edlib.align", "queries", "out",
                     params={"mode": {"enabled": True, "value": "NW"}})
        )
    )
    assert "-m NW" in shell_line(sm)


def test_disabled_parameter_is_omitted():
    sm = convert.convert_to_snakemake(
        convert.parse_biochef_workflow(
            workflow("edlib.align", "queries", "out",
                     params={"mode": {"enabled": False, "value": "NW"}})
        )
    )
    assert "-m" not in shell_line(sm)


def test_a_terminal_tool_with_no_output_edge_produces_nothing_to_build():
    """Recorded, not endorsed.

    A tool whose output is not connected onward gets no outputs at all, so the
    rule declares none and `rule all` has no inputs -- Snakemake would build
    nothing. This is why a workflow has to end in an output node.
    """
    wf = workflow("tn93.distance", "in", "out")
    wf["nodes"] = [n for n in wf["nodes"] if n["id"] != "output-1"]
    wf["edges"] = [e for e in wf["edges"] if e["target"] != "output-1"]

    sm = convert.convert_to_snakemake(convert.parse_biochef_workflow(wf))
    assert "rule all:\n    input:\nrule" in sm.replace("\r\n", "\n")
    assert "    output:\n    shell:" in sm
