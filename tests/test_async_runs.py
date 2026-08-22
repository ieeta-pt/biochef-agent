"""What happens to the connection during a long run (#5).

Recorded before anything changes. It is held open for the whole thing.

B1 asks two questions to verify first: is the current REST API synchronous, and
what happens to the HTTP connection during a long run. Yes, and it waits.

/convert does the whole job inside one request: it creates the workspace, pulls
the tools, writes the Snakefile, runs snakemake to completion, reads the outputs
back, base64-encodes them into the response, and only then replies. The default
timeout is 900 seconds, so a single request can legitimately hold a socket for
fifteen minutes and return nothing until the end.

What that costs:

  the client must wait          with no way to reconnect, and no way to ask how
                                far along it is
  a dropped connection loses    nothing survives the request, so a network blip
  everything                    means the work is gone
  a run has no identity         there is nothing to refer to afterwards, which
                                is why cancellation (#7), per-step logs (#6) and
                                progress (#8) all wait on this
  nothing can be polled         there is no second endpoint at all

And it is precisely wrong for what this service is for. Dispatching a step to an
HPC queue means waiting for a scheduler, not for a subprocess; there is no
version of that which fits inside one HTTP request.
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


def test_the_run_happens_inside_the_request():
    """The handler waits for snakemake and reads the outputs before replying."""
    source = inspect.getsource(main.convert)
    assert "await run_in_threadpool(run_snakemake, ws)" in source
    assert "return results" in source
    # The outputs are read and encoded in the same function, after the wait.
    assert source.index("run_in_threadpool") < source.index("b64encode")


def test_the_connection_can_be_held_for_the_whole_timeout():
    """Fifteen minutes by default, with nothing sent in the meantime."""
    assert main.RUN_TIMEOUT_S == 900


def test_there_is_no_second_endpoint_to_ask_anything():
    paths = {getattr(r, "path", None) for r in main.app.routes}
    assert "/convert" in paths
    assert not any(p and p.startswith("/runs") for p in paths), sorted(paths)


def test_a_run_has_no_identity_and_no_state():
    """So there is nothing to cancel, poll, or report progress against.

    #6, #7 and #8 all name this as their dependency, and this is why.
    """
    source = Path(REPO_ROOT / "main.py").read_text()
    for absent in ("run_id", "RunState", "QUEUED", "RUNNING", "COMPLETE"):
        assert absent not in source, absent


def test_no_openapi_document_is_shipped():
    """B1 wants the spec in the repository as the source of truth.

    FastAPI generates one at /openapi.json, but nothing is committed, so there
    is no artifact to review, diff, or generate a client from.
    """
    assert not (REPO_ROOT / "openapi.json").exists()
    assert not (REPO_ROOT / "openapi.yaml").exists()
    assert not list(REPO_ROOT.glob("docs/openapi*"))
