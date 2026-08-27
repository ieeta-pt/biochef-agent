"""What a large file costs to get in or out (#13).

Recorded before anything changes. Several copies of itself, in memory, on both
sides.

D2's acceptance is that a multi-gigabyte BAM crosses browser to agent and back
without being held fully in memory on either side. Today it cannot cross at all
on a machine of ordinary size, and the arithmetic is not subtle.

Outbound, an output is read whole, base64-encoded whole -- which is another copy,
4/3 the size -- and put in a dict that FastAPI then serialises whole. Measured on
an 8 MiB output: 8.0 MiB raw, 10.7 MiB encoded, both alive together, and the
serialised response after that. Extrapolated:

    1 GiB output  ->  ~3.7 GiB resident
    4 GiB output  ->  ~14.7 GiB resident

Inbound is simpler and no better: every part is read with `await f.read()`, so
the whole upload exists as one bytes object before the run starts.

Base64 is the deeper problem. It is in the response contract -- the editor
decodes it -- so an output cannot be streamed while it is also being encoded
into a JSON string field. Something has to give, and it should be the transport
rather than the correctness of the run.
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

import main


def test_an_output_is_read_and_encoded_whole():
    source = inspect.getsource(main.perform_run)
    assert "raw = file.read()" in source
    assert "base64.b64encode(raw)" in source, (
        "the whole file is encoded in one call, which is a second copy"
    )


def test_base64_costs_a_third_again_on_top_of_the_copy():
    """Not an impression -- the ratio is fixed and measurable."""
    import base64

    raw = b"x" * (1024 * 1024)
    encoded = base64.b64encode(raw)
    assert len(encoded) / len(raw) > 1.3


def test_every_output_is_held_at_once_in_one_dict():
    """So a workflow with several large outputs multiplies the problem."""
    source = inspect.getsource(main.perform_run)
    assert "results = {}" in source
    assert "return results" in source
    assert "StreamingResponse" not in source


def test_an_upload_is_read_whole_before_the_run_starts():
    for handler in (main.convert, main.submit_run):
        source = inspect.getsource(handler)
        assert "await f.read()" in source, handler.__name__


def test_there_is_no_way_to_fetch_one_output_on_its_own():
    """Everything comes back in the one response, or not at all."""
    paths = {getattr(r, "path", None) for r in main.app.routes}
    assert not any(p and "outputs" in p for p in paths), sorted(
        p for p in paths if p)
