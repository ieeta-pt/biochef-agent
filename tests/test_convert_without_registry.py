"""The page reads a Snakefile through convert.py, and has no registry to reach."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

import convert

REPO_ROOT = Path(__file__).resolve().parent.parent


class Recording:
    made = 0

    def __init__(self, *args, **kwargs):
        Recording.made += 1
        self.logged_in = 0

    def login(self, **kwargs):
        self.logged_in += 1

    def pull(self, **kwargs):
        return "pulled"


@pytest.fixture
def recording(monkeypatch):
    Recording.made = 0
    monkeypatch.setattr(convert.oras.client, "OrasClient", Recording)
    return convert._Registry()


def test_making_the_client_does_not_touch_the_registry(recording):
    assert Recording.made == 0


def test_the_first_use_logs_in_once(recording):
    assert recording.pull() == "pulled"
    recording.pull()
    assert Recording.made == 1
    assert recording.login is not None
    assert recording._client.logged_in == 1


def test_importing_the_module_reaches_for_nothing():
    """In a process of its own, with a client that fails if anything touches it."""
    code = """
import sys, types
class Refusing:
    def __init__(self, *a, **k):
        raise SystemExit("the client was built at import")
oras = types.ModuleType("oras")
client = types.ModuleType("oras.client")
client.OrasClient = Refusing
oras.client = client
sys.modules["oras"] = oras
sys.modules["oras.client"] = client
import convert
"""
    result = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def bundle():
    return {"bin": "tool", "io": {"inputs": [], "outputs": []},
            "parameters": [{"name": "n", "flag": "-n", "type": "text"}]}


def document():
    return {"nodes": [{"id": "tool-1", "type": "workflowNode",
                       "data": {"repo": "r@sha256:a", "paramValues": {
                           "n": {"enabled": True, "value": "1"}}}}], "edges": []}


def test_parsing_can_take_tools_from_anywhere(monkeypatch):
    monkeypatch.setattr(convert, "fetch_tool",
                        lambda *a: pytest.fail("reached the registry path"))
    workflow = convert.parse_biochef_workflow(document(), resolve=lambda *a: bundle())
    assert workflow.nodes[0].bin == "tool"


def test_parsing_still_fetches_by_default(monkeypatch):
    seen = []
    monkeypatch.setattr(convert, "fetch_tool",
                        lambda tool, repo: seen.append((tool, repo)) or bundle())
    convert.parse_biochef_workflow(document())
    assert seen == [("tool-1", "r@sha256:a")]


def test_a_cached_tool_is_read_without_the_registry(monkeypatch, tmp_path):
    (tmp_path / "tool").mkdir()
    (tmp_path / "tool" / "bundle.json").write_text(json.dumps(bundle()))
    monkeypatch.setattr(convert, "TOOL_CACHE", str(tmp_path))
    assert convert.cached_tool("tool-1", "r")["bin"] == "tool"


def test_a_tool_not_in_the_cache_says_which(monkeypatch, tmp_path):
    monkeypatch.setattr(convert, "TOOL_CACHE", str(tmp_path))
    with pytest.raises(convert.ToolNotCached, match="tool"):
        convert.cached_tool("tool-1", "r")


def test_a_hostile_node_id_cannot_leave_the_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(convert, "TOOL_CACHE", str(tmp_path))
    with pytest.raises(Exception) as refused:
        convert.cached_tool("../../etc/passwd", "r")
    assert not isinstance(refused.value, FileNotFoundError)
