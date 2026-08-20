"""Whether a required parameter depends on the client (#33).

Recorded before anything changes. The converter decides purely on the client's
`enabled` flag and never consults the recipe's own `required`, so a subcommand
survives only because the editor happens to initialise required parameters as
enabled. Any client that does not replicate that -- a hand-written workflow, an
older export, a second frontend, a WES submission -- loses it.

65 of the catalogue's 73 hidden parameters are subcommand selectors, so this is
the whole multi-command half of it: bcftools, bedtools, samtools, seqtk, ivar.
"""

import json

import pytest

import convert

BUNDLE = {
    "id": "samtools", "name": "samtools", "bin": "samtools",
    "io": {"inputs": [{"name": "in", "types": ["SAM"], "mode": "file"}],
           "outputs": [{"name": "out", "types": ["BAM"], "mode": "stdout"}]},
    "parameters": [
        # The real shape: a hidden, required subcommand with a default.
        {"name": "command", "type": "string", "required": True,
         "hidden": True, "default": "fixmate"},
        {"name": "mate_score", "type": "flag", "flag": "-m", "required": True},
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
            {"id": "samtools-1", "type": "workflowNode",
             "data": {"label": "samtools", "repo": "r", "outputs": {},
                      "paramValues": param_values}},
            {"id": "output-1", "type": "outputWorkflowNode", "data": {}},
        ],
        "edges": [
            {"source": "input-1", "sourceHandle": "out",
             "target": "samtools-1", "targetHandle": "in"},
            {"source": "samtools-1", "sourceHandle": "out",
             "target": "output-1", "targetHandle": "in"},
        ],
    }
    text = convert.convert_to_snakemake(convert.parse_biochef_workflow(export))
    lines = [l.strip() for l in text.splitlines()]
    return json.loads(lines[lines.index("shell:") + 1])


WHAT_THE_EDITOR_SENDS = {"command": {"enabled": True, "value": "fixmate"},
                         "mate_score": {"enabled": True, "value": ""}}


def test_it_works_when_the_client_volunteers_everything():
    """Which is why this has not been noticed."""
    assert emit(WHAT_THE_EDITOR_SENDS).startswith("./samtools fixmate -m ")


def test_a_client_that_sends_nothing_loses_the_subcommand():
    """#33. Not samtools doing the wrong thing -- samtools with no subcommand."""
    command = emit({})

    assert "fixmate" not in command, "the recipe's default is no longer needed"
    assert command.startswith("./samtools {input.i_0:q}")


def test_a_client_that_disables_a_required_parameter_also_loses_it():
    command = emit({"command": {"enabled": False, "value": "fixmate"}})

    assert "fixmate" not in command
