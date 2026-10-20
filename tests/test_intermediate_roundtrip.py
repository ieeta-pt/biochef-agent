"""Issue #2's acceptance criterion.

"Given a workflow from the editor, the Agent writes an intermediate.json that
validates against the schema, and regenerating the Snakefile from that file
alone yields the same result."

So every case here converts twice -- once from the document held in memory,
once from a document that has been written to disk, read back and validated --
and asserts the two Snakefiles are byte-identical. That equivalence is what
makes the feature flag safe to turn on.
"""

import json

import pytest

import convert
from intermediate import SCHEMA_VERSION, Workflow
from tests.fixtures.tools import BUNDLES


@pytest.fixture(autouse=True)
def stub_registry(monkeypatch):
    monkeypatch.setattr(
        convert, "fetch_tool", lambda tool_id, repo: BUNDLES[tool_id.split("-")[0]]
    )


def workflow(tool_id, in_handle, out_handle, params=None):
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


CASES = [
    ("positional input, flagged output", workflow("tn93.distance", "in", "out")),
    ("stdout output", workflow("edlib.align", "queries", "out")),
    ("enabled parameter", workflow("edlib.align", "queries", "out",
                                   {"mode": {"enabled": True, "value": "NW"}})),
    ("disabled parameter", workflow("edlib.align", "queries", "out",
                                    {"mode": {"enabled": False, "value": "NW"}})),
]


@pytest.mark.parametrize("label,editor_export", CASES, ids=[c[0] for c in CASES])
def test_the_snakefile_is_the_same_through_disk(label, editor_export, tmp_path):
    document = convert.parse_biochef_workflow(editor_export)
    direct = convert.convert_to_snakemake(document)

    path = convert.write_intermediate(document, str(tmp_path / "intermediate.json"))
    from_disk = convert.convert_to_snakemake(convert.read_intermediate(path))

    assert from_disk == direct, f"{label}: regenerating from disk changed the Snakefile"


@pytest.mark.parametrize("label,editor_export", CASES, ids=[c[0] for c in CASES])
def test_the_document_on_disk_validates(label, editor_export, tmp_path):
    document = convert.parse_biochef_workflow(editor_export)
    path = convert.write_intermediate(document, str(tmp_path / "intermediate.json"))

    raw = json.loads(open(path).read())
    assert raw["schemaVersion"] == SCHEMA_VERSION
    assert Workflow.model_validate(raw) == document


def test_argument_order_survives_the_round_trip():
    """The regression that a single-argument test cannot see.

    `inputs`, `outputs` and `parameters` are dicts whose insertion order the
    emitter walks to build the command. Serialising with sorted keys -- which
    is the obvious thing to do for a file meant to be diffable -- reordered the
    command line for 85 of the 176 catalogue operations while every test above
    still passed, because each of those workflows has a single argument of each
    kind and a single element cannot be out of order.
    """
    export = {
        "nodes": [
            {"id": "a-1", "type": "inputWorkflowNode",
             "data": {"outputs": {"out": {"kind": "text", "data": "x"}}}},
            {"id": "z-1", "type": "inputWorkflowNode",
             "data": {"outputs": {"out": {"kind": "text", "data": "y"}}}},
            {"id": "multi.args-1", "type": "workflowNode",
             "data": {"label": "t", "repo": "r", "outputs": {},
                      "paramValues": {
                          "zeta": {"enabled": True, "value": "Z"},
                          "mu": {"enabled": True, "value": "M"},
                          "alpha_p": {"enabled": True, "value": "A"},
                      }}},
            {"id": "output-1", "type": "outputWorkflowNode", "data": {}},
        ],
        "edges": [
            {"source": "a-1", "sourceHandle": "out",
             "target": "multi.args-1", "targetHandle": "alpha"},
            {"source": "z-1", "sourceHandle": "out",
             "target": "multi.args-1", "targetHandle": "zulu"},
            {"source": "multi.args-1", "sourceHandle": "out",
             "target": "output-1", "targetHandle": "in"},
        ],
    }

    document = convert.parse_biochef_workflow(export)
    direct = convert.convert_to_snakemake(document)
    from_disk = convert.convert_to_snakemake(Workflow.from_json(document.to_json()))

    assert from_disk == direct

    # And state the order positively, so the test says what it is protecting
    # rather than only that two things match.
    shell = [l.strip() for l in direct.splitlines() if l.strip().startswith("./")][0]
    assert shell.index("-z Z") < shell.index("-u M") < shell.index("-p A")


def test_the_flag_is_off_by_default():
    """New functionality lands behind a feature flag, per the roadmap conventions."""
    assert convert.WRITE_INTERMEDIATE is False


def test_with_the_flag_off_nothing_is_written(tmp_path, monkeypatch):
    monkeypatch.setattr(convert, "WRITE_INTERMEDIATE", False)
    monkeypatch.chdir(tmp_path)

    document = convert.parse_biochef_workflow(CASES[0][1])
    assert convert.through_intermediate(document) is document
    assert list(tmp_path.iterdir()) == []


def test_with_the_flag_on_the_document_is_written_and_reread(tmp_path, monkeypatch):
    monkeypatch.setattr(convert, "WRITE_INTERMEDIATE", True)
    monkeypatch.chdir(tmp_path)

    document = convert.parse_biochef_workflow(CASES[0][1])
    result = convert.through_intermediate(document)

    written = tmp_path / convert.INTERMEDIATE_FILENAME
    assert written.exists(), "the flag was on but no document was written"
    assert result is not document, "the document should have come back off disk"
    assert result == document
    assert convert.convert_to_snakemake(result) == convert.convert_to_snakemake(document)


def test_a_corrupt_document_is_refused_rather_than_half_read(tmp_path):
    """The reason for validating on the way in.

    Without it a truncated or hand-edited file would produce a Snakefile that
    looks plausible and builds the wrong thing.
    """
    import pydantic

    path = tmp_path / "intermediate.json"
    path.write_text('{"schemaVersion": "1.0.0", "nodes": [{"id": "x", "bin": "y", '
                    '"inputs": {"a": {"mode": "not-a-mode"}}}]}')

    with pytest.raises(pydantic.ValidationError):
        convert.read_intermediate(str(path))
