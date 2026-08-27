"""What a finished run says about how it was produced (#18).

Recorded before anything changes. Nothing that outlives it.

E5 asks for a manifest recording the workflow, tool digests, input identities,
timestamps and exit codes, sufficient to re-execute the run and reach the same
outputs. Today a run leaves behind base64 outputs and, since B2, its logs. There
is no record of which tool binaries ran, which bundle versions they came from,
what went in, or when -- and the workspace that held the evidence is deleted.

The pieces exist and are discarded. The agent already:

  * pulls build-evidence.json and sbom.cdx.json alongside bundle.json, because
    pull() writes every layer the manifest lists
  * verifies all three against the registry's manifest digests (#9), so their
    identity is established and then thrown away
  * knows each tool's binary, each declared input, the exit code, and the
    per-step status

None of it is written down. This test file records that, and the shape the
manifest should take: the hub publishes `biochef.build-evidence.v1` with a
`schema` field, digests throughout, and in-toto statements over the artifacts.
A run manifest inventing its own vocabulary would be a second, incompatible
provenance format inside one project.
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


def test_there_is_no_manifest():
    assert not (REPO_ROOT / "provenance.py").exists()
    service = Path(REPO_ROOT / "main.py").read_text()
    assert "run.json" not in service


def test_a_finished_run_records_nothing_about_how_it_ran():
    """State, outputs and logs. Not what produced them."""
    from runs import RunStore

    run = RunStore().create()
    assert set(run.as_dict()) == {"run_id", "state"}
    assert set(run.logs_as_dict()) == {"run_id", "state", "stdout", "stderr",
                                       "failed_steps"}


def test_the_evidence_the_registry_publishes_is_pulled_and_ignored():
    """bundle.json is opened. The other two are never mentioned.

    They land in the tool cache regardless, because pull() writes every layer
    the manifest lists -- and verify_against_manifest hashes all of them, so
    their identity is established and then discarded.
    """
    source = Path(REPO_ROOT / "convert.py").read_text()
    assert "bundle.json" in source
    assert "build-evidence.json" not in source
    assert "sbom.cdx.json" not in source


def test_nothing_records_which_binary_actually_ran():
    """materialise_tools copies one in per node and says nothing about it."""
    source = inspect.getsource(convert.materialise_tools)
    assert "place_executable" in source
    for absent in ("digest", "sha256", "manifest", "record"):
        assert absent not in source, absent


def test_the_exit_code_is_kept_only_when_it_is_non_zero():
    """And then only as an error detail, which is not a record of the run."""
    source = inspect.getsource(main.perform_run)
    assert '"exit_code": code' in source
    assert source.index("if code != 0") < source.index('"exit_code": code')


def test_the_workspace_holding_the_evidence_is_deleted():
    """Retention (#13) keeps outputs, not a description of how they were made."""
    source = inspect.getsource(main.perform_run)
    assert "ws.cleanup()" in source
