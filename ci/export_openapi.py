"""Write the OpenAPI document the service actually serves (#5).

B1 asks for the spec in the repository as the single source of truth for the
API. Generated rather than hand-written, because a hand-written one drifts and
then lies -- and a spec nobody can trust is worse than none, since a client is
generated from it.

Run it to regenerate:

    python ci/export_openapi.py

A test asserts the committed file matches what the app serves, so a route added
without regenerating fails CI rather than shipping a stale document.
"""

import json
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

if "oras" not in sys.modules:                       # no registry, no network
    oras = types.ModuleType("oras")
    client_mod = types.ModuleType("oras.client")

    class _Client:
        def __init__(self, *a, **k):
            pass

        def login(self, *a, **k):
            pass

    client_mod.OrasClient = _Client
    oras.client = client_mod
    sys.modules["oras"] = oras
    sys.modules["oras.client"] = client_mod

import main

DESTINATION = os.path.join(os.path.dirname(HERE), "openapi.json")


def document():
    """Stable across runs: sorted keys, and a trailing newline."""
    return json.dumps(main.app.openapi(), indent=2, sort_keys=True) + "\n"


if __name__ == "__main__":
    with open(DESTINATION, "w") as handle:
        handle.write(document())
    print(f"wrote {DESTINATION}")
