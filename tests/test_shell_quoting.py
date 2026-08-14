"""What reaches the shell today.

Recorded before any change. Each test here asserts the CURRENT behaviour, so
they pass against the unfixed converter and are the evidence that #35, #42 and
#37 are real rather than theoretical.

The important one is test_a_payload_actually_executes: it does not inspect the
generated string and judge it dangerous, it runs the workflow through real
snakemake and checks that a file the payload was told to create exists
afterwards. Every other kind of evidence for a shell injection is an opinion.
"""

import json
import shutil
import subprocess

import pytest

import convert
from tests.fixtures.tools import BUNDLES


@pytest.fixture(autouse=True)
def stub_registry(monkeypatch):
    monkeypatch.setattr(
        convert, "fetch_tool", lambda tool_id, repo: BUNDLES[tool_id.split("-")[0]]
    )


PAYLOADS = [
    "; touch PWNED_SEMI",
    "$(touch PWNED_SUBST)",
    "`touch PWNED_BQ`",
    "&& touch PWNED_AND",
]


def workflow_with(value, tool="edlib.align", in_handle="queries", out_handle="out"):
    return {
        "nodes": [
            {"id": "input-1", "type": "inputWorkflowNode",
             "data": {"outputs": {"out": {"kind": "text", "data": "x"}}}},
            {"id": f"{tool}-1", "type": "workflowNode",
             "data": {"label": "t", "repo": "r", "outputs": {},
                      "paramValues": {"mode": {"enabled": True, "value": value}}}},
            {"id": "output-1", "type": "outputWorkflowNode", "data": {}},
        ],
        "edges": [
            {"source": "input-1", "sourceHandle": "out",
             "target": f"{tool}-1", "targetHandle": in_handle},
            {"source": f"{tool}-1", "sourceHandle": out_handle,
             "target": "output-1", "targetHandle": "in"},
        ],
    }


def shell_line(text):
    return [l.strip() for l in text.splitlines() if l.strip().startswith("./")][0]


@pytest.mark.parametrize("payload", PAYLOADS)
def test_a_parameter_value_reaches_the_shell_unquoted(payload):
    """#35. The value is concatenated in and never quoted."""
    line = shell_line(convert.convert_to_snakemake(
        convert.parse_biochef_workflow(workflow_with(payload))))
    assert f"-m {payload} " in line, "the value is not being interpolated raw any more"


def test_a_numeric_value_crashes_the_emitter():
    """#37. 37 of the 176 catalogue operations declare a numeric default."""
    with pytest.raises(TypeError, match="expected str instance"):
        convert.convert_to_snakemake(
            convert.parse_biochef_workflow(workflow_with(2)))


def test_registry_supplied_strings_reach_the_shell_unquoted(monkeypatch):
    """#42. These come from bundle.json, and the client picks the repo."""
    hostile = {
        "id": "h", "name": "h", "bin": "h",
        "io": {"inputs": [{"name": "in", "types": ["T"], "mode": "file",
                           "flag": "-i; touch PWNED #"}],
               "outputs": [{"name": "out", "types": ["T"], "mode": "file",
                            "filename": "fixed.txt; touch PWNED"}]},
        "parameters": [{"name": "mode", "type": "string", "flag": "-p $(id)"}],
    }
    monkeypatch.setattr(convert, "fetch_tool", lambda tool_id, repo: hostile)
    convert.tools.clear()

    sm = convert.convert_to_snakemake(
        convert.parse_biochef_workflow(workflow_with("v", tool="h", in_handle="in")))

    assert "-p $(id)" in sm
    assert "-i; touch PWNED #" in sm
    assert "cp fixed.txt; touch PWNED" in sm


def test_a_node_id_can_write_into_the_snakefile_structure(monkeypatch):
    """A node id is client-supplied and lands where a rule is declared."""
    hostile_id = 'x:\n  shell: "touch PWNED"\nrule y'
    monkeypatch.setattr(
        convert, "fetch_tool", lambda tool_id, repo: BUNDLES["edlib.align"])
    convert.tools.clear()
    sm = convert.convert_to_snakemake(
        convert.parse_biochef_workflow({
            "nodes": [{"id": hostile_id, "type": "workflowNode",
                       "data": {"label": "t", "repo": "r",
                                "paramValues": {}, "outputs": {}}}],
            "edges": []}))
    assert "touch PWNED" in sm, "a node id no longer reaches the Snakefile verbatim"


SNAKEMAKE = shutil.which("snakemake")

STUB = """#!/usr/bin/env python3
import json, os, sys
here = os.path.dirname(os.path.abspath(__file__))
json.dump(sys.argv[1:], open(os.path.join(here, "argv.json"), "w"))
sys.stdout.write("ok")
"""


@pytest.mark.skipif(not SNAKEMAKE, reason="snakemake is not installed")
def test_a_payload_actually_executes(tmp_path):
    """The evidence that this is a live defect and not a style complaint.

    Runs the generated workflow through real snakemake and asserts the file the
    payload asked for exists afterwards. Nothing about this is inferred.
    """
    sm = convert.convert_to_snakemake(
        convert.parse_biochef_workflow(workflow_with("; touch PWNED_SEMI")))
    (tmp_path / "Snakefile").write_text(sm)
    stub = tmp_path / "edlib-aligner"
    stub.write_text(STUB)
    stub.chmod(0o755)
    (tmp_path / "input-1-out").write_text("input-content")

    subprocess.run(
        [SNAKEMAKE, "--cores", "1", "-s", str(tmp_path / "Snakefile"),
         "-d", str(tmp_path), "all"],
        capture_output=True, text=True)

    assert (tmp_path / "PWNED_SEMI").exists(), \
        "the payload no longer executes -- this test has served its purpose"
