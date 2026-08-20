"""What bounds the size of a request today (#11).

Recorded before anything changes. Nothing does.

The last deliverable of C3, and the one that cannot be done in the handler:
starlette parses and spools the whole multipart body before the handler is
entered, so by the time any of our code runs the bytes are already on disk. No
reordering inside /convert fixes that, which is why this belongs in middleware.
"""

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

import pytest
from fastapi.testclient import TestClient
from starlette.formparsers import MultiPartParser

import main


def test_starlette_has_no_total_body_limit():
    """max_part_size bounds ONE part, not the request.

    So a body of many parts, or one streamed without a Content-Length, has no
    ceiling from the framework.
    """
    assert MultiPartParser.spool_max_size == 1024 * 1024
    assert getattr(MultiPartParser, "max_file_size", None) is None


def test_the_app_declares_no_size_limit():
    """Nothing in the application layer either."""
    assert not hasattr(main, "MAX_UPLOAD_BYTES")
    names = [type(m.cls).__name__ if hasattr(m, "cls") else str(m)
             for m in getattr(main.app, "user_middleware", [])]
    assert not any("size" in n.lower() or "limit" in n.lower() for n in names), names


def test_a_large_body_is_accepted_as_far_as_the_handler():
    """The body is spooled and the request reaches application code.

    Kept small enough to be quick but over starlette's 1 MiB spool threshold, so
    it exercises the path where the part goes to disk rather than staying in
    memory.
    """
    client = TestClient(main.app, raise_server_exceptions=False)
    payload = b"x" * (2 * 1024 * 1024)

    response = client.post(
        "/convert",
        data={"biochef_workflow": '{"nodes": [], "edges": []}'},
        files=[("files", ("input-1-out", payload, "application/octet-stream"))],
    )

    # Whatever the handler decides, nothing refused it for being too large.
    assert response.status_code != 413
