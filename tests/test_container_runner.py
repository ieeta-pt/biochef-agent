"""Whether a step can be made to run in a container today (#15).

Recorded before anything changes. It cannot.

The seam from the previous PR made the *strategy* replaceable, which is half of
what E2 needs. The other half is that a step has to run somewhere isolated, and
nothing in what the service writes or launches says so: the Snakefile carries no
container directive, and the command carries no deployment method, so every
rule's shell block runs directly on the host as the agent's own user.
"""

import os
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

from runner import PROVIDERS, SubprocessRunner


class _Workspace:
    def __init__(self, path):
        self.path = str(path)


def test_there_is_only_one_provider():
    """A seam with one implementation is not yet a choice."""
    assert sorted(PROVIDERS) == ["subprocess"]


def test_nothing_asks_snakemake_to_deploy_software_anywhere(tmp_path):
    """No --software-deployment-method, so no rule is containerised.

    Verified against snakemake itself rather than assumed: with a global
    `container:` directive present, snakemake 9.21 refuses to start without
    apptainer, and without the directive the same flags run to completion. So
    the absence of both is what makes every step run on the host.
    """
    argv = SubprocessRunner().command(_Workspace(tmp_path))
    assert "--software-deployment-method" not in argv
    assert "--use-apptainer" not in argv
    assert not any("container" in a for a in argv)


def test_a_provider_cannot_contribute_anything_to_the_snakefile():
    """The Snakefile is entirely the emitter's, so a runner cannot ask for a
    container without the emitter growing a container directive -- and the
    emitter lives on a different branch of this stack."""
    assert not hasattr(SubprocessRunner(), "snakefile_preamble")


def test_the_snakefile_the_service_writes_has_no_container_directive():
    """Read from the emitter's own output, not from a fixture."""
    import convert

    workflow = convert.Workflow(nodes=[])
    text = convert.convert_to_snakemake(workflow)
    assert "container:" not in text
