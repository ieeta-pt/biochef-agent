"""The intermediate document must stay with its run and retain tool evidence."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading

import pytest

import convert
from intermediate import Node, Workflow
from tests.test_provenance import _run


@pytest.mark.parametrize("enabled", [False, True])
def test_executor_keeps_intermediate_in_its_workspace(tmp_path, monkeypatch, enabled):
    monkeypatch.setattr(convert, "WRITE_INTERMEDIATE", enabled)
    cwd = tmp_path / "unrelated-cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    ws = None
    try:
        manifest, ws = _run(tmp_path, monkeypatch)
        assert manifest["execution"]["exit_code"] == 0
        document_path = Path(ws.path) / convert.INTERMEDIATE_FILENAME
        assert document_path.exists() is enabled
        assert not (cwd / convert.INTERMEDIATE_FILENAME).exists()
        if enabled:
            document = Workflow.from_json(document_path.read_text())
            assert document.nodes[0].provenance
            assert document.nodes[0].provenance == manifest["tools"]["tool-1"]
    finally:
        convert.tools.clear()
        if ws is not None:
            ws.cleanup()


@pytest.mark.parametrize("enabled", [False, True])
def test_concurrent_documents_use_the_supplied_paths(tmp_path, monkeypatch, enabled):
    monkeypatch.setattr(convert, "WRITE_INTERMEDIATE", enabled)
    monkeypatch.chdir(tmp_path)
    barrier = threading.Barrier(2)

    def roundtrip(label):
        directory = tmp_path / label
        directory.mkdir()
        path = directory / convert.INTERMEDIATE_FILENAME
        workflow = Workflow(nodes=[Node(
            id=label, bin="tool", provenance={"subject": "public-" + label},
        )])
        barrier.wait(timeout=10)
        restored = convert.through_intermediate(workflow, str(path))
        assert restored == workflow
        assert path.exists() is enabled
        if enabled:
            assert Workflow.from_json(path.read_text()) == workflow
        return restored

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(roundtrip, ["left", "right"]))

    assert [result.nodes[0].id for result in results] == ["left", "right"]
    assert not (tmp_path / convert.INTERMEDIATE_FILENAME).exists()
