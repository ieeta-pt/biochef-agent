"""How an input reaches a run (#12).

Recorded before anything changes. One way only, and the code says so in its
names.

Every input arrives as bytes in a multipart request. The handler reads each part
whole, hands perform_run a list of (filename, bytes), and the run writes them
into its workspace. The function that decides what a run needs is called
expected_UPLOADS, and the parameter is called `uploads`: the single supported
source is spelled into the vocabulary, so a second one cannot be added without
either renaming things or lying about what they mean.

That matters beyond tidiness. D1 exists because F1 (htsget) and F2 (DRS) fetch
their inputs from elsewhere -- a slice streamed from a server, an object resolved
by identifier -- and neither of those is a thing a browser pushes. A file already
sitting on the agent's disk, which is the ordinary case inside a TRE, cannot be
used at all today without uploading it to the machine it is already on.

It also puts a ceiling on size that has nothing to do with the tool: every input
is held in memory in its entirety, twice over -- once as the request body, once
as the list handed to the run.
"""

import inspect
import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

if "oras" not in sys.modules:
    oras = types.ModuleType("oras")
    client_mod = types.ModuleType("oras.client")

    class _Client:
        def __init__(self, *a, **k):
            pass

        def login(self, *a, **k):
            pass

        def pull(self, *a, **k):
            raise AssertionError("a test reached the registry")

    client_mod.OrasClient = _Client
    oras.client = client_mod
    sys.modules["oras"] = oras
    sys.modules["oras.client"] = client_mod

import convert
import main


def test_there_is_no_datasource_interface():
    assert not (REPO_ROOT / "datasource.py").exists()


def test_the_only_source_is_spelled_into_the_names():
    """expected_uploads, and a parameter called uploads.

    A second provider cannot be added without renaming these or leaving them
    meaning something they no longer mean.
    """
    assert hasattr(convert, "expected_uploads")
    assert "uploads" in inspect.signature(main.perform_run).parameters


def test_an_input_must_be_pushed_as_bytes():
    """No path takes a file already on the agent's host.

    Which is the ordinary case inside a TRE: the data is on the machine, and the
    only way to use it is to upload it to where it already is.
    """
    source = inspect.getsource(main.perform_run)
    assert "for filename, content in uploads" in source
    assert "ws.write_bytes(name, content)" in source
    for absent in ("localpath", "DataSource", "fetch", "open(", "shutil.copy"):
        assert absent not in source, absent


def test_every_input_is_held_in_memory_whole():
    """Twice: as the request body, and as the list handed to the run."""
    submit = inspect.getsource(main.submit_run)
    convert_handler = inspect.getsource(main.convert)
    for source in (submit, convert_handler):
        assert "await f.read()" in source, (
            "each part is read in full rather than streamed to disk"
        )


def test_the_converter_decides_what_is_needed_in_upload_terms():
    source = inspect.getsource(convert.expected_uploads)
    assert "consumed - produced" in source
    # It answers "which names must arrive", with no notion of where from.
    assert "source" not in source.lower().replace("sourceHandle", "")
