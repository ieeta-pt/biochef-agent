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

if "fastapi" not in sys.modules:
    fastapi = types.ModuleType("fastapi")

    class _App:
        def __init__(self, *a, **k):
            pass

        def post(self, *a, **k):
            return lambda fn: fn

    fastapi.FastAPI = _App
    sys.modules["fastapi"] = fastapi

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
