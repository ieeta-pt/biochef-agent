import sys
import types
from pathlib import Path

# convert.py builds an ORAS client and logs in at import time, so importing it
# in a test would try to reach the registry. Neither the client nor FastAPI is
# used by the conversion itself.
if "oras" not in sys.modules:
    oras = types.ModuleType("oras")
    client = types.ModuleType("oras.client")

    class _Client:
        def __init__(self, *a, **k):
            pass

        def login(self, *a, **k):
            pass

        def pull(self, *a, **k):
            raise AssertionError("a test reached the registry; stub fetch_tool instead")

    client.OrasClient = _Client
    oras.client = client
    sys.modules["oras"] = oras
    sys.modules["oras.client"] = client

# FastAPI is a pinned dependency, so the real one is normally present and is
# what should be used -- convert.py only needs FastAPI() to construct. The stub
# is a fallback for an environment without it.
#
# The condition has to be "can it be imported", not "is it in sys.modules".
# Guarding on sys.modules installs the stub whenever this conftest is imported
# before anything has touched fastapi -- which is always, because conftest runs
# first -- and the stub is a plain module rather than a package, so any test
# doing `from fastapi.testclient import TestClient` then fails with
# "No module named 'fastapi.testclient'; 'fastapi' is not a package".
#
# That is not hypothetical: tests added later for the upload handler need a
# TestClient, and with the sys.modules guard the whole suite stopped collecting
# -- including the tests that had nothing to do with FastAPI.
try:
    import fastapi  # noqa: F401
except ImportError:
    fastapi = types.ModuleType("fastapi")

    class _App:
        def __init__(self, *a, **k):
            pass

        def post(self, *a, **k):
            return lambda fn: fn

    fastapi.FastAPI = _App
    sys.modules["fastapi"] = fastapi

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
